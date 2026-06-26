"""VS Code Copilot chat sessions.

VS Code stores each chat under
``…/workspaceStorage/<id>/chatSessions/<uuid>.json``. Each file holds an ordered
list of ``requests`` (turns); every turn has a user ``message`` and a list of
``response`` parts (markdown text, tool invocations, file edits, references).
This adapter extracts clean, structured content and is defensive about schema
drift — unknown part kinds are simply ignored.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .. import config
from ..persist import write_session
from .base import (
    _FENCE_RE,
    _URL_RE,
    ProgressCb,
    WatchedSource,
    _derive_title,
    _epoch_ms_to_iso,
    _estimate_metrics,
    _friendly_repo,
    _uri_to_path,
)

# --- workspace → repository mapping ------------------------------------------


def load_workspace_map() -> dict[str, dict[str, str | None]]:
    """Map each workspaceStorage id to its repository path/name."""
    mapping: dict[str, dict[str, str | None]] = {}
    for root in config.vscode_storage_roots():
        for wj in root.glob("*/workspace.json"):
            ws_id = wj.parent.name
            try:
                data = json.loads(wj.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            folder = data.get("folder")
            path = _uri_to_path(folder) if folder else None
            if not path and data.get("workspace"):
                # Multi-root: resolve the .code-workspace and use its first folder.
                wpath = _uri_to_path(data["workspace"])
                path = wpath
            mapping[ws_id] = {"path": path, "name": _friendly_repo(path)}
    return mapping


# --- turn extraction ---------------------------------------------------------


def _extract_response(parts: Any) -> dict[str, Any]:
    """Pull text, tools, touched files, urls and code blocks from response parts."""
    text_segments: list[str] = []
    tools: list[str] = []
    files: list[str] = []
    urls: list[str] = []

    if not isinstance(parts, list):
        return {"text": "", "tools": [], "files": [], "urls": []}

    for p in parts:
        if not isinstance(p, dict):
            if isinstance(p, str):
                text_segments.append(p)
            continue
        kind = p.get("kind")
        if kind is None and "value" in p:  # markdown string part
            val = p["value"]
            text_segments.append(
                val if isinstance(val, str) else str(val.get("value", ""))
            )
        elif kind == "inlineReference":
            path = _uri_to_path(p.get("inlineReference") or p.get("reference"))
            if path:
                files.append(path)
        elif kind == "codeblockUri":
            path = _uri_to_path(p.get("uri"))
            if path:
                files.append(path)
        elif kind == "textEditGroup":
            path = _uri_to_path(p.get("uri"))
            if path:
                files.append(path)
        elif kind == "toolInvocationSerialized":
            tool = p.get("toolId") or p.get("toolName")
            if isinstance(tool, str):
                tools.append(tool)

    text = "".join(text_segments).strip()
    for m in _URL_RE.findall(text):
        urls.append(m.rstrip(".,);"))
    return {"text": text, "tools": tools, "files": files, "urls": urls}


def _content_references(req: dict[str, Any]) -> tuple[list[str], list[str]]:
    files: list[str] = []
    urls: list[str] = []
    for cr in req.get("contentReferences") or []:
        if not isinstance(cr, dict):
            continue
        ref = cr.get("reference") or cr.get("value")
        path = _uri_to_path(ref)
        if path:
            (urls if path.startswith("http") else files).append(path)
    return files, urls


def _parse_turn(req: dict[str, Any], index: int) -> dict[str, Any]:
    msg = req.get("message") or {}
    user_text = msg.get("text") if isinstance(msg, dict) else str(msg)
    resp = _extract_response(req.get("response"))
    cr_files, cr_urls = _content_references(req)

    assistant = resp["text"]
    code_blocks = [
        {"language": (lang or "").strip() or None, "content": code.strip()}
        for lang, code in _FENCE_RE.findall(assistant)
    ]

    files = list(dict.fromkeys(resp["files"] + cr_files))
    urls = list(dict.fromkeys(resp["urls"] + cr_urls))
    return {
        "turn_index": index,
        "user_message": (user_text or "").strip(),
        "assistant_response": assistant,
        "tools": list(dict.fromkeys(resp["tools"])),
        "timestamp": _epoch_ms_to_iso(req.get("timestamp")),
        "files": files,
        "urls": urls,
        "code_blocks": code_blocks,
    }


# --- session parsing ---------------------------------------------------------


def parse_session(
    path: Path, wsmap: dict[str, dict[str, str | None]]
) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    requests = data.get("requests") or []
    turns = [_parse_turn(r, i) for i, r in enumerate(requests) if isinstance(r, dict)]
    turns = [t for t in turns if t["user_message"] or t["assistant_response"]]
    if not turns:
        return None

    workspace_id = path.parent.parent.name
    repo = wsmap.get(workspace_id, {})
    session_id = data.get("sessionId") or path.stem

    return {
        "id": session_id,
        "source": "vscode",
        "title": _derive_title(turns),
        "workspace_id": workspace_id,
        "repository": repo.get("name"),
        "repo_path": repo.get("path"),
        "requester": data.get("requesterUsername"),
        "responder": data.get("responderUsername"),
        "created_at": _epoch_ms_to_iso(data.get("creationDate")),
        "updated_at": _epoch_ms_to_iso(data.get("lastMessageDate"))
        or _epoch_ms_to_iso(data.get("creationDate")),
        "source_path": str(path),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "turns": turns,
        "metrics": _estimate_metrics(turns),
    }


def iter_session_paths() -> Iterable[Path]:
    for root in config.vscode_storage_roots():
        yield from root.glob("*/chatSessions/*.json")


class VSCodeSource(WatchedSource):
    key = "vscode"

    def fingerprint(self) -> str:
        count = 0
        newest = 0
        for f in iter_session_paths():
            try:
                st = f.stat()
            except OSError:
                continue
            count += 1
            if st.st_mtime_ns > newest:
                newest = st.st_mtime_ns
        return f"vs:{count}:{newest}"

    def ingest(
        self,
        cur,
        existing: dict[str, str],
        *,
        rebuild: bool,
        progress: ProgressCb | None = None,
    ) -> dict[str, int]:
        wsmap = load_workspace_map()
        added = updated = skipped = 0
        for path in iter_session_paths():
            session = parse_session(path, wsmap)
            if not session:
                continue
            prior = existing.get(session["id"])
            if prior is not None and prior == session["content_hash"] and not rebuild:
                skipped += 1
                continue
            write_session(cur, session, light=True)
            added += 1 if prior is None else 0
            updated += 0 if prior is None else 1
            if progress:
                progress(f"Indexed VS Code: {session['title']!r}")
        return {"added": added, "updated": updated, "skipped": skipped}
