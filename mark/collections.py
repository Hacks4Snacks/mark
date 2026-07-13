from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from . import config, search, visibility
from .repositories import collections as repo
from .schemas import CollectionRule


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class _ParsedRule:
    rule: dict[str, Any] | None
    error: str | None = None


@dataclass(frozen=True)
class _Resolution:
    ids: set[str]
    policy: dict[str, Any]
    rule: dict[str, Any] | None
    error: str | None = None


def _parse_rule(rule: Any) -> _ParsedRule:
    """Validate current or legacy JSON without crashing collection reads."""
    if not rule:
        return _ParsedRule(None)
    if isinstance(rule, dict):
        parsed = dict(rule)
    else:
        try:
            parsed = json.loads(rule)
        except (TypeError, ValueError) as exc:
            return _ParsedRule(None, f"invalid rule JSON: {exc}")
    if not isinstance(parsed, dict):
        return _ParsedRule(None, "collection rule must be an object")
    # Pre-validation releases allowed a client-owned result limit. Membership
    # policy is now server-owned; ignore that one known legacy field.
    parsed.pop("limit", None)
    try:
        model = CollectionRule.model_validate(parsed)
    except ValidationError as exc:
        detail = "; ".join(error["msg"] for error in exc.errors())
        return _ParsedRule(None, f"invalid collection rule: {detail}")
    normalized = model.model_dump(mode="json", exclude_none=True)
    return _ParsedRule(normalized if not _rule_is_empty(normalized) else None)


def _rule_is_empty(rule: dict[str, Any] | None) -> bool:
    if not rule:
        return True
    return not any(
        rule.get(k) for k in ("q", "repo", "source", "tags", "date_from", "date_to")
    )


def _rule_resolution(rule: dict[str, Any]) -> tuple[set[str], dict[str, Any]]:
    q = (rule.get("q") or "").strip()
    common: dict[str, Any] = {
        "repo": rule.get("repo") or None,
        "source": rule.get("source") or None,
        "tags": rule.get("tags") or None,
        "date_from": rule.get("date_from") or None,
        "date_to": rule.get("date_to") or None,
    }
    if not q:
        return search.scoped_session_ids(**common), {
            "kind": "complete",
            "cap": None,
            "truncated": False,
        }
    mode = rule.get("mode") or "hybrid"
    if mode == "keyword":
        return search.keyword_session_ids(q, **common), {
            "kind": "complete",
            "cap": None,
            "truncated": False,
        }
    ids, truncated = search.ranked_session_ids(
        q,
        mode=mode,
        limit=config.COLLECTION_RANKED_LIMIT,
        **common,
    )
    return set(ids), {
        "kind": "ranked",
        "cap": config.COLLECTION_RANKED_LIMIT,
        "truncated": truncated,
    }


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
    ids = _rule_resolution(rule)[0] if rule and not _rule_is_empty(rule) else set()
    ids |= includes
    ids -= excludes
    return ids


def _resolve(coll: dict[str, Any]) -> _Resolution:
    parsed = _parse_rule(coll.get("rule"))
    includes, excludes = _manual_members(coll["id"])
    if parsed.rule:
        ids, policy = _rule_resolution(parsed.rule)
    else:
        ids = set()
        policy = {
            "kind": "invalid" if parsed.error else "manual",
            "cap": None,
            "truncated": False,
        }
    ids |= includes
    ids -= excludes
    return _Resolution(
        ids=visibility.filter_visible(ids),
        policy=policy,
        rule=parsed.rule,
        error=parsed.error,
    )


def resolution(cid: str) -> _Resolution:
    row = repo.get_row(cid)
    if not row:
        return _Resolution(
            ids=set(),
            policy={"kind": "manual", "cap": None, "truncated": False},
            rule=None,
        )
    return _resolve(row)


def resolve_member_ids(coll: dict[str, Any]) -> set[str]:
    """Effective member ids: (rule | includes) - excludes.

    Hidden sessions and sessions from disabled sources are dropped so a
    collection never resurfaces data the user has hidden everywhere else.
    """
    return _resolve(coll).ids


def list_collections() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in repo.list_rows():
        resolved = _resolve(r)
        r["rule"] = resolved.rule
        r["rule_error"] = resolved.error
        r["membership_policy"] = resolved.policy
        r["pinned"] = bool(r.get("pinned"))
        r["count"] = len(resolved.ids)
        out.append(r)
    return out


def get_collection(cid: str) -> dict[str, Any] | None:
    c = repo.get_row(cid)
    if not c:
        return None
    parsed = _parse_rule(c.get("rule"))
    c["rule"] = parsed.rule
    c["rule_error"] = parsed.error
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
    parsed = _parse_rule(rule)
    if parsed.error:
        raise ValueError(parsed.error)
    repo.insert(
        cid,
        (name or "").strip() or "Untitled collection",
        description,
        icon,
        color,
        json.dumps(parsed.rule) if parsed.rule else None,
        1 if pinned else 0,
        _now(),
    )
    return cid


def update(cid: str, fields: dict[str, Any]) -> bool:
    prepared: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "rule":
            parsed = _parse_rule(value)
            if parsed.error:
                raise ValueError(parsed.error)
            value = json.dumps(parsed.rule) if parsed.rule else None
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
        rule = _parse_rule(row.get("rule")).rule
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


def _load_member_cards(
    ids: set[str], *, offset: int = 0, limit: int | None = None, sort: str = "recent"
) -> list[dict[str, Any]]:
    rows = repo.session_rows(list(ids), offset=offset, limit=limit, sort=sort)
    search.attach_tags(rows)
    for r in rows:
        r["score"] = None
        r["snippet"] = html.escape((r.get("summary") or "")[:240])
    return rows


def member_cards(
    ids: set[str], *, offset: int = 0, limit: int | None = None, sort: str = "recent"
) -> list[dict[str, Any]]:
    return _load_member_cards(ids, offset=offset, limit=limit, sort=sort)


def members_as_cards(cid: str) -> list[dict[str, Any]]:
    coll = repo.get_row(cid)
    if not coll:
        return []
    return _load_member_cards(_resolve(coll).ids)


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
    coll = repo.get_row(cid)
    if not coll:
        return _empty_overview()
    return overview_for_ids(_resolve(coll).ids)


def overview_for_ids(member_ids: set[str]) -> dict[str, Any]:
    ids = list(member_ids)
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
