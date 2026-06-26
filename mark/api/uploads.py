"""Manual content: notes, file uploads / export imports, and ad-hoc rendering."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from .. import config, ingest, render
from .. import uploads as uploads_svc
from ..schemas import IdResponse, NoteIn, RenderIn, RenderResponse

router = APIRouter()

_READ_CHUNK = 1 << 20  # 1 MiB


async def _read_capped(file: UploadFile, cap: int) -> bytes | None:
    """Read the upload in chunks, aborting once it exceeds ``cap`` bytes.

    Returns the bytes, or ``None`` if the stream is larger than ``cap`` so the
    caller can reject it without having buffered the whole (possibly huge) body.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/api/notes", response_model=IdResponse)
def api_add_note(note: NoteIn) -> dict[str, Any]:
    if not note.text.strip() and not note.title.strip():
        raise HTTPException(status_code=400, detail="note is empty")
    sid = uploads_svc.add_note(note.title, note.text)
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
    imported = await run_in_threadpool(ingest.import_export, filename, data)
    if imported.get("matched"):
        return imported
    sid = await run_in_threadpool(
        uploads_svc.add_file, filename, data, file.content_type
    )
    return {"id": sid}


@router.post("/api/render", response_model=RenderResponse)
def api_render(body: RenderIn) -> dict[str, str]:
    return {"html": render.render_markdown(body.text or "")}


@router.get("/api/pygments.css")
def api_pygments_css() -> PlainTextResponse:
    return PlainTextResponse(render.pygments_css(), media_type="text/css")
