from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from ..persist import load_file_signatures, record_file_signature, write_session
from .base import FENCE_RE, URL_RE, ProgressCb, WatchedSource
from .vscode import load_workspace_map

# VS Code's Copilot chat "memory tool" persists the agent's durable notes as
# markdown under each workspace's storage:
#
#   workspaceStorage/<ws-id>/GitHub.copilot-chat/memory-tool/memories/
#       repo/<name>.md                 -> repository-scoped knowledge (cross-session)
#       <base64(session-id)>/<name>.md -> one conversation's working memory
#       <name>.md                      -> user-scoped notes (rare)
#
# The chat-session log only records that the ``copilot_memory`` tool was invoked,
# never the notes themselves, so this adapter indexes the repository- and
# user-scoped notes directly, each as its own searchable session attributed to
# the owning repository. Session-scoped notes are left to the VS Code source,
# which attaches them to the chat that produced them (read in context, and kept
# in sync as that session re-ingests).

#: ``<ws-id>/GitHub.copilot-chat/memory-tool/memories`` relative to a
#: workspaceStorage root.
_MEMORIES_GLOB = "*/GitHub.copilot-chat/memory-tool/memories"


def _iter_memories_dirs(roots: list[Path]) -> Iterable[Path]:
    for root in roots:
        try:
            matches = root.glob(_MEMORIES_GLOB)
        except OSError:
            continue
        for memories in matches:
            if memories.is_dir():
                yield memories


def iter_memory_files(roots: list[Path]) -> Iterable[tuple[Path, Path]]:
    """Yield ``(memories_dir, file)`` for every ``*.md`` memory note found."""
    for memories in _iter_memories_dirs(roots):
        for path in sorted(memories.rglob("*.md")):
            if path.is_file():
                yield memories, path


def _scope(memories: Path, path: Path) -> str:
    """Classify a memory file as ``repo`` / ``session`` / ``user`` scope.

    ``repo`` and ``user`` notes are indexed here as their own sessions; ``session``
    notes are skipped — the VS Code source attaches them to the chat that produced
    them, so they are read in context and survive that session's re-ingest.
    """
    parts = path.relative_to(memories).parts
    if len(parts) < 2:  # a bare file directly under memories/
        return "user"
    return "repo" if parts[0] == "repo" else "session"


def _match_folder(
    folders: list[dict[str, str | None]], stem: str
) -> dict[str, str | None]:
    """Pick the workspace folder that best owns a repo-memory file.

    Repo notes are named after their subject/repo, so in a multi-root workspace
    the folder sharing the longest name prefix with the file name is the right
    owner; fall back to the first folder when nothing meaningfully matches.
    """
    low = stem.lower()

    def prefix_len(name: str | None) -> int:
        i = 0
        for a, b in zip(low, (name or "").lower(), strict=False):
            if a != b:
                break
            i += 1
        return i

    best = max(folders, key=lambda f: prefix_len(f.get("name")))
    return best if prefix_len(best.get("name")) >= 3 else folders[0]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _timestamps(st: Any) -> tuple[str, str]:
    """(created, updated) ISO stamps; birth time when the platform reports it."""
    updated = _iso(st.st_mtime)
    birth = getattr(st, "st_birthtime", None)
    created = _iso(birth) if birth else updated
    return created, updated


def parse_memory_file(
    memories: Path, path: Path, wsmap: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        st = path.stat()
    except OSError:
        return None
    text = raw.decode("utf-8", "replace").strip()

    scope = _scope(memories, path)
    # memories -> memory-tool -> GitHub.copilot-chat -> <ws-id>
    ws_id = memories.parents[2].name if len(memories.parents) >= 3 else None
    info = wsmap.get(ws_id or "", {})
    stem = path.stem
    repo_name = info.get("name")
    repo_path = info.get("path")

    if scope == "repo":
        # A repo note in a multi-root workspace belongs to one of its folders.
        folders = info.get("folders") or []
        if len(folders) > 1:
            best = _match_folder(folders, stem)
            repo_name, repo_path = best.get("name"), best.get("path")
        title = f"Repo memory · {stem}"
        context = f"Repository memory ({repo_name or 'workspace'})"
    else:  # user scope (session scope is attached to its chat by the VS Code source)
        title = f"Memory · {stem}"
        context = "User memory"

    created, updated = _timestamps(st)
    code_blocks = [
        {"language": (lang or "").strip() or None, "content": code.strip()}
        for lang, code in FENCE_RE.findall(text)
    ]
    urls = list(dict.fromkeys(u.rstrip(".,);") for u in URL_RE.findall(text)))
    turn = {
        "turn_index": 0,
        "user_message": f"{context}: {path.name}",
        "assistant_response": text,
        "thinking": "",
        "tools": [],
        "timestamp": updated,
        "files": [],
        "urls": urls,
        "code_blocks": code_blocks,
    }
    # Memory notes are knowledge artifacts, not LLM turns: report zero usage so
    # they never inflate the cost/token dashboards, while staying fully indexed.
    metrics = {
        "duration_seconds": None,
        "model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "premium_requests": None,
        "aiu": None,
        "est_cost_usd": 0.0,
        "tokens_estimated": 0,
    }
    return {
        "id": f"copilot-memory-{hashlib.sha256(str(path).encode()).hexdigest()[:16]}",
        "source": "copilot_memory",
        "title": title,
        "workspace_id": ws_id,
        "repository": repo_name,
        "repo_path": repo_path,
        "requester": None,
        "responder": "GitHub Copilot",
        "created_at": created,
        "updated_at": updated,
        "source_path": str(path),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "turns": [turn],
        "metrics": metrics,
    }


class CopilotMemorySource(WatchedSource):
    key = "copilot_memory"
    row_sources = ("copilot_memory",)

    def default_config(self) -> config.SourceConfig:
        return config.SourceConfig(
            key=self.key,
            roots=config.vscode_storage_roots(),
            label="Copilot memory",
        )

    def fingerprint(self, cfg: config.SourceConfig) -> str:
        count = 0
        newest = 0
        for memories, path in iter_memory_files(cfg.roots):
            if _scope(memories, path) == "session":
                continue  # attached to its chat by the VS Code source
            try:
                st = path.stat()
            except OSError:
                continue
            count += 1
            if st.st_mtime_ns > newest:
                newest = st.st_mtime_ns
        return f"mem:{count}:{newest}"

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
        for memories, path in iter_memory_files(cfg.roots):
            if _scope(memories, path) == "session":
                continue  # attached to its chat by the VS Code source
            sp = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            sig = f"{st.st_mtime_ns}:{st.st_size}"
            # An unchanged file was parsed and indexed on a prior run — skip the
            # re-read/re-hash from its cheap stat signature alone.
            if not rebuild and sigs.get(sp) == sig:
                skipped += 1
                continue
            session = parse_memory_file(memories, path, wsmap)
            record_file_signature(cur, sp, sig)
            if not session:
                continue
            prior = existing.get(session["id"])
            if prior is not None and prior == session["content_hash"] and not rebuild:
                skipped += 1
                continue
            write_session(cur, session)
            added += 1 if prior is None else 0
            updated += 0 if prior is None else 1
            if progress:
                progress(f"Indexed memory: {session['title']!r}")
        return {"added": added, "updated": updated, "skipped": skipped}
