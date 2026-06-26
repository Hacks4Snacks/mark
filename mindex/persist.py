"""Persistence boundary: turn a canonical session dict into database rows.

Every source adapter converges here. ``write_session`` replaces any prior copy
of the session (a ``DELETE`` cascades to child rows) and repopulates turns,
file/url references, code blocks, attachments, search chunks, summary and tags.
Keeping this in one place is what lets the search / embedding / UI layers stay
completely source-agnostic.
"""

from __future__ import annotations

import json
from typing import Any

from . import config, enrich


def _chunk_turn(turn: dict[str, Any]) -> list[str]:
    parts = []
    if turn["user_message"]:
        parts.append("User: " + turn["user_message"])
    if turn["assistant_response"]:
        parts.append("Assistant: " + turn["assistant_response"])
    text = "\n\n".join(parts)
    if not text:
        return []
    limit = config.MAX_CHUNK_CHARS
    if len(text) <= limit:
        return [text]
    chunks, start, overlap = [], 0, 200
    while start < len(text):
        chunks.append(text[start : start + limit])
        start += limit - overlap
    return chunks


def write_session(cur, session: dict[str, Any], *, light: bool = True) -> None:
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

    chunk_rows: list[tuple[int, str]] = []  # (chunk_id, content)
    for t in turns:
        cur.execute(
            """INSERT INTO turns
               (session_id, turn_index, user_message, assistant_response, tools, timestamp)
               VALUES (?,?,?,?,?,?)""",
            (
                sid,
                t["turn_index"],
                t["user_message"],
                t["assistant_response"],
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
        for piece in _chunk_turn(t):
            if len(chunk_rows) >= config.MAX_CHUNKS_PER_SESSION:
                break
            cur.execute(
                "INSERT INTO chunks(session_id, source_type, turn_index, content) VALUES (?,?,?,?)",
                (sid, "turn", t["turn_index"], piece),
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

    summary, tags = enrich.enrich_session(session["title"], turns, light=light)
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

    tag_text = " ".join(t for t, _ in tags)
    for chunk_id, content in chunk_rows:
        cur.execute(
            "INSERT INTO search_index(content, title, tags, chunk_id, session_id, source_type, turn_index) "
            "VALUES (?,?,?,?,?,?,?)",
            (content, session["title"], tag_text, chunk_id, sid, "turn", None),
        )
