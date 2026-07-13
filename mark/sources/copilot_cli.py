from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .. import attachments as attachment_store
from .. import config
from ..persist import _write_session, load_file_signatures, record_file_signature
from .base import (
    FENCE_RE,
    URL_RE,
    ProgressCb,
    WatchedSource,
    cleanup_snapshot,
    compute_cost,
    derive_title,
    epoch_ms_to_iso,
    estimate_metrics,
    repo_from_cwd,
    snapshot_sqlite,
    tool_trace,
    ts_diff_seconds,
    uri_to_path,
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

    duration = ts_diff_seconds(first_ts, last_ts)
    if duration is None and shutdown and shutdown.get("sessionStartTime") and last_ts:
        duration = ts_diff_seconds(
            epoch_ms_to_iso(shutdown["sessionStartTime"]), last_ts
        )

    return {
        "duration_seconds": duration,
        "model": model,
        "input_tokens": inp,
        "output_tokens": outp,
        "premium_requests": premium,
        "aiu": aiu,
        "est_cost_usd": compute_cost(model, inp, outp, cread, cwrite),
        "tokens_estimated": 1 if (inp == 0 and outp == 0) else 0,
    }


def _hash_cli_session(
    updated_at: str | None,
    turns: list[dict[str, Any]],
    files_modified: list[str] | None = None,
) -> str:
    h = hashlib.sha256()
    h.update(f"attachment-capture-v{attachment_store.CAPTURE_VERSION}\0".encode())
    h.update((updated_at or "").encode("utf-8"))
    for t in turns:
        h.update((t["user_message"] or "").encode("utf-8", "ignore"))
        h.update((t["assistant_response"] or "").encode("utf-8", "ignore"))
        for path in t.get("files") or []:
            h.update(b"\0write\0")
            h.update(path.encode("utf-8", "ignore"))
    for path in files_modified or []:
        h.update(b"\0shutdown-write\0")
        h.update(path.encode("utf-8", "ignore"))
    return h.hexdigest()


def _cli_session_signature(sid: str, updated_at: str | None, state_dir: Path) -> str:
    """A cheap per-session change signature.

    Combines the store's ``updated_at`` with the size/mtime of the authoritative
    ``events.jsonl`` the CLI appends to as a session runs. It changes whenever a
    turn is added (to the store or the event log), so an unchanged signature
    means an unchanged session that need not be re-parsed.
    """
    ev = state_dir / sid / "events.jsonl"
    try:
        st = ev.stat()
        ev_part = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        ev_part = "0:0"
    return f"{updated_at or ''}|{ev_part}"


def _live_session_signatures(src: Path, state_dir: Path) -> dict[str, str] | None:
    """Per-session signatures read straight from the (possibly live) store.

    Reads only session ids + ``updated_at`` over a read-only connection (no
    whole-store backup) and pairs each with its events.jsonl stat. Returns
    ``None`` if the store can't be opened, so the caller falls back to a full pass.
    """
    try:
        ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        rows = ro.execute("SELECT id, updated_at FROM sessions").fetchall()
    except sqlite3.Error:
        return None
    finally:
        ro.close()
    return {
        r["id"]: _cli_session_signature(r["id"], r["updated_at"], state_dir)
        for r in rows
    }


def _snapshot_store(src: Path) -> Path:
    """Read a consistent snapshot of the (possibly live) store for safe reading."""
    return snapshot_sqlite(src, config.DATA_DIR / "_copilot_store_snapshot.db")


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
            for lang, code in FENCE_RE.findall(ar)
        ]
        urls = list(
            dict.fromkeys(u.rstrip(".,);") for u in URL_RE.findall(f"{um} {ar}"))
        )
        turns.append(
            {
                "turn_index": r["turn_index"],
                "user_message": um,
                "assistant_response": ar,
                "thinking": "",
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
    paths: list[str] = []
    for key in _FILE_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            paths.append(uri_to_path(val.strip()) or val.strip())
    unique = list(dict.fromkeys(paths))
    return unique[0] if len(unique) == 1 else None


def _join_segments(segments: list[list[str]]) -> str:
    """Join the ordered (kind, text) trace into markdown: prose and each tool
    call become blank-line-separated blocks (a tool call is one line, or a fenced
    code block when its argument spans several lines)."""
    return "\n\n".join(t for _, t in segments if t and t.strip()).strip()


def _finish_event_turn(turn: dict[str, Any]) -> dict[str, Any]:
    um = (turn["user_message"] or "").strip()
    turn["tools"] = list(dict.fromkeys(turn["tools"]))
    turn["files"] = list(dict.fromkeys(turn["files"]))
    ar = _join_segments(turn.get("segments") or [])
    # A turn that is pure tool activity with no captured trace still names its
    # tools so it is never rendered empty.
    if not ar and turn["tools"]:
        ar = ", ".join(f"`▷ {t}`" for t in turn["tools"])
    turn["user_message"] = um
    turn["assistant_response"] = ar
    turn["thinking"] = (turn.get("thinking") or "").strip()
    turn["code_blocks"] = [
        {"language": (lang or "").strip() or None, "content": code.strip()}
        for lang, code in FENCE_RE.findall(ar)
    ]
    turn["urls"] = list(
        dict.fromkeys(u.rstrip(".,);") for u in URL_RE.findall(f"{um} {ar}"))
    )
    turn.pop("segments", None)
    turn.pop("_calls", None)
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
    write_ledger: dict[str, dict[str, Any]] = {}

    def new_turn(user: str, ts: str | None) -> dict[str, Any]:
        return {
            "turn_index": len(turns),
            "user_message": user,
            # Ordered [kind, text] trace (kind in {"prose", "tool"}) joined into
            # the assistant_response when the turn is finished.
            "segments": [],
            "thinking": "",
            "tools": [],
            "files": [],
            "timestamp": ts,
            "_calls": {},  # toolCallId -> segment index, for marking failures
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
                        cur_turn["segments"].append(["prose", content.strip()])
                    # Plaintext model reasoning (only some messages carry it; the
                    # reasoningOpaque/encryptedContent variants are not decodable).
                    rt = data.get("reasoningText")
                    if isinstance(rt, str) and rt.strip():
                        tsep = "\n\n" if cur_turn["thinking"] else ""
                        cur_turn["thinking"] += tsep + rt.strip()
                    # The ordered execution stream below is authoritative for
                    # outcomes. Keep requested tool names for trace fidelity,
                    # but never trust a requested path as a completed write.
                    for tr in data.get("toolRequests") or []:
                        if not isinstance(tr, dict):
                            continue
                        nm = tr.get("name")
                        if nm:
                            cur_turn["tools"].append(nm)
                elif et == "tool.execution_start":
                    if cur_turn is None:
                        cur_turn = new_turn("", ts)
                    nm = data.get("toolName")
                    args = data.get("arguments")
                    if nm:
                        cur_turn["tools"].append(nm)
                    fp = _tool_file_path(nm, args)
                    # Inline the agent's actions in order: this is what makes a
                    # mostly-autonomous run (one prompt, dozens of tool turns)
                    # read as a conversation instead of just its first prose and
                    # its last.
                    call_id = data.get("toolCallId")
                    valid_call_id = isinstance(call_id, str) and bool(call_id.strip())
                    if valid_call_id:
                        entry = write_ledger.setdefault(
                            call_id,
                            {
                                "starts": 0,
                                "paths": [],
                                "terminals": [],
                                "turn": None,
                                "invalid": False,
                            },
                        )
                        entry["starts"] += 1
                        if entry["turn"] is None:
                            entry["turn"] = cur_turn
                        if fp:
                            entry["paths"].append(fp)
                        if entry["starts"] == 1:
                            cur_turn["_calls"][call_id] = len(cur_turn["segments"])
                        else:
                            for turn in (entry["turn"], cur_turn):
                                calls = turn.get("_calls")
                                if calls is not None:
                                    calls.pop(call_id, None)
                    cur_turn["segments"].append(["tool", tool_trace(nm, args)])
                elif et == "tool.execution_complete":
                    call_id = data.get("toolCallId")
                    valid_call_id = isinstance(call_id, str) and bool(call_id.strip())
                    if valid_call_id:
                        entry = write_ledger.setdefault(
                            call_id,
                            {
                                "starts": 0,
                                "paths": [],
                                "terminals": [],
                                "turn": None,
                                "invalid": False,
                            },
                        )
                        if entry["starts"] == 0:
                            entry["invalid"] = True
                        entry["terminals"].append(data.get("success"))
                    # Annotate only explicit failures; successes are implied.
                    if cur_turn is None or data.get("success") is not False:
                        continue
                    idx = cur_turn["_calls"].get(call_id)
                    if idx is not None and idx < len(cur_turn["segments"]):
                        seg = cur_turn["segments"][idx]
                        # Mark the header line so a fenced multi-line arg stays intact.
                        head, nl, rest = seg[1].partition("\n")
                        seg[1] = f"{head} — failed{nl}{rest}"
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
    for entry in write_ledger.values():
        turn = entry["turn"]
        if (
            turn is not None
            and not entry["invalid"]
            and entry["starts"] == 1
            and len(entry["paths"]) == 1
            and len(entry["terminals"]) == 1
            and entry["terminals"][0] is True
        ):
            turn["files"].append(entry["paths"][0])
    for turn in turns:
        turn["files"] = list(dict.fromkeys(turn["files"]))
    turns = [t for t in turns if t["user_message"] or t["assistant_response"]]
    files_modified = list(dict.fromkeys(files_modified))
    return turns, files_modified


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
        state_dir = Path(
            cfg.options.get("state_dir") or config.SESSION_STATE_DIR
        ).expanduser()
        event_hash = hashlib.sha256()
        event_count = 0
        for event_path in sorted(
            state_dir.glob("*/events.jsonl"), key=lambda p: p.parent.name
        ):
            try:
                st = event_path.stat()
            except OSError:
                continue
            event_count += 1
            event_hash.update(event_path.parent.name.encode("utf-8", "ignore"))
            event_hash.update(f":{st.st_mtime_ns}:{st.st_size}\0".encode())
        parts.append(f"events:{event_count}:{event_hash.hexdigest()}")
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
        state_dir = Path(
            cfg.options.get("state_dir") or config.SESSION_STATE_DIR
        ).expanduser()

        sigs = load_file_signatures(cur, prefix="cli:")
        # Cheap pre-check on the live store (read-only, no backup): if no session's
        # signature changed since the last successful ingest, there is nothing to
        # do — so skip the expensive whole-store SQLite backup entirely.
        live_sigs = _live_session_signatures(src, state_dir)
        if (
            live_sigs is not None
            and not rebuild
            and all(sigs.get(f"cli:{sid}") == sig for sid, sig in live_sigs.items())
        ):
            counts["skipped"] = len(live_sigs)
            return counts

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
                sig = _cli_session_signature(sid, s["updated_at"], state_dir)
                # Skip the events.jsonl re-parse + re-hash for an already-indexed
                # session whose cheap signature is unchanged.
                if (
                    not rebuild
                    and existing.get(sid) is not None
                    and sigs.get(f"cli:{sid}") == sig
                ):
                    counts["skipped"] += 1
                    continue
                events = _events_to_turns(sid, state_dir)
                if events and events[0]:
                    turns, files_modified = events
                else:
                    turns = _cli_turns(ro, sid)
                    files_modified = []
                if not turns:
                    continue
                content_hash = _hash_cli_session(s["updated_at"], turns, files_modified)
                prior = existing.get(sid)
                if prior is not None and prior == content_hash and not rebuild:
                    record_file_signature(cur, f"cli:{sid}", sig)
                    counts["skipped"] += 1
                    continue
                metrics = _read_session_metrics(sid, state_dir) or estimate_metrics(
                    turns
                )
                if metrics.get("duration_seconds") is None:
                    metrics["duration_seconds"] = ts_diff_seconds(
                        s["created_at"], s["updated_at"]
                    )
                # Successful writes and shutdown code changes are candidates,
                # but only files contained by this session's workspace are read.
                agent_files: list[str] = list(files_modified)
                for t in turns:
                    agent_files.extend(t["files"])
                agent_files = list(dict.fromkeys(p for p in agent_files if p))
                extra_files = list(files_by.get(sid, []))
                extra_files.extend((p, "agent", None) for p in files_modified)
                attachments: list[dict[str, Any]] = []
                for fp in agent_files:
                    att = attachment_store.snapshot_file(
                        fp,
                        workspace=s["cwd"],
                        session_id=sid,
                    )
                    if att:
                        attachments.append(att)
                session = {
                    "id": sid,
                    "source": "cli",
                    "title": derive_title(turns),
                    "workspace_id": None,
                    "repository": repo_from_cwd(s["repository"], s["cwd"]),
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
                _write_session(cur, session)
                record_file_signature(cur, f"cli:{sid}", sig)
                counts["added" if prior is None else "updated"] += 1
                seen += 1
                if progress and seen % 100 == 0:
                    progress(f"Indexed {seen} Copilot CLI sessions...")
        finally:
            ro.close()
            cleanup_snapshot(snapshot)
        return counts
