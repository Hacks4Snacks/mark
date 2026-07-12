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


def load_file_signatures(cur, prefix: str = "") -> dict[str, str]:
    """Cached change signatures keyed by source-file path.

    Lets a source's incremental ``ingest`` decide a file is unchanged from a
    cheap ``stat`` alone and skip re-reading/re-hashing it. Pass ``prefix`` to
    keep only synthetic keys for one source (e.g. ``"cli:"``); the table is small
    (one row per source file), so it is read in full and filtered in memory.
    """
    rows = cur.execute("SELECT path, signature FROM source_file_stat").fetchall()
    sigs = {r["path"]: r["signature"] for r in rows}
    if prefix:
        return {k: v for k, v in sigs.items() if k.startswith(prefix)}
    return sigs


def record_file_signature(cur, path: str, signature: str) -> None:
    """Remember a file's cheap change signature for the next incremental scan."""
    cur.execute(
        "INSERT INTO source_file_stat(path, signature) VALUES(?, ?) "
        "ON CONFLICT(path) DO UPDATE SET signature = excluded.signature",
        (path, signature),
    )


def write_session(cur, session: dict[str, Any]) -> None:
    sid = session["id"]
    # A permanently deleted session is tombstoned; honor it here — the single
    # chokepoint every source writes through — so a re-scan can't resurrect it.
    if cur.execute("SELECT 1 FROM tombstones WHERE session_id = ?", (sid,)).fetchone():
        return
    # Manual topics the user added survive re-ingest (which replaces the row).
    manual_tags = cur.execute(
        "SELECT tag, score FROM tags WHERE session_id = ? AND manual = 1", (sid,)
    ).fetchall()
    prior = cur.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone()
    # Re-indexing replaces the row, cascading away its chunks and their vectors.
    # For an actively-growing session that would re-embed identical text on every
    # pass, so snapshot existing vectors keyed by chunk content and carry them
    # onto the rebuilt chunks below; only genuinely new content then re-embeds.
    preserved_vectors: dict[str, tuple[str, int, bytes]] = {
        row["content"]: (row["model"], row["dim"], row["vector"])
        for row in cur.execute(
            "SELECT c.content, e.model, e.dim, e.vector "
            "FROM chunks c JOIN embeddings e ON e.chunk_id = c.id "
            "WHERE c.session_id = ?",
            (sid,),
        )
    }
    # Replace only ingestion-owned children. The parent row stays in place so
    # user-owned collection include/exclude rows never cascade away.
    if prior:
        for table in (
            "turns",
            "documents",
            "session_files",
            "session_refs",
            "code_blocks",
            "tags",
            "chunks",
        ):
            cur.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))
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
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET
                         source = excluded.source,
                         title = excluded.title,
                         summary = NULL,
                         workspace_id = excluded.workspace_id,
                         repository = excluded.repository,
                         repo_path = excluded.repo_path,
                         requester = excluded.requester,
                         responder = excluded.responder,
                         created_at = excluded.created_at,
                         updated_at = excluded.updated_at,
                         turn_count = excluded.turn_count,
                         duration_seconds = excluded.duration_seconds,
                         model = excluded.model,
                         input_tokens = excluded.input_tokens,
                         output_tokens = excluded.output_tokens,
                         premium_requests = excluded.premium_requests,
                         aiu = excluded.aiu,
                         est_cost_usd = excluded.est_cost_usd,
                         tokens_estimated = excluded.tokens_estimated,
                         source_path = excluded.source_path,
                         content_hash = excluded.content_hash,
                         indexed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
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
        chunk_id = cur.lastrowid
        chunk_rows.append((chunk_id, piece))
        # Carry an unchanged chunk's existing vector onto its rebuilt row so a
        # re-index doesn't pay to recompute an identical embedding.
        keep = preserved_vectors.get(piece)
        if keep is not None:
            cur.execute(
                "INSERT OR REPLACE INTO embeddings(chunk_id, session_id, model, dim, vector) "
                "VALUES (?,?,?,?,?)",
                (chunk_id, sid, keep[0], keep[1], keep[2]),
            )

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
               (session_id, kind, filename, stored_path, mime, size_bytes, content,
                storage_kind, sha256, capture_version)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                sid,
                "attachment",
                att.get("filename"),
                att.get("stored_path"),
                att.get("mime"),
                att.get("size_bytes"),
                att.get("content"),
                att.get("storage_kind"),
                att.get("sha256"),
                att.get("capture_version"),
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
