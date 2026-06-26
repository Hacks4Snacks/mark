"""Single-session endpoints: detail, related, export, and manual topics."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from .. import exporting, render, search
from ..repositories import sessions as sessions_repo
from ..schemas import OkResponse, TagIn, TagResponse

router = APIRouter()


@router.get("/api/sessions/{session_id}")
def api_session(session_id: str) -> dict[str, Any]:
    session = search.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    for turn in session["turns"]:
        turn["user_html"] = render.render_markdown(turn.get("user_message"))
        turn["assistant_html"] = render.render_markdown(turn.get("assistant_response"))
        try:
            turn["tools"] = json.loads(turn.get("tools") or "[]")
        except (TypeError, json.JSONDecodeError):
            turn["tools"] = []
    if session.get("document") and session["document"].get("content"):
        session["document"]["html"] = render.render_markdown(
            session["document"]["content"]
        )
    for att in session.get("attachments") or []:
        content = att.get("content")
        if not content:
            continue
        name = (att.get("filename") or "").lower()
        if name.endswith((".md", ".markdown")):
            att["html"] = render.render_markdown(content)
        else:
            lang = name.rsplit(".", 1)[-1] if "." in name else ""
            att["html"] = render.render_markdown(f"```{lang}\n{content}\n```")
    return session


@router.get("/api/sessions/{session_id}/related")
def api_related(session_id: str) -> list[dict[str, Any]]:
    return search.related_sessions(session_id)


@router.get("/api/sessions/{session_id}/export.md")
def api_export_markdown(session_id: str) -> PlainTextResponse:
    session = search.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    body = exporting.session_to_markdown(session)
    fname = exporting.slug(session.get("title") or "", session_id) + ".md"
    return PlainTextResponse(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/api/sessions/{session_id}/tags", response_model=TagResponse)
def api_add_tag(session_id: str, body: TagIn) -> dict[str, Any]:
    tag = " ".join(body.tag.strip().lower().split())[:40]
    if not tag:
        raise HTTPException(status_code=400, detail="empty topic")
    if not sessions_repo.exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    sessions_repo.add_tag(session_id, tag)
    return {"tag": tag}


@router.delete("/api/sessions/{session_id}/tags/{tag}", response_model=OkResponse)
def api_remove_tag(session_id: str, tag: str) -> dict[str, Any]:
    sessions_repo.remove_tag(session_id, tag)
    return {"ok": True}
