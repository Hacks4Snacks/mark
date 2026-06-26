"""Copilot CLI / agent session store.

The CLI keeps a live SQLite store at ``~/.copilot/session-store.db`` plus a
per-session event log at ``~/.copilot/session-state/<id>/events.jsonl``. The
event log is authoritative for turns (the store's ``assistant_response`` is empty
for recent interactive sessions) and for real token/cost metrics and the list of
files the agent created or modified.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .. import config
from ..persist import write_session
from .base import (
    _FENCE_RE,
    _URL_RE,
    ProgressCb,
    WatchedSource,
    _compute_cost,
    _derive_title,
    _epoch_ms_to_iso,
    _estimate_metrics,
    _repo_from_cwd,
    _ts_diff_seconds,
    _uri_to_path,
)


def _read_session_metrics(session_id: str, state_dir: Path) -> dict[str, Any] | None:
    """Real model/token/duration metrics from the CLI's per-session events.jsonl."""
    path = state_dir / session_id / "events.jsonl"
    if not path.exists():
        return None
    first_ts = last_ts = None
    start_model = None
    out_acc = 0
    msg_models: list[str] = []
    shutdown: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = ev.get("timestamp")
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts
                et = ev.get("type")
                data = ev.get("data")
                if not isinstance(data, dict):
                    continue
                if et == "session.start":
                    start_model = data.get("selectedModel") or start_model
                elif et == "session.model_change":
                    start_model = data.get("newModel") or start_model
                elif et == "assistant.message":
                    out_acc += int(data.get("outputTokens") or 0)
                    if data.get("model"):
                        msg_models.append(data["model"])
                elif et == "session.shutdown":
                    shutdown = data
    except OSError:
        return None

    inp = outp = cread = cwrite = 0
    premium = aiu = None
    model = start_model
    if shutdown:
        mm = shutdown.get("modelMetrics") or {}
        for m in mm.values():
            usage = (m or {}).get("usage") or {}
            inp += int(usage.get("inputTokens") or 0)
            outp += int(usage.get("outputTokens") or 0)
            cread += int(usage.get("cacheReadTokens") or 0)
            cwrite += int(usage.get("cacheWriteTokens") or 0)
        model = shutdown.get("currentModel") or model or next(iter(mm), None)
        premium = shutdown.get("totalPremiumRequests")
        nano = shutdown.get("totalNanoAiu")
        aiu = round(nano / 1e9, 3) if isinstance(nano, (int, float)) else None
    if outp == 0:
        outp = out_acc
    if not model and msg_models:
        model = Counter(msg_models).most_common(1)[0][0]

    duration = _ts_diff_seconds(first_ts, last_ts)
    if duration is None and shutdown and shutdown.get("sessionStartTime") and last_ts:
        duration = _ts_diff_seconds(
            _epoch_ms_to_iso(shutdown["sessionStartTime"]), last_ts
        )

    return {
        "duration_seconds": duration,
        "model": model,
        "input_tokens": inp,
        "output_tokens": outp,
        "premium_requests": premium,
        "aiu": aiu,
        "est_cost_usd": _compute_cost(model, inp, outp, cread, cwrite),
        "tokens_estimated": 1 if (inp == 0 and outp == 0) else 0,
    }


def _hash_cli_session(updated_at: str | None, turns: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    h.update((updated_at or "").encode("utf-8"))
    for t in turns:
        h.update((t["user_message"] or "").encode("utf-8", "ignore"))
        h.update((t["assistant_response"] or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def _snapshot_store(src: Path) -> Path:
    """Read a consistent snapshot of the (possibly live) store via SQLite backup."""
    config.ensure_dirs()
    dest = config.DATA_DIR / "_copilot_store_snapshot.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(dest) + suffix).unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest


def _cli_turns(ro: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = ro.execute(
        "SELECT turn_index, user_message, assistant_response, timestamp "
        "FROM turns WHERE session_id = ? ORDER BY turn_index",
        (session_id,),
    ).fetchall()
    turns: list[dict[str, Any]] = []
    for r in rows:
        um = (r["user_message"] or "").strip()
        ar = (r["assistant_response"] or "").strip()
        if not um and not ar:
            continue
        code_blocks = [
            {"language": (lang or "").strip() or None, "content": code.strip()}
            for lang, code in _FENCE_RE.findall(ar)
        ]
        urls = list(
            dict.fromkeys(u.rstrip(".,);") for u in _URL_RE.findall(f"{um} {ar}"))
        )
        turns.append(
            {
                "turn_index": r["turn_index"],
                "user_message": um,
                "assistant_response": ar,
                "tools": [],
                "timestamp": r["timestamp"],
                "files": [],
                "urls": urls,
                "code_blocks": code_blocks,
            }
        )
    return turns


# Tool names whose arguments carry the path of a file the agent writes/creates.
_FILE_WRITE_TOOLS = {
    "create_file",
    "create_directory",
    "write",
    "write_file",
    "edit",
    "edit_file",
    "str_replace",
    "str_replace_editor",
    "apply_patch",
    "replace_string_in_file",
    "multi_replace_string_in_file",
    "insert_edit_into_file",
}
_FILE_ARG_KEYS = ("filePath", "file_path", "path", "filename", "target_file", "uri")


def _tool_file_path(name: str | None, args: Any) -> str | None:
    if not name or name not in _FILE_WRITE_TOOLS or not isinstance(args, dict):
        return None
    for key in _FILE_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return _uri_to_path(val.strip()) or val.strip()
    return None


def _finish_event_turn(turn: dict[str, Any]) -> dict[str, Any]:
    ar = (turn["assistant_response"] or "").strip()
    um = (turn["user_message"] or "").strip()
    turn["tools"] = list(dict.fromkeys(turn["tools"]))
    turn["files"] = list(dict.fromkeys(turn["files"]))
    # Some turns reply purely with tool calls (e.g. ask_user / report_intent) and
    # carry no prose. Surface the tools so the turn isn't rendered as empty.
    if not ar and turn["tools"]:
        ar = "↳ " + ", ".join(turn["tools"])
    turn["user_message"] = um
    turn["assistant_response"] = ar
    turn["code_blocks"] = [
        {"language": (lang or "").strip() or None, "content": code.strip()}
        for lang, code in _FENCE_RE.findall(ar)
    ]
    turn["urls"] = list(
        dict.fromkeys(u.rstrip(".,);") for u in _URL_RE.findall(f"{um} {ar}"))
    )
    return turn


def _events_to_turns(
    session_id: str, state_dir: Path
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Reconstruct turns + agent-modified files from the CLI per-session log.

    The store's ``turns.assistant_response`` is empty for recent sessions; the
    authoritative assistant text, tool calls and file writes live in
    ``~/.copilot/session-state/<id>/events.jsonl``. Returns ``None`` when the log
    is missing so callers can fall back to the store's ``turns`` table.
    """
    path = state_dir / session_id / "events.jsonl"
    if not path.exists():
        return None
    turns: list[dict[str, Any]] = []
    files_modified: list[str] = []
    cur_turn: dict[str, Any] | None = None

    def new_turn(user: str, ts: str | None) -> dict[str, Any]:
        return {
            "turn_index": len(turns),
            "user_message": user,
            "assistant_response": "",
            "tools": [],
            "files": [],
            "timestamp": ts,
        }

    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                et = ev.get("type")
                data = ev.get("data")
                if not isinstance(data, dict):
                    continue
                ts = ev.get("timestamp")
                if et == "user.message":
                    content = (data.get("content") or "").strip()
                    # The CLI emits an empty user.message between turn_start and
                    # the assistant reply; it must not start a new turn.
                    if not content:
                        continue
                    if cur_turn is not None:
                        turns.append(_finish_event_turn(cur_turn))
                    cur_turn = new_turn(content, ts)
                elif et == "assistant.message":
                    if cur_turn is None:
                        cur_turn = new_turn("", ts)
                    content = data.get("content")
                    if isinstance(content, str) and content.strip():
                        sep = "\n\n" if cur_turn["assistant_response"] else ""
                        cur_turn["assistant_response"] += sep + content.strip()
                    for tr in data.get("toolRequests") or []:
                        if not isinstance(tr, dict):
                            continue
                        nm = tr.get("name")
                        if nm:
                            cur_turn["tools"].append(nm)
                        fp = _tool_file_path(nm, tr.get("arguments"))
                        if fp:
                            cur_turn["files"].append(fp)
                elif et == "tool.execution_start":
                    if cur_turn is None:
                        cur_turn = new_turn("", ts)
                    nm = data.get("toolName")
                    if nm:
                        cur_turn["tools"].append(nm)
                    fp = _tool_file_path(nm, data.get("arguments"))
                    if fp:
                        cur_turn["files"].append(fp)
                elif et == "session.shutdown":
                    cc = data.get("codeChanges")
                    if isinstance(cc, dict):
                        for f in cc.get("filesModified") or []:
                            if isinstance(f, str) and f.strip():
                                files_modified.append(f.strip())
    except OSError:
        return None
    if cur_turn is not None:
        turns.append(_finish_event_turn(cur_turn))
    turns = [t for t in turns if t["user_message"] or t["assistant_response"]]
    files_modified = list(dict.fromkeys(files_modified))
    return turns, files_modified


def _read_attachment(path: str) -> dict[str, Any] | None:
    """Snapshot an agent-created file as a viewable attachment (text only)."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        size = p.stat().st_size
    except OSError:
        return None
    mime = mimetypes.guess_type(p.name)[0]
    base = {
        "filename": p.name,
        "stored_path": str(p),
        "mime": mime,
        "size_bytes": size,
        "content": None,
    }
    if size > config.MAX_ATTACHMENT_BYTES:
        return base
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return base
    try:
        base["content"] = raw.decode("utf-8")
    except UnicodeDecodeError:
        return base
    return base


class CopilotCliSource(WatchedSource):
    key = "copilot_cli"
    row_sources = ("cli",)

    def default_config(self) -> config.SourceConfig:
        return config.SourceConfig(
            key=self.key,
            roots=[config.COPILOT_STORE_PATH],
            label="Copilot CLI",
            options={"state_dir": str(config.SESSION_STATE_DIR)},
        )

    def fingerprint(self, cfg: config.SourceConfig) -> str:
        # The main db plus its write-ahead log, which is what changes as turns
        # are appended during/after a session.
        if not cfg.roots:
            return ""
        store = cfg.roots[0]
        parts: list[str] = []
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{store}{suffix}")
            try:
                st = p.stat()
                parts.append(f"c{suffix}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                pass
        return "|".join(parts)

    def ingest(
        self,
        cur,
        existing: dict[str, str],
        cfg: config.SourceConfig,
        *,
        rebuild: bool,
        progress: ProgressCb | None = None,
    ) -> dict[str, int]:
        """Index sessions from the Copilot CLI / agent store."""
        counts = {"added": 0, "updated": 0, "skipped": 0}
        if not cfg.roots:
            return counts
        src = cfg.roots[0]
        if not src.exists():
            return counts
        state_dir = Path(cfg.options.get("state_dir") or config.SESSION_STATE_DIR)

        snapshot = _snapshot_store(src)
        ro = sqlite3.connect(snapshot)
        ro.row_factory = sqlite3.Row
        try:
            files_by: dict[str, list[tuple[str, str | None, int | None]]] = {}
            try:
                for fr in ro.execute(
                    "SELECT session_id, file_path, tool_name, turn_index FROM session_files"
                ):
                    files_by.setdefault(fr["session_id"], []).append(
                        (fr["file_path"], fr["tool_name"], fr["turn_index"])
                    )
            except sqlite3.Error:
                pass

            sessions = ro.execute(
                "SELECT id, cwd, repository, summary, created_at, updated_at FROM sessions"
            ).fetchall()
            seen = 0
            for s in sessions:
                sid = s["id"]
                events = _events_to_turns(sid, state_dir)
                if events and events[0]:
                    turns, files_modified = events
                else:
                    turns = _cli_turns(ro, sid)
                    files_modified = []
                if not turns:
                    continue
                content_hash = _hash_cli_session(s["updated_at"], turns)
                prior = existing.get(sid)
                if prior is not None and prior == content_hash and not rebuild:
                    counts["skipped"] += 1
                    continue
                metrics = _read_session_metrics(sid, state_dir) or _estimate_metrics(
                    turns
                )
                if metrics.get("duration_seconds") is None:
                    metrics["duration_seconds"] = _ts_diff_seconds(
                        s["created_at"], s["updated_at"]
                    )
                # Agent-created/modified files become session attachments. The
                # git-diff-derived filesModified list is authoritative; supplement
                # it with paths seen in structured file-write tool calls.
                agent_files: list[str] = list(files_modified)
                for t in turns:
                    agent_files.extend(t["files"])
                agent_files = list(dict.fromkeys(p for p in agent_files if p))
                extra_files = list(files_by.get(sid, []))
                extra_files.extend((p, "agent", None) for p in files_modified)
                attachments: list[dict[str, Any]] = []
                for fp in agent_files:
                    att = _read_attachment(fp)
                    if att:
                        attachments.append(att)
                session = {
                    "id": sid,
                    "source": "cli",
                    "title": _derive_title(turns),
                    "workspace_id": None,
                    "repository": _repo_from_cwd(s["repository"], s["cwd"]),
                    "repo_path": s["cwd"],
                    "requester": None,
                    "responder": "GitHub Copilot",
                    "created_at": s["created_at"],
                    "updated_at": s["updated_at"] or s["created_at"],
                    "source_path": str(src),
                    "content_hash": content_hash,
                    "turns": turns,
                    "metrics": metrics,
                    "extra_files": extra_files,
                    "attachments": attachments,
                }
                write_session(cur, session, light=True)
                counts["added" if prior is None else "updated"] += 1
                seen += 1
                if progress and seen % 100 == 0:
                    progress(f"Indexed {seen} Copilot CLI sessions...")
        finally:
            ro.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(snapshot) + suffix).unlink(missing_ok=True)
        return counts
