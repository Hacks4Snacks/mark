"""Small session-scoped writes/lookups used by the API (tags, existence)."""

from __future__ import annotations

from .. import db


def exists(session_id: str) -> bool:
    with db.cursor() as cur:
        return (
            cur.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            is not None
        )


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
            (session_id, tag, 100.0),
        )
        _sync_fts_tags(cur, session_id)


def remove_tag(session_id: str, tag: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM tags WHERE session_id = ? AND tag = ?",
            (session_id, tag.strip().lower()),
        )
        _sync_fts_tags(cur, session_id)
