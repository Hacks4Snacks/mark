"""Persistence boundary: turn a canonical session dict into database rows.

Every source adapter converges here. ``write_session`` replaces any prior copy
of the session (a ``DELETE`` cascades to child rows) and repopulates turns,
file/url references, code blocks, attachments, search chunks, summary and tags.
Keeping this in one place is what lets the search / embedding / UI layers stay
completely source-agnostic.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config, enrich

# NUL/C0 control characters (except tab/newline/carriage-return) and the U+FFFD
# replacement character — broken-UTF-8 noise that pollutes stored text and search.
_BAD_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]")


def _clean(text: str | None) -> str | None:
    """Strip NUL/control and U+FFFD noise from text bound for storage/search."""
    if not text:
        return text
    return _BAD_CHARS_RE.sub("", text)


def window_chunks(
    text: str, *, limit: int | None = None, overlap: int = 200
) -> list[str]:
    """Split text into overlapping windows bounded by ``limit`` characters.

    Shared by turn indexing here and document uploads so both chunk identically.
    """
    cap = limit or config.MAX_CHUNK_CHARS
    if len(text) <= cap:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + cap])
        start += cap - overlap
    return chunks


def write_session(cur, session: dict[str, Any]) -> None:
    sid = session["id"]
    # Manual topics the user added survive re-ingest (which replaces the row).
    manual_tags = cur.execute(
        "SELECT tag, score FROM tags WHERE session_id = ? AND manual = 1", (sid,)
    ).fetchall()
    # Replace any prior copy of this session (cascades to children).
    cur.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    cur.execute("DELETE FROM search_index WHERE session_id = ?", (sid,))

    turns = session["turns"]
    m = session.get("metrics") or {}
    cur.execute(
        """INSERT INTO sessions
           (id, source, title, workspace_id, repository, repo_path, requester,
            responder, created_at, updated_at, turn_count,
            duration_seconds, model, input_tokens, output_tokens,
            premium_requests, aiu, est_cost_usd, tokens_estimated,
            source_path, content_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sid,
            session["source"],
            session["title"],
            session["workspace_id"],
            session["repository"],
            session["repo_path"],
            session["requester"],
            session["responder"],
            session["created_at"],
            session["updated_at"],
            len(turns),
            m.get("duration_seconds"),
            m.get("model"),
            m.get("input_tokens"),
            m.get("output_tokens"),
            m.get("premium_requests"),
            m.get("aiu"),
            m.get("est_cost_usd"),
            m.get("tokens_estimated", 0),
            session["source_path"],
            session["content_hash"],
        ),
    )

    # User prompts carry the most search signal, so when a session exceeds the
    # per-session chunk cap we keep every turn's user text before spending the
    # remaining budget on assistant/tool output.
    user_pieces: list[tuple[int, str]] = []  # (turn_index, content)
    asst_pieces: list[tuple[int, str]] = []
    for t in turns:
        um = _clean(t["user_message"])
        ar = _clean(t["assistant_response"])
        # Reasoning/"thinking" is retained verbatim for auditable records but is
        # display-only: it is not chunked, so it never enters FTS or embeddings.
        thinking = _clean(t.get("thinking")) or None
        cur.execute(
            """INSERT INTO turns
               (session_id, turn_index, user_message, assistant_response, thinking, tools, timestamp)
               VALUES (?,?,?,?,?,?,?)""",
            (
                sid,
                t["turn_index"],
                um,
                ar,
                thinking,
                json.dumps(t["tools"]),
                t["timestamp"],
            ),
        )
        for f in t["files"]:
            cur.execute(
                "INSERT OR IGNORE INTO session_files(session_id, file_path, tool_name, turn_index) VALUES (?,?,?,?)",
                (sid, f, "edit" if f else None, t["turn_index"]),
            )
        for u in t["urls"]:
            cur.execute(
                "INSERT OR IGNORE INTO session_refs(session_id, ref_type, ref_value, turn_index) VALUES (?,?,?,?)",
                (sid, "url", u, t["turn_index"]),
            )
        for cb in t["code_blocks"]:
            cur.execute(
                "INSERT INTO code_blocks(session_id, turn_index, language, content) VALUES (?,?,?,?)",
                (sid, t["turn_index"], cb["language"], cb["content"]),
            )
        if um and um.strip():
            user_pieces.extend(
                (t["turn_index"], p) for p in window_chunks("User: " + um.strip())
            )
        if ar and ar.strip():
            asst_pieces.extend(
                (t["turn_index"], p) for p in window_chunks("Assistant: " + ar.strip())
            )

    # Every chunk is indexed for keyword (FTS) search — no per-session cap, so no
    # searchable text is dropped. User prompts are emitted before assistant/tool
    # output so that the per-session *embedding* cap (applied later, at embed time)
    # keeps the highest-signal chunks.
    chunk_rows: list[tuple[int, str]] = []  # (chunk_id, content)
    for turn_index, piece in user_pieces + asst_pieces:
        cur.execute(
            "INSERT INTO chunks(session_id, source_type, turn_index, content) VALUES (?,?,?,?)",
            (sid, "turn", turn_index, piece),
        )
        chunk_rows.append((cur.lastrowid, piece))

    # Session-level file references (e.g. from the Copilot CLI store).
    for path, tool, turn_index in session.get("extra_files", []):
        if path:
            cur.execute(
                "INSERT OR IGNORE INTO session_files(session_id, file_path, tool_name, turn_index) VALUES (?,?,?,?)",
                (sid, path, tool or "file", turn_index),
            )

    # Viewable snapshots of files the agent created/modified in this session.
    for att in session.get("attachments", []):
        cur.execute(
            """INSERT INTO documents
               (session_id, kind, filename, stored_path, mime, size_bytes, content)
               VALUES (?,?,?,?,?,?,?)""",
            (
                sid,
                "attachment",
                att.get("filename"),
                att.get("stored_path"),
                att.get("mime"),
                att.get("size_bytes"),
                att.get("content"),
            ),
        )

    summary, tags = enrich.enrich_session(session["title"], turns)
    if summary:
        cur.execute("UPDATE sessions SET summary = ? WHERE id = ?", (summary, sid))
    # Restore user topics first so they win over any auto tag of the same name.
    for mt in manual_tags:
        cur.execute(
            "INSERT OR IGNORE INTO tags(session_id, tag, score, manual) VALUES (?,?,?,1)",
            (sid, mt["tag"], mt["score"]),
        )
    for tag, score in tags:
        cur.execute(
            "INSERT OR IGNORE INTO tags(session_id, tag, score) VALUES (?,?,?)",
            (sid, tag, score),
        )

    tag_text = " ".join([mt["tag"] for mt in manual_tags] + [t for t, _ in tags])
    for chunk_id, content in chunk_rows:
        cur.execute(
            "INSERT INTO search_index(content, title, tags, chunk_id, session_id, source_type, turn_index) "
            "VALUES (?,?,?,?,?,?,?)",
            (content, session["title"], tag_text, chunk_id, sid, "turn", None),
        )
