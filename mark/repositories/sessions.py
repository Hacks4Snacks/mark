from __future__ import annotations

from .. import db

# Manual topics are stored normalized (lowercase, collapsed whitespace, capped)
# so add/remove always agree regardless of caller.
_MANUAL_TAG_SCORE = 100.0


def _norm_tag(tag: str) -> str:
    return " ".join((tag or "").strip().lower().split())[:40]


def exists(session_id: str) -> bool:
    with db.cursor() as cur:
        return (
            cur.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
            is not None
        )


def set_hidden(session_id: str, hidden: bool) -> bool:
    """Hide or unhide a session; returns ``False`` if no such session exists.

    Hiding is non-destructive: the row stays indexed so a re-scan can't fight
    it, but it is filtered from listings and aggregates until unhidden.
    """
    with db.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET hidden = ? WHERE id = ?",
            (1 if hidden else 0, session_id),
        )
        return cur.rowcount > 0


def purge(session_id: str) -> bool:
    """Permanently delete a session and tombstone its id; ``False`` if unknown.

    Unlike hiding, this reclaims the row and (via cascade) all its children. The
    tombstone is the one thing kept, deliberately, so a background re-scan can't
    silently re-import what the user chose to delete. This cannot be undone.
    """
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT source, content_hash FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return False
        cur.execute(
            "INSERT INTO tombstones(session_id, source, content_hash) VALUES (?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET source = excluded.source, "
            "content_hash = excluded.content_hash, "
            "deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (session_id, row["source"], row["content_hash"]),
        )
        # FTS rows have no foreign key, so drop them explicitly; the session
        # delete cascades to turns/chunks/embeddings/tags/members.
        cur.execute("DELETE FROM search_index WHERE session_id = ?", (session_id,))
        cur.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return True


def get_attachment(session_id: str, doc_id: int) -> dict | None:
    """Fetch one attachment document (scoped to its session) for download."""
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT id, filename, stored_path, mime, size_bytes, content, "
            "storage_kind, sha256, capture_version "
            "FROM documents WHERE id = ? AND session_id = ? AND kind = 'attachment'",
            (doc_id, session_id),
        ).fetchone()
    return dict(row) if row else None


def _sync_fts_tags(cur, session_id: str) -> None:
    """Refresh a session's FTS ``tags`` column so manual topics stay searchable."""
    tag_text = " ".join(
        r["tag"]
        for r in cur.execute("SELECT tag FROM tags WHERE session_id = ?", (session_id,))
    )
    cur.execute(
        "UPDATE search_index SET tags = ? WHERE session_id = ?", (tag_text, session_id)
    )


def add_tag(session_id: str, tag: str) -> None:
    """Add (or re-flag as manual) a user topic and resync the FTS tags column."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO tags(session_id, tag, score, manual) VALUES (?,?,?,1) "
            "ON CONFLICT(session_id, tag) DO UPDATE SET manual = 1",
            (session_id, _norm_tag(tag), _MANUAL_TAG_SCORE),
        )
        _sync_fts_tags(cur, session_id)


def remove_tag(session_id: str, tag: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM tags WHERE session_id = ? AND tag = ?",
            (session_id, _norm_tag(tag)),
        )
        _sync_fts_tags(cur, session_id)
