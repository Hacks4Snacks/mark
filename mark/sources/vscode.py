from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import config
from ..persist import _write_session, load_file_signatures, record_file_signature
from .base import (
    FENCE_RE,
    URL_RE,
    ProgressCb,
    WatchedSource,
    derive_title,
    epoch_ms_to_iso,
    estimate_metrics,
    friendly_repo,
    uri_to_path,
)


def _workspace_folders(descriptor_uri: Any) -> list[str]:
    """Resolve a multi-root workspace descriptor to its member folder paths.

    A multi-root window's ``workspace.json`` only points at a ``.code-workspace``
    descriptor; the actual repo folders live inside it under ``folders`` (each a
    ``{"path": …}`` or ``{"uri": …}``, possibly relative to the descriptor file).
    """
    wpath = uri_to_path(descriptor_uri)
    if not wpath:
        return []
    try:
        data = json.loads(Path(wpath).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    base = Path(wpath).parent
    out: list[str] = []
    for entry in data.get("folders") or []:
        if not isinstance(entry, dict):
            continue
        p = uri_to_path(entry.get("uri")) or entry.get("path")
        if not isinstance(p, str) or not p:
            continue
        if not p.startswith("/") and "://" not in p:  # relative to the descriptor
            p = str((base / p).resolve())
        out.append(p)
    return out


def load_workspace_map(
    roots: list[Path],
) -> dict[str, dict[str, Any]]:
    """Map each workspaceStorage id to its repository path/name.

    Single-folder windows store the repo directly (``folder``); multi-root windows
    store only a pointer to a ``.code-workspace`` descriptor, which is resolved to
    its member folders. ``folders`` lists every resolved repo so a caller can
    attribute a file to the right one; ``path``/``name`` are the primary (first).
    """
    mapping: dict[str, dict[str, Any]] = {}
    for root in roots:
        for wj in root.glob("*/workspace.json"):
            ws_id = wj.parent.name
            try:
                data = json.loads(wj.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            folder = data.get("folder")
            path = uri_to_path(folder) if folder else None
            folders = [path] if path else []
            if not folders and data.get("workspace"):
                folders = _workspace_folders(data["workspace"])
                path = folders[0] if folders else None
            mapping[ws_id] = {
                "path": path,
                "name": friendly_repo(path),
                "folders": [{"path": f, "name": friendly_repo(f)} for f in folders],
            }
    return mapping


def _part_text(val: Any) -> str:
    """Coerce a response part's ``value`` (string or {value: …}) to text."""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return str(val.get("value", ""))
    return ""


_MD_LINK_RE = re.compile(r"\[\]\(([^)]+)\)")


def _resolve_md_links(text: str, uris: dict[str, Any] | None) -> str:
    """Expand VS Code ``[](uri)`` placeholders using the part's ``uris`` map."""

    def _repl(match: re.Match[str]) -> str:
        uri = match.group(1)
        ref = (uris or {}).get(uri) if uris else None
        path = uri_to_path(ref if ref is not None else uri)
        if path:
            return f"`{Path(path).name}`"
        return uri

    return _MD_LINK_RE.sub(_repl, text)


def _message_part_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    val = msg.get("value")
    if not isinstance(val, str):
        return ""
    return _resolve_md_links(val, msg.get("uris"))


def _inline_ref_text(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    name = ref.get("name")
    if isinstance(name, str) and name.strip():
        return f"`{name.strip()}`"
    loc = ref.get("location")
    uri = loc.get("uri") if isinstance(loc, dict) else None
    path = uri_to_path(uri) if uri else uri_to_path(ref)
    if path:
        return f"`{Path(path).name}`"
    return ""


def _text_edit_text(part: dict[str, Any]) -> str:
    chunks: list[str] = []
    for edit_group in part.get("edits") or []:
        if not isinstance(edit_group, list):
            continue
        for edit in edit_group:
            if isinstance(edit, dict):
                text = edit.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)


def _fence_lang(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).suffix.lstrip(".").lower()


def _is_fence_only(text: str) -> bool:
    """True when a value part is only empty markdown code-fence markers."""
    return not text.replace("`", "").strip()


def _ref_path(ref: Any) -> str | None:
    if not isinstance(ref, dict):
        return None
    loc = ref.get("location")
    if isinstance(loc, dict):
        path = uri_to_path(loc.get("uri"))
        if path:
            return path
    return uri_to_path(ref)


def _strip_trailing_fence_opener(segments: list[str]) -> None:
    if not segments:
        return
    segments[-1] = re.sub(r"\n```\s*$", "", segments[-1])


def _tool_message_paths(part: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("pastTenseMessage", "invocationMessage"):
        msg = part.get(key)
        if not isinstance(msg, dict):
            continue
        for ref in (msg.get("uris") or {}).values():
            path = uri_to_path(ref)
            if path:
                paths.append(path)
    return paths


def _append_code_fence(segments: list[str], uri_path: str | None, content: str) -> None:
    body = content.rstrip()
    if not body:
        return
    lang = _fence_lang(uri_path)
    segments.append(f"\n```{lang}\n{body}\n```\n")


def _extract_response(parts: Any) -> dict[str, Any]:
    """Pull text, thinking, tools, touched files, urls and code blocks from parts."""
    text_segments: list[str] = []
    think_segments: list[str] = []
    tools: list[str] = []
    files: list[str] = []
    urls: list[str] = []

    if not isinstance(parts, list):
        return {"text": "", "thinking": "", "tools": [], "files": [], "urls": []}

    i = 0
    while i < len(parts):
        p = parts[i]
        i += 1
        if not isinstance(p, dict):
            if isinstance(p, str):
                text_segments.append(p)
            continue
        kind = p.get("kind")
        if kind is None and "value" in p:  # markdown string part
            val = p["value"]
            text = val if isinstance(val, str) else str(val.get("value", ""))
            text = _resolve_md_links(text, p.get("uris"))
            if not _is_fence_only(text):
                text_segments.append(text)
        elif kind == "thinking":  # model reasoning, kept for auditable records
            seg = _part_text(p.get("value") if "value" in p else p.get("text"))
            if seg.strip():
                think_segments.append(seg.strip())
        elif kind == "inlineReference":
            ref = p.get("inlineReference") or p.get("reference")
            label = _inline_ref_text(ref)
            if label:
                text_segments.append(label)
            path = _ref_path(ref)
            if path:
                files.append(path)
        elif kind == "codeblockUri":
            uri_path = uri_to_path(p.get("uri"))
            if uri_path:
                files.append(uri_path)
            nxt = parts[i] if i < len(parts) else None
            if isinstance(nxt, dict) and nxt.get("kind") == "textEditGroup":
                edit_path = uri_to_path(nxt.get("uri")) or uri_path
                if edit_path:
                    files.append(edit_path)
                content = _text_edit_text(nxt)
                if content.strip():
                    _strip_trailing_fence_opener(text_segments)
                    _append_code_fence(text_segments, edit_path or uri_path, content)
                else:
                    # VS Code emits empty edit groups as UI placeholders; drop
                    # any trailing fence opener from the preceding prose part.
                    _strip_trailing_fence_opener(text_segments)
                i += 1
        elif kind == "textEditGroup":
            uri_path = uri_to_path(p.get("uri"))
            if uri_path:
                files.append(uri_path)
            content = _text_edit_text(p)
            if content.strip():
                _strip_trailing_fence_opener(text_segments)
                _append_code_fence(text_segments, uri_path, content)
        elif kind == "toolInvocationSerialized":
            tool = p.get("toolId") or p.get("toolName")
            if isinstance(tool, str):
                tools.append(tool)
            files.extend(_tool_message_paths(p))
            msg = (
                _message_part_text(p.get("pastTenseMessage"))
                if p.get("isComplete")
                else _message_part_text(p.get("invocationMessage"))
            )
            if not msg.strip():
                msg = _message_part_text(
                    p.get("pastTenseMessage")
                ) or _message_part_text(p.get("invocationMessage"))
            if msg.strip():
                text_segments.append(f"\n{msg.strip()}\n")

    text = "".join(text_segments).strip()
    for m in URL_RE.findall(text):
        urls.append(m.rstrip(".,);"))
    return {
        "text": text,
        "thinking": "\n\n".join(think_segments).strip(),
        "tools": tools,
        "files": files,
        "urls": urls,
    }


def _content_references(req: dict[str, Any]) -> tuple[list[str], list[str]]:
    files: list[str] = []
    urls: list[str] = []
    for cr in req.get("contentReferences") or []:
        if not isinstance(cr, dict):
            continue
        ref = cr.get("reference") or cr.get("value")
        path = uri_to_path(ref)
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
        for lang, code in FENCE_RE.findall(assistant)
    ]

    files = list(dict.fromkeys(resp["files"] + cr_files))
    urls = list(dict.fromkeys(resp["urls"] + cr_urls))
    return {
        "turn_index": index,
        "user_message": (user_text or "").strip(),
        "assistant_response": assistant,
        "thinking": resp.get("thinking", ""),
        "tools": list(dict.fromkeys(resp["tools"])),
        "timestamp": epoch_ms_to_iso(req.get("timestamp")),
        "files": files,
        "urls": urls,
        "code_blocks": code_blocks,
    }


def _is_request(obj: Any) -> bool:
    """A turn object carries both a ``requestId`` and the user ``message``."""
    return isinstance(obj, dict) and "requestId" in obj and "message" in obj


def _add_request(
    req: dict[str, Any],
    order: list[str],
    by_id: dict[str, dict[str, Any]],
    anon: list[dict[str, Any]],
) -> str | None:
    """Register a request (turn), returning the id that subsequently streamed
    response parts should attach to. A re-emit keeps the richer response and
    refreshes the rest of the metadata.
    """
    rid = req.get("requestId")
    if not isinstance(rid, str):
        anon.append(dict(req))
        return None
    if rid not in by_id:
        stored = dict(req)
        stored["response"] = list(req.get("response") or [])
        by_id[rid] = stored
        order.append(rid)
    else:
        stored = by_id[rid]
        incoming = list(req.get("response") or [])
        if len(incoming) > len(stored.get("response") or []):
            stored["response"] = incoming
        for key, val in req.items():
            if key != "response":
                stored[key] = val
    return rid


def _reconstruct_jsonl(text: str) -> dict[str, Any] | None:
    """Rebuild a session object from VS Code's append-only JSONL chat log.

    Each line is ``{"kind": k, "v": ...}``. ``kind 0`` is a full snapshot (often
    written up front with an empty ``requests`` list). ``kind 2`` either appends
    finalized request objects (``requestId`` + ``message``) or streams a batch
    of response parts for the request currently in flight. ``kind 1`` carries
    positional scalar deltas we don't replay — the user prompt always lives on
    the request object and the assistant answer arrives as ``kind 2`` response
    batches, so both are recovered without tracking the writer's cursor.
    """
    base: dict[str, Any] | None = None
    order: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    anon: list[dict[str, Any]] = []
    current: str | None = None  # request currently receiving response parts
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        kind, v = evt.get("kind"), evt.get("v")
        if kind == 0 and isinstance(v, dict):
            if base is None:
                base = v
            for r in v.get("requests") or []:
                if _is_request(r):
                    _add_request(r, order, by_id, anon)
            current = None  # snapshot turns are already complete
        elif kind == 2 and isinstance(v, list) and v:
            if _is_request(v[0]):
                for r in v:
                    if _is_request(r):
                        current = _add_request(r, order, by_id, anon)
            elif current is not None and current in by_id:
                # A streamed batch of response parts for the in-flight request.
                by_id[current]["response"].extend(v)
    if base is None and not order:
        return None
    base = dict(base or {})
    base["requests"] = [by_id[i] for i in order] + anon
    if not base.get("lastMessageDate"):
        stamps = [
            r.get("timestamp")
            for r in base["requests"]
            if isinstance(r.get("timestamp"), (int, float))
        ]
        if stamps:
            base["lastMessageDate"] = max(stamps)
    return base


def _load_session_data(path: Path, raw: bytes) -> dict[str, Any] | None:
    """Load a session object from either the legacy whole-file JSON or the newer
    append-only JSONL event log."""
    text = raw.decode("utf-8", "replace")
    if path.suffix != ".jsonl":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "requests" in data and "kind" not in data:
            return data
    return _reconstruct_jsonl(text)


def _decode_session_id(name: str) -> str | None:
    """Decode a base64 memory-tool directory name back to its chat session id.

    VS Code names a session's memory directory with base64 of the session id. A
    directory name can't contain ``/``, so the URL-safe alphabet (``-``/``_``) is
    tried first, then standard. Best-effort: an undecodable name yields ``None``.
    """
    pad = "=" * (-len(name) % 4)
    for altchars in (b"-_", b"+/"):
        try:
            text = base64.b64decode(
                name + pad, altchars=altchars, validate=True
            ).decode("utf-8", "strict")
        except (ValueError, UnicodeDecodeError):
            continue
        text = text.strip()
        if text and text.isprintable():
            return text
    return None


def _read_memory_attachment(path: Path, root: Path) -> dict[str, Any] | None:
    """Snapshot a memory note as a viewable text attachment (content capped)."""
    from .. import attachments

    return attachments.inline_file(path, root=root)


def _session_memory_attachments(ws_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """The agent's memory-tool notes written for THIS chat session.

    Copilot's memory tool stores a conversation's working notes at
    ``…/GitHub.copilot-chat/memory-tool/memories/<base64(session-id)>/*.md`` beside
    the workspace's chats. Attaching them to the session that produced them keeps
    the notes where the conversation is read and — because they are written as part
    of this session — they survive its re-ingest. (Repository-scoped memory, which
    is cross-session, is indexed separately by the ``copilot_memory`` source.)
    """
    base = ws_dir / "GitHub.copilot-chat" / "memory-tool" / "memories"
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name == "repo":
            continue
        if _decode_session_id(d.name) != session_id:
            continue
        for md in sorted(d.rglob("*.md")):
            if md.is_file():
                att = _read_memory_attachment(md, d)
                if att:
                    out.append(att)
    return out


def parse_session(
    path: Path, wsmap: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    data = _load_session_data(path, raw)
    if not isinstance(data, dict):
        return None

    requests = data.get("requests") or []
    turns = [_parse_turn(r, i) for i, r in enumerate(requests) if isinstance(r, dict)]
    turns = [t for t in turns if t["user_message"] or t["assistant_response"]]
    if not turns:
        return None

    workspace_id: str | None = path.parent.parent.name
    if path.parent.name == "emptyWindowChatSessions":
        workspace_id = None  # started without a folder open  no repository
    repo = wsmap.get(workspace_id or "", {})
    session_id = data.get("sessionId") or path.stem
    attachments = _session_memory_attachments(path.parent.parent, session_id)

    return {
        "id": session_id,
        "source": "vscode",
        "title": derive_title(turns),
        "workspace_id": workspace_id,
        "repository": repo.get("name"),
        "repo_path": repo.get("path"),
        "requester": data.get("requesterUsername"),
        "responder": data.get("responderUsername"),
        "created_at": epoch_ms_to_iso(data.get("creationDate")),
        "updated_at": epoch_ms_to_iso(data.get("lastMessageDate"))
        or epoch_ms_to_iso(data.get("creationDate")),
        "source_path": str(path),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "turns": turns,
        "metrics": estimate_metrics(turns),
        "attachments": attachments,
    }


def iter_session_paths(roots: list[Path]) -> Iterable[Path]:
    seen_empty: set[Path] = set()
    for root in roots:
        # Workspace-scoped chats. Newer VS Code writes an append-only ``.jsonl``
        # event log; older builds wrote a single ``.json`` document — scan both.
        yield from root.glob("*/chatSessions/*.json")
        yield from root.glob("*/chatSessions/*.jsonl")
        # Chats started with no folder open live beside workspaceStorage.
        empty = root.parent / "globalStorage" / "emptyWindowChatSessions"
        if empty not in seen_empty and empty.is_dir():
            seen_empty.add(empty)
            yield from empty.glob("*.json")
            yield from empty.glob("*.jsonl")


class VSCodeSource(WatchedSource):
    key = "vscode"
    row_sources = ("vscode",)

    def default_config(self) -> config.SourceConfig:
        return config.SourceConfig(
            key=self.key,
            roots=config.vscode_storage_roots(),
            label="VS Code chat",
        )

    def fingerprint(self, cfg: config.SourceConfig) -> str:
        count = 0
        newest = 0
        for f in iter_session_paths(cfg.roots):
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
        cfg: config.SourceConfig,
        *,
        rebuild: bool,
        progress: ProgressCb | None = None,
    ) -> dict[str, int]:
        wsmap = load_workspace_map(cfg.roots)
        sigs = load_file_signatures(cur)
        added = updated = skipped = 0
        for path in iter_session_paths(cfg.roots):
            sp = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            sig = f"{st.st_mtime_ns}:{st.st_size}"
            # An unchanged file was already parsed and indexed on a prior run, so
            # skip re-reading and re-hashing it (the expensive part of a re-scan).
            if not rebuild and sigs.get(sp) == sig:
                skipped += 1
                continue
            session = parse_session(path, wsmap)
            record_file_signature(cur, sp, sig)  # remember this version either way
            if not session:
                continue
            session["source_adapter"] = self.key
            prior = existing.get(session["id"])
            if prior is not None and prior == session["content_hash"] and not rebuild:
                skipped += 1
                continue
            _write_session(cur, session)
            added += 1 if prior is None else 0
            updated += 0 if prior is None else 1
            if progress:
                progress(f"Indexed VS Code: {session['title']!r}")
        return {"added": added, "updated": updated, "skipped": skipped}
