"""Cursor editor chat / Composer history.

Cursor (a VS Code fork) keeps its AI conversations in a SQLite key-value store at
``.../Cursor/User/globalStorage/state.vscdb``. Two key shapes matter in its
``cursorDiskKV`` table:

* ``composerData:<composerId>`` — one conversation: ``name`` (title),
  ``createdAt``/``lastUpdatedAt`` (epoch ms), ``unifiedMode`` (``agent``/``chat``)
  and ``fullConversationHeadersOnly``, an ordered list of
  ``{bubbleId, type}`` pointers (``type`` 1 = user, 2 = assistant).
* ``bubbleId:<composerId>:<bubbleId>`` — one message: ``text`` (markdown), an
  optional ``toolFormerData`` tool call (``name``/``rawArgs``/``result``) and
  ``codeBlocks``.

Per-workspace ``.../Cursor/User/workspaceStorage/<id>/state.vscdb`` databases keep a
``composer.composerData`` list that maps each composer to its workspace folder,
which is how a session gets its repository attribution.

The global store can be many gigabytes, so this adapter never copies it: it reads
read-only (honoring the WAL) and uses primary-key lookups. Unchanged conversations
are skipped from a cheap metadata hash before any message bubbles are read. The
model is read from ``composerData.modelConfig.modelName``; per-bubble ``tokenCount``
supplies real usage. Because agentic turns re-send the whole conversation, the
cumulative ``inputTokens`` are treated as prompt-cache reads (only the single
largest context is billed as a fresh write) while ``outputTokens`` are billed
verbatim; ``isRefunded`` bubbles are excluded. Conversations with no recorded
tokens fall back to a text-length estimate (``tokens_estimated``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

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
    _friendly_repo,
    _turns_duration,
    _uri_to_path,
)

_BUBBLE_USER = 1
_BUBBLE_ASSISTANT = 2

# Tool-argument keys that carry the path of a file the agent read or edited.
_FILE_ARG_KEYS = (
    "target_file",
    "file_path",
    "relative_workspace_path",
    "path",
    "filename",
    "uri",
)


# --- workspace → repository mapping ------------------------------------------


def _ro_connect(path: Path) -> sqlite3.Connection | None:
    """Open a Cursor SQLite store read-only (honoring the WAL); None on failure."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=5000")
        return con
    except sqlite3.Error:
        return None


def load_workspace_map(roots: list[Path]) -> dict[str, dict[str, str | None]]:
    """Map each composerId to its workspace id / repository path + name."""
    mapping: dict[str, dict[str, str | None]] = {}
    for root in roots:
        for wsdb in root.glob("*/state.vscdb"):
            wsdir = wsdb.parent
            ws_id = wsdir.name
            folder = None
            wsjson = wsdir / "workspace.json"
            if wsjson.exists():
                try:
                    folder = json.loads(wsjson.read_text()).get("folder")
                except (OSError, json.JSONDecodeError):
                    folder = None
            path = _uri_to_path(folder) if folder else None
            name = _friendly_repo(path)

            con = _ro_connect(wsdb)
            if con is None:
                continue
            try:
                row = con.execute(
                    "SELECT value FROM ItemTable WHERE key='composer.composerData'"
                ).fetchone()
            except sqlite3.Error:
                row = None  # workspace store without an ItemTable
            finally:
                con.close()
            if not row:
                continue
            try:
                composers = (json.loads(row[0]).get("allComposers")) or []
            except (json.JSONDecodeError, AttributeError):
                continue
            for entry in composers:
                cid = entry.get("composerId") if isinstance(entry, dict) else None
                if cid:
                    mapping[cid] = {
                        "workspace_id": ws_id,
                        "name": name,
                        "path": path,
                    }
    return mapping


# --- bubble / turn extraction ------------------------------------------------


def _parse_raw_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _looks_like_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and ("/" in value or "." in value)
    )


def _assistant_segment(bubble: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Render one assistant/tool bubble to (text, tools, files)."""
    text = (bubble.get("text") or "").strip()
    tools: list[str] = []
    files: list[str] = []

    tfd = bubble.get("toolFormerData")
    if isinstance(tfd, dict) and tfd.get("name"):
        name = tfd["name"]
        tools.append(name)
        args = _parse_raw_args(tfd.get("rawArgs"))
        for key in _FILE_ARG_KEYS:
            if _looks_like_path(args.get(key)):
                files.append(args[key].strip())
        preview = json.dumps(args, ensure_ascii=False)[:60] if args else ""
        trace = f"`▷ {name}` {preview}".rstrip()
        result = tfd.get("result")
        rtext = (
            result if isinstance(result, str) else json.dumps(result) if result else ""
        )
        rtext = " ".join((rtext or "").split())
        if rtext:
            trace += f"\n  ⮑ {rtext[:100]}"
        text = f"{text}\n{trace}" if text else trace

    return text, tools, files


def _composer_turns(bubbles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each user prompt with the assistant's response (incl. tool activity)."""
    turns: list[dict[str, Any]] = []
    cur_user: str | None = None
    cur_asst: list[str] = []
    cur_tools: list[str] = []
    cur_files: list[str] = []

    def flush() -> None:
        nonlocal cur_user, cur_asst, cur_tools, cur_files
        if cur_user is None and not cur_asst:
            return
        asst = "".join(cur_asst).strip()
        user = (cur_user or "").strip()
        if user or asst:
            code_blocks = [
                {"language": (lang or "").strip() or None, "content": code.strip()}
                for lang, code in _FENCE_RE.findall(asst)
            ]
            urls = list(
                dict.fromkeys(
                    u.rstrip(".,);") for u in _URL_RE.findall(f"{user} {asst}")
                )
            )
            turns.append(
                {
                    "turn_index": len(turns),
                    "user_message": user,
                    "assistant_response": asst,
                    "tools": list(dict.fromkeys(cur_tools)),
                    "timestamp": None,
                    "files": list(dict.fromkeys(cur_files)),
                    "urls": urls,
                    "code_blocks": code_blocks,
                }
            )
        cur_user, cur_asst, cur_tools, cur_files = None, [], [], []

    for b in bubbles:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == _BUBBLE_USER:
            if (b.get("text") or "").strip():
                flush()
                cur_user = (b.get("text") or "").strip()
            # An empty user bubble is a tool-result placeholder — ignore it.
        elif btype == _BUBBLE_ASSISTANT:
            seg, tools, files = _assistant_segment(b)
            if seg:
                cur_asst.append(seg + "\n")
            cur_tools.extend(tools)
            cur_files.extend(files)
    flush()
    return turns


# --- composer parsing --------------------------------------------------------


def _composer_hash(data: dict[str, Any], headers: list[dict[str, Any]]) -> str:
    """Cheap change signature from metadata only — no bubble reads required."""
    last = headers[-1].get("bubbleId") if headers else ""
    sig = f"{data.get('lastUpdatedAt')}:{len(headers)}:{last}"
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()


def _model_name(data: dict[str, Any]) -> str | None:
    """Real model from ``modelConfig.modelName``; ``default``/empty → None."""
    mc = data.get("modelConfig")
    name = (mc.get("modelName") if isinstance(mc, dict) else None) or ""
    name = name.strip()
    return None if not name or name.lower() == "default" else name


def _token_totals(bubbles: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    """Sum (input, output, peak_input, billable_requests) over usable bubbles.

    ``inputTokens`` is the whole re-sent context (cumulative across agentic
    turns); ``outputTokens`` is the fresh generation. ``isRefunded`` bubbles were
    credited back, so they are excluded.
    """
    sum_in = sum_out = peak = requests = 0
    for b in bubbles:
        if not isinstance(b, dict) or b.get("isRefunded"):
            continue
        tc = b.get("tokenCount")
        if not isinstance(tc, dict):
            continue
        ti = int(tc.get("inputTokens") or 0)
        to = int(tc.get("outputTokens") or 0)
        sum_in += ti
        sum_out += to
        peak = max(peak, ti)
        if to > 0:
            requests += 1
    return sum_in, sum_out, peak, requests


def _cursor_metrics(
    data: dict[str, Any],
    bubbles: list[dict[str, Any]],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Real model + cache-aware token cost, or a text estimate when absent."""
    model = _model_name(data)
    sum_in, sum_out, peak, requests = _token_totals(bubbles)
    if sum_in == 0 and sum_out == 0:
        metrics = _estimate_metrics(turns)
        metrics["model"] = model
        return metrics
    # Agentic turns re-send the conversation each request, so the cumulative
    # inputTokens are almost entirely prompt-cache reads; only the single largest
    # context is billed as a fresh write. Output tokens are billed verbatim.
    cost = _compute_cost(
        model,
        sum_in,
        sum_out,
        cache_read=max(0, sum_in - peak),
        cache_write=peak,
        input_includes_cache=True,
    )
    return {
        "duration_seconds": _turns_duration(turns),
        "model": model,
        "input_tokens": sum_in,
        "output_tokens": sum_out,
        "premium_requests": requests or None,
        "aiu": None,
        "est_cost_usd": cost,
        "tokens_estimated": 0,
    }


def _load_bubbles(
    con: sqlite3.Connection, composer_id: str, data: dict[str, Any]
) -> list[dict[str, Any]]:
    headers = data.get("fullConversationHeadersOnly")
    if not headers and isinstance(data.get("conversation"), list):
        return [b for b in data["conversation"] if isinstance(b, dict)]
    bubbles: list[dict[str, Any]] = []
    for h in headers or []:
        bid = h.get("bubbleId") if isinstance(h, dict) else None
        if not bid:
            continue
        row = con.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"bubbleId:{composer_id}:{bid}",),
        ).fetchone()
        if not row:
            continue
        try:
            bubbles.append(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError):
            continue
    return bubbles


def _build_session(
    data: dict[str, Any],
    composer_id: str,
    turns: list[dict[str, Any]],
    bubbles: list[dict[str, Any]],
    content_hash: str,
    repo: dict[str, str | None],
    db_path: Path,
) -> dict[str, Any]:
    name = (data.get("name") or "").strip()
    title = name or _derive_title(turns)
    if len(title) > 90:
        title = title[:90].rstrip() + "..."
    created = _epoch_ms_to_iso(data.get("createdAt"))
    updated = _epoch_ms_to_iso(data.get("lastUpdatedAt")) or created
    return {
        "id": f"cursor-{composer_id}",
        "source": "cursor",
        "title": title,
        "workspace_id": repo.get("workspace_id"),
        "repository": repo.get("name"),
        "repo_path": repo.get("path"),
        "requester": None,
        "responder": "cursor",
        "created_at": created,
        "updated_at": updated,
        "source_path": f"{db_path}::{composer_id}",
        "content_hash": content_hash,
        "turns": turns,
        "metrics": _cursor_metrics(data, bubbles, turns),
    }


def _composer_keys(con: sqlite3.Connection) -> Iterable[str]:
    for (key,) in con.execute(
        "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ):
        yield key


class CursorSource(WatchedSource):
    key = "cursor"
    row_sources = ("cursor",)

    def default_config(self) -> config.SourceConfig:
        return config.SourceConfig(
            key=self.key,
            roots=config.cursor_global_db_paths(),
            label="Cursor",
            options={
                "workspace_roots": [
                    str(p) for p in config.cursor_workspace_storage_roots()
                ]
            },
        )

    def fingerprint(self, cfg: config.SourceConfig) -> str:
        parts: list[str] = []
        for store in cfg.roots:
            for suffix in ("", "-wal", "-shm"):
                p = Path(f"{store}{suffix}")
                try:
                    st = p.stat()
                except OSError:
                    continue
                parts.append(f"cu{suffix}:{st.st_mtime_ns}:{st.st_size}")
        return "|".join(parts)

    def _workspace_roots(self, cfg: config.SourceConfig) -> list[Path]:
        opt = cfg.options.get("workspace_roots")
        if opt:
            return [Path(p).expanduser() for p in opt]
        return config.cursor_workspace_storage_roots()

    def ingest(
        self,
        cur,
        existing: dict[str, str],
        cfg: config.SourceConfig,
        *,
        rebuild: bool,
        progress: ProgressCb | None = None,
    ) -> dict[str, int]:
        """Index Cursor composer conversations from the globalStorage store(s)."""
        counts = {"added": 0, "updated": 0, "skipped": 0}
        wsmap = load_workspace_map(self._workspace_roots(cfg))
        seen = 0
        for store in cfg.roots:
            if not Path(store).exists():
                continue
            con = _ro_connect(Path(store))
            if con is None:
                continue
            try:
                for key in list(_composer_keys(con)):
                    row = con.execute(
                        "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)
                    ).fetchone()
                    if not row:
                        continue
                    try:
                        data = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    composer_id = data.get("composerId") or key.split(":", 1)[-1]
                    headers = data.get("fullConversationHeadersOnly") or []
                    content_hash = _composer_hash(data, headers)
                    sid = f"cursor-{composer_id}"
                    prior = existing.get(sid)
                    if prior is not None and prior == content_hash and not rebuild:
                        counts["skipped"] += 1
                        continue
                    bubbles = _load_bubbles(con, composer_id, data)
                    turns = _composer_turns(bubbles)
                    if not turns:
                        continue
                    session = _build_session(
                        data,
                        composer_id,
                        turns,
                        bubbles,
                        content_hash,
                        wsmap.get(composer_id, {}),
                        Path(store),
                    )
                    write_session(cur, session, light=True)
                    counts["added" if prior is None else "updated"] += 1
                    seen += 1
                    if progress and seen % 25 == 0:
                        progress(f"Indexed {seen} Cursor conversations...")
            finally:
                con.close()
        return counts
