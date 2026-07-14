from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as ApiPath
from fastapi.responses import PlainTextResponse, Response

from .. import attachments, config, exporting, render, search
from ..repositories import sessions as sessions_repo
from ..schemas import HiddenResponse, OkResponse, TagIn, TagResponse

router = APIRouter()
_SQLITE_MAX_INT = 2**63 - 1


def _safe_filename(name: str | None, fallback: str) -> str:
    """A header-safe download filename: basename only, no quotes/control chars."""
    base = Path(name or "").name.strip()
    base = "".join(c for c in base if c >= " " and c not in '"\\')
    return base or fallback


def _render_turn(turn: dict[str, Any], *, allow_deferred: bool) -> dict[str, Any]:
    content_chars = turn.get("content_chars")
    if content_chars is None:
        content_chars = sum(
            len(turn.get(field) or "")
            for field in ("user_message", "assistant_response", "thinking")
        )
    rendered = {
        "turn_index": turn["turn_index"],
        "timestamp": turn.get("timestamp"),
        "content_chars": content_chars,
    }
    try:
        rendered["tools"] = json.loads(turn.get("tools") or "[]")
    except (TypeError, json.JSONDecodeError):
        rendered["tools"] = []
    if allow_deferred and content_chars > config.DETAIL_INLINE_TURN_CHARS:
        rendered["deferred"] = True
        return rendered
    rendered.update(
        deferred=False,
        user_html=render.render_markdown(turn.get("user_message")),
        assistant_html=render.render_markdown(turn.get("assistant_response")),
        thinking_html=(
            render.render_markdown(turn["thinking"]) if turn.get("thinking") else ""
        ),
    )
    return rendered


def _render_turn_page(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_render_turn(turn, allow_deferred=True) for turn in turns]


def _attachment_metadata(att: dict[str, Any]) -> dict[str, Any]:
    kind = att.get("storage_kind")
    att["category"] = "memory" if kind == "inline" else "agent"
    att["downloadable"] = kind in ("inline", "managed")
    att["content_available"] = kind in ("inline", "managed")
    att["content"] = None
    att.pop("stored_path", None)
    att.pop("storage_kind", None)
    att.pop("sha256", None)
    att.pop("capture_version", None)
    return att


def _page(
    items: list[dict[str, Any]], *, offset: int, limit: int, total: int
) -> dict[str, Any]:
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": offset + len(items) < total,
    }


@router.get("/api/sessions/{session_id}/turns")
def api_session_turns(
    session_id: str,
    offset: int = Query(default=0, ge=0, le=_SQLITE_MAX_INT),
    limit: int = Query(default=config.DETAIL_TURN_PAGE_SIZE, ge=1, le=100),
) -> dict[str, Any]:
    if not sessions_repo.exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    turns = search.get_session_turns(
        session_id,
        offset=offset,
        limit=limit,
        defer_above=config.DETAIL_INLINE_TURN_CHARS,
    )
    total = sessions_repo.turn_count(session_id)
    return {
        "turns": _render_turn_page(turns),
        "offset": offset,
        "limit": limit,
        "total": total,
        "has_more": offset + len(turns) < total,
    }


@router.get("/api/sessions/{session_id}/turns/{turn_index}")
def api_session_turn(
    session_id: str,
    turn_index: int = ApiPath(ge=0, le=_SQLITE_MAX_INT),
) -> dict[str, Any]:
    turn = search.get_session_turn(session_id, turn_index)
    if not turn:
        raise HTTPException(status_code=404, detail="turn not found")
    return _render_turn(turn, allow_deferred=False)


@router.get("/api/sessions/{session_id}/files")
def api_session_files(
    session_id: str,
    offset: int = Query(default=0, ge=0, le=_SQLITE_MAX_INT),
    limit: int = Query(default=config.DETAIL_FILE_LIMIT, ge=1, le=1_000),
) -> dict[str, Any]:
    if not sessions_repo.exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    items = search.get_session_files(session_id, offset=offset, limit=limit)
    return _page(
        items,
        offset=offset,
        limit=limit,
        total=sessions_repo.file_count(session_id),
    )


@router.get("/api/sessions/{session_id}/refs")
def api_session_refs(
    session_id: str,
    offset: int = Query(default=0, ge=0, le=_SQLITE_MAX_INT),
    limit: int = Query(default=config.DETAIL_LINK_LIMIT, ge=1, le=1_000),
) -> dict[str, Any]:
    if not sessions_repo.exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    items = search.get_session_refs(session_id, offset=offset, limit=limit)
    return _page(
        items,
        offset=offset,
        limit=limit,
        total=sessions_repo.ref_count(session_id),
    )


@router.get("/api/sessions/{session_id}/attachments")
def api_session_attachments(
    session_id: str,
    offset: int = Query(default=0, ge=0, le=_SQLITE_MAX_INT),
    limit: int = Query(default=config.DETAIL_ATTACHMENT_LIMIT, ge=1, le=1_000),
) -> dict[str, Any]:
    if not sessions_repo.exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    items = [
        _attachment_metadata(att)
        for att in search.get_session_attachments(
            session_id, offset=offset, limit=limit
        )
    ]
    return _page(
        items,
        offset=offset,
        limit=limit,
        total=sessions_repo.attachment_count(session_id),
    )


@router.get("/api/sessions/{session_id}")
def api_session(
    session_id: str,
    turns_offset: int = Query(default=0, ge=0, le=_SQLITE_MAX_INT),
    turns_limit: int = Query(default=config.DETAIL_TURN_PAGE_SIZE, ge=1, le=100),
) -> dict[str, Any]:
    session = search.get_session(
        session_id,
        turns_offset=turns_offset,
        turns_limit=turns_limit,
        defer_turns_above=config.DETAIL_INLINE_TURN_CHARS,
        defer_document_above=config.DETAIL_INLINE_TURN_CHARS,
    )
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    session["turns"] = _render_turn_page(session["turns"])
    session["turns_offset"] = turns_offset
    session["turns_limit"] = turns_limit
    turns_total = sessions_repo.turn_count(session_id)
    session["turns_total"] = turns_total
    session["has_more_turns"] = turns_offset + len(session["turns"]) < turns_total
    summary = session.get("summary") or ""
    if len(summary) > config.DETAIL_SUMMARY_CHARS:
        session["summary"] = summary[: config.DETAIL_SUMMARY_CHARS] + "..."
    session["tags"] = [tag[: config.MAX_TAG_CHARS] for tag in session.get("tags") or []]
    session["manual_tags"] = [
        tag[: config.MAX_TAG_CHARS] for tag in session.get("manual_tags") or []
    ]
    if session.get("document"):
        document = session["document"]
        if document.get("content") is not None:
            document["html"] = render.render_markdown(document["content"])
            document["deferred"] = False
        else:
            document["deferred"] = bool(document.get("content_chars"))
        document["content"] = None
    session["attachments"] = [
        _attachment_metadata(att) for att in session.get("attachments") or []
    ]
    return session


@router.get("/api/sessions/{session_id}/document")
def api_document_content(session_id: str) -> dict[str, Any]:
    document = sessions_repo.get_document(session_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    content = document.get("content")
    if not content:
        raise HTTPException(status_code=404, detail="document content unavailable")
    return {"html": render.render_markdown(content)}


@router.get("/api/sessions/{session_id}/attachments/{doc_id}")
def api_attachment_content(
    session_id: str, doc_id: int = ApiPath(ge=0, le=_SQLITE_MAX_INT)
) -> dict[str, Any]:
    att = sessions_repo.get_attachment(session_id, doc_id)
    if not att:
        raise HTTPException(status_code=404, detail="attachment not found")
    content = attachments.attachment_text(att)
    if content is None:
        raise HTTPException(status_code=404, detail="attachment content unavailable")
    name = (att.get("filename") or "").lower()
    if name.endswith((".md", ".markdown")):
        html = render.render_markdown(content)
    else:
        lang = name.rsplit(".", 1)[-1] if "." in name else ""
        html = render.render_markdown(f"```{lang}\n{content}\n```")
    return {"html": html}


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
def api_download_attachment(
    session_id: str, doc_id: int = ApiPath(ge=0, le=_SQLITE_MAX_INT)
):
    """Download immutable captured content, never the original live file."""
    att = sessions_repo.get_attachment(session_id, doc_id)
    if not att:
        raise HTTPException(status_code=404, detail="attachment not found")
    filename = _safe_filename(att.get("filename"), f"attachment-{doc_id}")
    mime = att.get("mime") or "application/octet-stream"

    body = attachments.attachment_bytes(att)
    if body is not None:
        return Response(
            content=body,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    raise HTTPException(
        status_code=404,
        detail="attachment content was not captured or is no longer available",
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
