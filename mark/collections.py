"""Collections service: rule evaluation + effective membership math.

Persistence lives in :mod:`mark.repositories.collections`; this module composes
those CRUD/query functions with the search layer to resolve a collection's
effective members — ``(rule | manual includes) - manual excludes`` — and to
shape the cards/overview the API returns.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from . import search
from .repositories import collections as repo


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
        rule.get(k) for k in ("q", "repo", "source", "tags", "date_from", "date_to")
    )


def _rule_session_ids(rule: dict[str, Any]) -> list[str]:
    q = (rule.get("q") or "").strip()
    common: dict[str, Any] = {
        "repo": rule.get("repo") or None,
        "source": rule.get("source") or None,
        "tags": rule.get("tags") or None,
        "date_from": rule.get("date_from") or None,
        "date_to": rule.get("date_to") or None,
        "sort": rule.get("sort") or "recent",
        "limit": int(rule.get("limit") or 500),
    }
    if q:
        results = search.search(q, mode=rule.get("mode") or "hybrid", **common)
    else:
        results = search.browse(**common)
    return [r["id"] for r in results]


def _manual_members(cid: str) -> tuple[set[str], set[str]]:
    includes: set[str] = set()
    excludes: set[str] = set()
    for sid, state in repo.member_states(cid):
        (excludes if state == "exclude" else includes).add(sid)
    return includes, excludes


def _resolve_ids(
    rule: dict[str, Any] | None, includes: set[str], excludes: set[str]
) -> set[str]:
    """Effective ids from a parsed rule plus manual include/exclude sets."""
    ids = set(_rule_session_ids(rule)) if rule and not _rule_is_empty(rule) else set()
    ids |= includes
    ids -= excludes
    return ids


def resolve_member_ids(coll: dict[str, Any]) -> set[str]:
    """Effective member ids: (rule | includes) - excludes."""
    rule = _parse_rule(coll.get("rule"))
    includes, excludes = _manual_members(coll["id"])
    return _resolve_ids(rule, includes, excludes)


def list_collections() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in repo.list_rows():
        r["rule"] = _parse_rule(r.get("rule"))
        r["pinned"] = bool(r.get("pinned"))
        r["count"] = len(resolve_member_ids(r))
        out.append(r)
    return out


def get_collection(cid: str) -> dict[str, Any] | None:
    c = repo.get_row(cid)
    if not c:
        return None
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
    repo.insert(
        cid,
        (name or "").strip() or "Untitled collection",
        description,
        icon,
        color,
        json.dumps(rule) if rule else None,
        1 if pinned else 0,
        _now(),
    )
    return cid


def update(cid: str, fields: dict[str, Any]) -> bool:
    prepared: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "rule":
            value = json.dumps(value) if value else None
        elif key == "pinned":
            value = 1 if value else 0
        prepared[key] = value
    return repo.update(cid, prepared, _now())


def delete(cid: str) -> bool:
    return repo.delete(cid)


def set_member(cid: str, session_id: str, state: str = "include") -> None:
    state = "exclude" if state == "exclude" else "include"
    repo.set_member(cid, session_id, state, _now())


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
    repo.delete_member(cid, session_id, _now())


def collections_for_session(session_id: str) -> list[dict[str, Any]]:
    """Which collections currently *contain* this session (rule or manual).

    Resolves each collection's rule + manual sets exactly once per collection.
    """
    out: list[dict[str, Any]] = []
    for row in repo.list_rows():
        cid = row["id"]
        rule = _parse_rule(row.get("rule"))
        includes, excludes = _manual_members(cid)
        member_ids = _resolve_ids(rule, includes, excludes)
        out.append(
            {
                "id": cid,
                "name": row["name"],
                "icon": row.get("icon"),
                "color": row.get("color"),
                "member": session_id in member_ids,
                "manual_include": session_id in includes,
                "manual_exclude": session_id in excludes,
            }
        )
    return out


def _load_member_cards(ids: set[str]) -> list[dict[str, Any]]:
    rows = repo.session_rows(list(ids))
    search.attach_tags(rows)
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
    agg = repo.member_aggregates(ids)
    t = agg["totals"]
    return {
        "totals": {
            "sessions": t["sessions"],
            "cost": round(t["cost"], 2),
            "premium": int(t["premium"]),
            "input_tokens": int(t["input_tokens"]),
            "output_tokens": int(t["output_tokens"]),
            "duration": t["duration"] or 0,
            "files": int(agg["files"] or 0),
        },
        "date_min": t["date_min"],
        "date_max": t["date_max"],
        "by_source": [
            {"source": r["source"], "sessions": r["sessions"]} for r in agg["by_source"]
        ],
        "topics": [{"tag": r["tag"], "count": r["n"]} for r in agg["topics"]],
        "by_day": [
            {"day": r["day"], "sessions": r["sessions"], "cost": round(r["cost"], 4)}
            for r in agg["by_day"]
        ],
    }
