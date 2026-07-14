from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from .. import background, config, ingest, render
from .. import uploads as uploads_svc
from ..schemas import IdResponse, NoteIn, RenderIn, RenderResponse

router = APIRouter()


async def _read_capped(file: UploadFile, cap: int) -> bytes | None:
    """Read at most ``cap`` bytes from FastAPI's already-spooled upload.

    The extra byte detects overflow without retaining a chunk list and then
    allocating the entire accepted payload again during ``join``.
    """
    if file.size is not None and file.size > cap:
        return None
    data = await file.read(cap + 1)
    return None if len(data) > cap else data


@router.post("/api/notes", response_model=IdResponse)
def api_add_note(note: NoteIn) -> dict[str, Any]:
    if not note.text.strip() and not note.title.strip():
        raise HTTPException(status_code=400, detail="note is empty")
    sid = uploads_svc.add_note(note.title, note.text, do_embed=False)
    background.request_semantic_repair()
    return {"id": sid}


@router.post("/api/uploads")
async def api_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await _read_capped(file, config.MAX_UPLOAD_BYTES)
    if data is None:
        raise HTTPException(status_code=413, detail="file too large")
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    filename = file.filename or "upload.bin"
    # A recognised export (e.g. ChatGPT conversations.json) is imported as many
    # sessions; anything else becomes a single searchable document.
    imported = await run_in_threadpool(
        ingest.import_export, filename, data, do_embed=False
    )
    if imported.get("matched"):
        background.request_semantic_repair()
        return imported
    sid = await run_in_threadpool(
        uploads_svc.add_file,
        filename,
        data,
        file.content_type,
        do_embed=False,
    )
    background.request_semantic_repair()
    return {"id": sid}


@router.post("/api/render", response_model=RenderResponse)
def api_render(body: RenderIn) -> dict[str, str]:
    return {"html": render.render_markdown(body.text or "")}


@router.get("/api/pygments.css")
def api_pygments_css() -> PlainTextResponse:
    return PlainTextResponse(render.pygments_css(), media_type="text/css")
