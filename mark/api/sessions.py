from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response

from .. import exporting, render, search
from ..repositories import sessions as sessions_repo
from ..schemas import HiddenResponse, OkResponse, TagIn, TagResponse

router = APIRouter()


def _safe_filename(name: str | None, fallback: str) -> str:
    """A header-safe download filename: basename only, no quotes/control chars."""
    base = Path(name or "").name.strip()
    base = "".join(c for c in base if c >= " " and c not in '"\\')
    return base or fallback


@router.get("/api/sessions/{session_id}")
def api_session(session_id: str) -> dict[str, Any]:
    session = search.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    for turn in session["turns"]:
        turn["user_html"] = render.render_markdown(turn.get("user_message"))
        turn["assistant_html"] = render.render_markdown(turn.get("assistant_response"))
        turn["thinking_html"] = (
            render.render_markdown(turn["thinking"]) if turn.get("thinking") else ""
        )
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


@router.post("/api/sessions/{session_id}/hide", response_model=HiddenResponse)
def api_hide_session(session_id: str) -> dict[str, Any]:
    """Hide a session from listings/aggregates without deleting it."""
    if not sessions_repo.set_hidden(session_id, True):
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "hidden": True}


@router.post("/api/sessions/{session_id}/unhide", response_model=HiddenResponse)
def api_unhide_session(session_id: str) -> dict[str, Any]:
    """Restore a previously hidden session."""
    if not sessions_repo.set_hidden(session_id, False):
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "hidden": False}


@router.delete("/api/sessions/{session_id}", response_model=OkResponse)
def api_delete_session(session_id: str) -> dict[str, Any]:
    """Permanently delete a session and tombstone it so a re-scan can't restore it."""
    if not sessions_repo.purge(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True}


@router.get("/api/sessions/{session_id}/attachments/{doc_id}/download")
def api_download_attachment(session_id: str, doc_id: int):
    """Download an agent-created attachment: from disk if it still exists,
    otherwise from the snapshot stored at ingest time."""
    att = sessions_repo.get_attachment(session_id, doc_id)
    if not att:
        raise HTTPException(status_code=404, detail="attachment not found")
    filename = _safe_filename(att.get("filename"), f"attachment-{doc_id}")
    mime = att.get("mime") or "application/octet-stream"

    stored = att.get("stored_path")
    if stored and Path(stored).is_file():
        return FileResponse(stored, media_type=mime, filename=filename)

    content = att.get("content")
    if content is not None:
        body = content.encode("utf-8") if isinstance(content, str) else content
        return Response(
            content=body,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    raise HTTPException(
        status_code=404,
        detail="attachment content is unavailable (binary or larger than the snapshot limit)",
    )


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
