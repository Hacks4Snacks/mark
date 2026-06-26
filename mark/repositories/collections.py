"""Collection persistence: CRUD, membership rows, and member aggregates.

Pure data-access — no rule evaluation or search. The :mod:`mark.collections`
service composes these with the search layer to resolve effective membership.
"""

from __future__ import annotations

from typing import Any

from .. import db

# Columns a PATCH may set; everything else in the payload is ignored.
_UPDATABLE = {"name", "description", "icon", "color", "rule", "pinned"}


def list_rows() -> list[dict[str, Any]]:
    with db.cursor() as cur:
        return [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM collections ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
        ]


def get_row(cid: str) -> dict[str, Any] | None:
    with db.cursor() as cur:
        row = cur.execute("SELECT * FROM collections WHERE id = ?", (cid,)).fetchone()
    return dict(row) if row else None


def insert(
    cid: str,
    name: str,
    description: str | None,
    icon: str | None,
    color: str | None,
    rule_json: str | None,
    pinned: int,
    now: str,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO collections(id, name, description, icon, color, rule, pinned, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, name, description, icon, color, rule_json, pinned, now, now),
        )


def update(cid: str, fields: dict[str, Any], now: str) -> bool:
    """Apply a whitelisted column update. ``fields`` values must already be in
    storage form (rule serialized to JSON, pinned as 0/1)."""
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in _UPDATABLE:
            continue
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return get_row(cid) is not None
    sets.append("updated_at = ?")
    params.append(now)
    params.append(cid)
    with db.cursor() as cur:
        cur.execute(f"UPDATE collections SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete(cid: str) -> bool:
    with db.cursor() as cur:
        cur.execute("DELETE FROM collections WHERE id = ?", (cid,))
        return cur.rowcount > 0


def member_states(cid: str) -> list[tuple[str, str]]:
    """``(session_id, state)`` rows for a collection's manual include/exclude set."""
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT session_id, state FROM collection_members WHERE collection_id = ?",
            (cid,),
        ).fetchall()
    return [(r["session_id"], r["state"]) for r in rows]


def set_member(cid: str, session_id: str, state: str, now: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_members(collection_id, session_id, state, added_at) "
            "VALUES (?,?,?,?) ON CONFLICT(collection_id, session_id) "
            "DO UPDATE SET state = excluded.state, added_at = excluded.added_at",
            (cid, session_id, state, now),
        )
        cur.execute("UPDATE collections SET updated_at = ? WHERE id = ?", (now, cid))


def delete_member(cid: str, session_id: str, now: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM collection_members WHERE collection_id = ? AND session_id = ?",
            (cid, session_id),
        )
        cur.execute("UPDATE collections SET updated_at = ? WHERE id = ?", (now, cid))


def session_rows(ids: list[str]) -> list[dict[str, Any]]:
    """Raw session rows for ``ids``, newest first (undated last)."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    with db.cursor() as cur:
        return [
            dict(r)
            for r in cur.execute(
                f"SELECT * FROM sessions WHERE id IN ({ph}) "
                "ORDER BY COALESCE(updated_at, created_at) IS NULL, "
                "COALESCE(updated_at, created_at) DESC",
                ids,
            ).fetchall()
        ]


def member_aggregates(ids: list[str]) -> dict[str, Any]:
    """Totals + per-source/topic/day breakdowns for a set of session ids.

    Caller guarantees ``ids`` is non-empty and shapes/rounds the result.
    """
    ph = ",".join("?" * len(ids))
    with db.cursor() as cur:
        totals = cur.execute(
            "SELECT COUNT(*) sessions, COALESCE(SUM(est_cost_usd),0) cost, "
            "COALESCE(SUM(premium_requests),0) premium, "
            "COALESCE(SUM(input_tokens),0) input_tokens, "
            "COALESCE(SUM(output_tokens),0) output_tokens, "
            "COALESCE(SUM(duration_seconds),0) duration, "
            "MIN(COALESCE(created_at, updated_at)) date_min, "
            "MAX(COALESCE(updated_at, created_at)) date_max "
            f"FROM sessions WHERE id IN ({ph})",
            ids,
        ).fetchone()
        files = cur.execute(
            f"SELECT COUNT(DISTINCT file_path) n FROM session_files "
            f"WHERE session_id IN ({ph})",
            ids,
        ).fetchone()["n"]
        by_source = cur.execute(
            f"SELECT source, COUNT(*) sessions FROM sessions WHERE id IN ({ph}) "
            "GROUP BY source ORDER BY sessions DESC",
            ids,
        ).fetchall()
        topics = cur.execute(
            f"SELECT tag, COUNT(*) n FROM tags WHERE session_id IN ({ph}) "
            "GROUP BY tag ORDER BY n DESC, tag LIMIT 12",
            ids,
        ).fetchall()
        by_day = cur.execute(
            "SELECT substr(COALESCE(updated_at, created_at),1,10) day, COUNT(*) sessions, "
            f"COALESCE(SUM(est_cost_usd),0) cost FROM sessions WHERE id IN ({ph}) "
            "AND COALESCE(updated_at, created_at) IS NOT NULL GROUP BY day ORDER BY day",
            ids,
        ).fetchall()
    return {
        "totals": dict(totals),
        "files": files,
        "by_source": [dict(r) for r in by_source],
        "topics": [dict(r) for r in topics],
        "by_day": [dict(r) for r in by_day],
    }
