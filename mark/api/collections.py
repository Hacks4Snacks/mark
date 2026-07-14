from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import ask, config
from .. import collections as collections_svc
from ..repositories import sessions as sessions_repo
from ..schemas import (
    CollAskIn,
    CollectionIn,
    CollectionPatch,
    MemberIn,
    OkCountResponse,
    OkResponse,
)

router = APIRouter()


@router.get("/api/collections")
def api_collections() -> list[dict[str, Any]]:
    return collections_svc.list_collections()


@router.post("/api/collections")
def api_create_collection(body: CollectionIn) -> dict[str, Any]:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    rule = body.rule.model_dump(mode="json", exclude_none=True) if body.rule else None
    cid = collections_svc.create(
        name, body.description, body.icon, body.color, rule, body.pinned
    )
    created = collections_svc.get_collection(cid)
    assert created is not None
    return created


@router.get("/api/collections/{cid}")
def api_collection(
    cid: str,
    members_offset: int = 0,
    members_limit: int = 100,
    members_sort: Literal["recent", "oldest", "turns", "title"] | None = None,
    include_overview: bool = True,
) -> dict[str, Any]:
    coll = collections_svc.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    resolved = collections_svc.resolution(cid)
    members_offset = max(0, members_offset)
    members_limit = max(1, min(members_limit, 200))
    member_sort = members_sort or (resolved.rule or {}).get("sort") or "recent"
    members = collections_svc.member_cards(
        resolved.ids,
        offset=members_offset,
        limit=members_limit,
        sort=member_sort,
    )
    coll["membership_policy"] = resolved.policy
    coll["rule_error"] = resolved.error
    coll["members"] = members
    coll["members_offset"] = members_offset
    coll["members_limit"] = members_limit
    coll["members_sort"] = member_sort
    coll["has_more_members"] = members_offset + len(members) < len(resolved.ids)
    coll["overview"] = (
        collections_svc.overview_for_ids(resolved.ids) if include_overview else None
    )
    coll["count"] = len(resolved.ids)
    return coll


@router.patch("/api/collections/{cid}")
def api_update_collection(cid: str, body: CollectionPatch) -> dict[str, Any]:
    if not collections_svc.get_collection(cid):
        raise HTTPException(status_code=404, detail="collection not found")
    fields = body.model_dump(exclude_unset=True)
    if body.rule is not None:
        fields["rule"] = body.rule.model_dump(mode="json", exclude_none=True)
    if "name" in fields and not (fields["name"] or "").strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    collections_svc.update(cid, fields)
    updated = collections_svc.get_collection(cid)
    assert updated is not None
    return updated


@router.delete("/api/collections/{cid}", response_model=OkResponse)
def api_delete_collection(cid: str) -> dict[str, Any]:
    if not collections_svc.delete(cid):
        raise HTTPException(status_code=404, detail="collection not found")
    return {"ok": True}


@router.post("/api/collections/{cid}/members", response_model=OkCountResponse)
def api_add_member(cid: str, body: MemberIn) -> dict[str, Any]:
    coll = collections_svc.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    if not sessions_repo.exists(body.session_id):
        raise HTTPException(status_code=404, detail="session not found")
    collections_svc.set_member(cid, body.session_id, body.state)
    return {
        "ok": True,
        "count": len(collections_svc.resolve_member_ids(coll)),
    }


@router.delete(
    "/api/collections/{cid}/members/{session_id}", response_model=OkCountResponse
)
def api_remove_member(cid: str, session_id: str) -> dict[str, Any]:
    coll = collections_svc.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    collections_svc.remove_member(cid, session_id)
    return {"ok": True, "count": len(collections_svc.resolve_member_ids(coll))}


@router.get("/api/sessions/{session_id}/collections")
def api_session_collections(session_id: str) -> list[dict[str, Any]]:
    return collections_svc.collections_for_session(session_id)


@router.post("/api/collections/{cid}/ask")
def api_collection_ask(cid: str, body: CollAskIn) -> StreamingResponse:
    if not config.ENABLE_ASK:
        raise HTTPException(status_code=404, detail="ask is disabled")
    coll = collections_svc.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="empty question")
    limit = max(1, body.limit) if body.limit else None
    member_ids = collections_svc.resolve_member_ids(coll)

    def gen():
        for event in ask.stream_answer(question, limit=limit, session_ids=member_ids):
            yield "data: " + json.dumps(event) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
