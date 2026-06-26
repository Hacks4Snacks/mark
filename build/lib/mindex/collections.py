"""Collections — auto-populated, manually-tunable groups of sessions.

A collection's effective membership is::

    (rule matches) ∪ (manual includes) − (manual excludes)

The *rule* is a saved search (the same parameters the ``/api/search`` endpoint
takes), so a collection keeps picking up newly indexed sessions on its own.
Manual edits are stored as ``include``/``exclude`` rows in
``collection_members``; recording a removal as an explicit ``exclude`` is what
makes the manual tweak survive the next auto-sync — otherwise a re-indexed
session that still matches the rule would silently reappear.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from . import db, search

_UPDATABLE = {"name", "description", "icon", "color", "rule", "pinned"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_rule(rule: Any) -> dict[str, Any] | None:
    """Accept a JSON string, an already-parsed dict, or None."""
    if not rule:
        return None
    if isinstance(rule, dict):
        return rule or None
    try:
        parsed = json.loads(rule)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _rule_is_empty(rule: dict[str, Any] | None) -> bool:
    if not rule:
        return True
    return not any(
        rule.get(k)
        for k in ("q", "repo", "source", "tags", "date_from", "date_to")
    ) and not rule.get("include_automation")


# --- membership resolution ---------------------------------------------------


def _rule_session_ids(rule: dict[str, Any]) -> list[str]:
    q = (rule.get("q") or "").strip()
    common: dict[str, Any] = dict(
        repo=rule.get("repo") or None,
        source=rule.get("source") or None,
        tags=rule.get("tags") or None,
        date_from=rule.get("date_from") or None,
        date_to=rule.get("date_to") or None,
        include_automation=bool(rule.get("include_automation")),
        sort=rule.get("sort") or "recent",
        limit=int(rule.get("limit") or 500),
    )
    if q:
        results = search.search(q, mode=rule.get("mode") or "hybrid", **common)
    else:
        results = search.browse(**common)
    return [r["id"] for r in results]


def _manual_members(cid: str) -> tuple[set[str], set[str]]:
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT session_id, state FROM collection_members WHERE collection_id = ?",
            (cid,),
        ).fetchall()
    includes = {r["session_id"] for r in rows if r["state"] == "include"}
    excludes = {r["session_id"] for r in rows if r["state"] == "exclude"}
    return includes, excludes


def resolve_member_ids(coll: dict[str, Any]) -> set[str]:
    """Effective member ids: (rule ∪ includes) − excludes."""
    rule = _parse_rule(coll.get("rule"))
    ids: set[str] = set(_rule_session_ids(rule)) if rule and not _rule_is_empty(rule) else set()
    includes, excludes = _manual_members(coll["id"])
    ids |= includes
    ids -= excludes
    return ids


# --- CRUD --------------------------------------------------------------------


def list_collections() -> list[dict[str, Any]]:
    with db.cursor() as cur:
        rows = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM collections ORDER BY pinned DESC, updated_at DESC"
            ).fetchall()
        ]
    out: list[dict[str, Any]] = []
    for r in rows:
        r["rule"] = _parse_rule(r.get("rule"))
        r["pinned"] = bool(r.get("pinned"))
        r["count"] = len(resolve_member_ids(r))
        out.append(r)
    return out


def get_collection(cid: str) -> dict[str, Any] | None:
    with db.cursor() as cur:
        row = cur.execute("SELECT * FROM collections WHERE id = ?", (cid,)).fetchone()
    if not row:
        return None
    c = dict(row)
    c["rule"] = _parse_rule(c.get("rule"))
    c["pinned"] = bool(c.get("pinned"))
    return c


def create(
    name: str,
    description: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    rule: dict[str, Any] | None = None,
    pinned: bool = False,
) -> str:
    cid = uuid4().hex
    now = _now()
    rule_json = json.dumps(rule) if rule else None
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO collections(id, name, description, icon, color, rule, pinned, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                cid,
                (name or "").strip() or "Untitled collection",
                description,
                icon,
                color,
                rule_json,
                1 if pinned else 0,
                now,
                now,
            ),
        )
    return cid


def update(cid: str, fields: dict[str, Any]) -> bool:
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in _UPDATABLE:
            continue
        if key == "rule":
            value = json.dumps(value) if value else None
        elif key == "pinned":
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return get_collection(cid) is not None
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(cid)
    with db.cursor() as cur:
        cur.execute(f"UPDATE collections SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def delete(cid: str) -> bool:
    with db.cursor() as cur:
        cur.execute("DELETE FROM collections WHERE id = ?", (cid,))
        return cur.rowcount > 0


# --- membership edits --------------------------------------------------------


def set_member(cid: str, session_id: str, state: str = "include") -> None:
    state = "exclude" if state == "exclude" else "include"
    now = _now()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_members(collection_id, session_id, state, added_at) "
            "VALUES (?,?,?,?) ON CONFLICT(collection_id, session_id) "
            "DO UPDATE SET state = excluded.state, added_at = excluded.added_at",
            (cid, session_id, state, now),
        )
        cur.execute("UPDATE collections SET updated_at = ? WHERE id = ?", (now, cid))


def remove_member(cid: str, session_id: str) -> None:
    """Remove a session from a collection.

    With a rule, record an ``exclude`` so the removal sticks across re-syncs;
    for a manual-only collection just drop the membership row.
    """
    coll = get_collection(cid)
    has_rule = bool(coll and not _rule_is_empty(coll.get("rule")))
    if has_rule:
        set_member(cid, session_id, "exclude")
        return
    now = _now()
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM collection_members WHERE collection_id = ? AND session_id = ?",
            (cid, session_id),
        )
        cur.execute("UPDATE collections SET updated_at = ? WHERE id = ?", (now, cid))


def collections_for_session(session_id: str) -> list[dict[str, Any]]:
    """Which collections currently *contain* this session (rule or manual)."""
    out: list[dict[str, Any]] = []
    for c in list_collections():
        member = session_id in resolve_member_ids(c)
        includes, excludes = _manual_members(c["id"])
        out.append(
            {
                "id": c["id"],
                "name": c["name"],
                "icon": c.get("icon"),
                "color": c.get("color"),
                "member": member,
                "manual_include": session_id in includes,
                "manual_exclude": session_id in excludes,
            }
        )
    return out


# --- rendering & rollups -----------------------------------------------------


def _load_member_cards(ids: set[str]) -> list[dict[str, Any]]:
    id_list = list(ids)
    if not id_list:
        return []
    placeholders = ",".join("?" * len(id_list))
    with db.cursor() as cur:
        rows = [
            dict(r)
            for r in cur.execute(
                f"SELECT * FROM sessions WHERE id IN ({placeholders}) "
                "ORDER BY COALESCE(updated_at, created_at) IS NULL, "
                "COALESCE(updated_at, created_at) DESC",
                id_list,
            ).fetchall()
        ]
    search._attach_tags(rows)
    for r in rows:
        r["score"] = None
        r["snippet"] = html.escape((r.get("summary") or "")[:240])
    return rows


def members_as_cards(cid: str) -> list[dict[str, Any]]:
    coll = get_collection(cid)
    if not coll:
        return []
    return _load_member_cards(resolve_member_ids(coll))


def _empty_overview() -> dict[str, Any]:
    return {
        "totals": {
            "sessions": 0,
            "cost": 0.0,
            "premium": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration": 0,
            "files": 0,
        },
        "date_min": None,
        "date_max": None,
        "by_source": [],
        "topics": [],
        "by_day": [],
    }


def overview(cid: str) -> dict[str, Any]:
    coll = get_collection(cid)
    if not coll:
        return _empty_overview()
    ids = list(resolve_member_ids(coll))
    if not ids:
        return _empty_overview()
    ph = ",".join("?" * len(ids))
    with db.cursor() as cur:
        t = cur.execute(
            "SELECT COUNT(*) sessions, COALESCE(SUM(est_cost_usd),0) cost, "
            "COALESCE(SUM(premium_requests),0) premium, COALESCE(SUM(input_tokens),0) input_tokens, "
            "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(duration_seconds),0) duration, "
            "MIN(COALESCE(created_at, updated_at)) date_min, MAX(COALESCE(updated_at, created_at)) date_max "
            f"FROM sessions WHERE id IN ({ph})",
            ids,
        ).fetchone()
        files = cur.execute(
            f"SELECT COUNT(DISTINCT file_path) n FROM session_files WHERE session_id IN ({ph})",
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
        "totals": {
            "sessions": t["sessions"],
            "cost": round(t["cost"], 2),
            "premium": int(t["premium"]),
            "input_tokens": int(t["input_tokens"]),
            "output_tokens": int(t["output_tokens"]),
            "duration": t["duration"] or 0,
            "files": int(files or 0),
        },
        "date_min": t["date_min"],
        "date_max": t["date_max"],
        "by_source": [
            {"source": r["source"], "sessions": r["sessions"]} for r in by_source
        ],
        "topics": [{"tag": r["tag"], "count": r["n"]} for r in topics],
        "by_day": [
            {"day": r["day"], "sessions": r["sessions"], "cost": round(r["cost"], 4)}
            for r in by_day
        ],
    }
