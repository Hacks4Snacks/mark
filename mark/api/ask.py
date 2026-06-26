"""Optional local-LLM "Ask your history" endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import ask
from ..schemas import AskIn

router = APIRouter()


@router.get("/api/ask/status")
def api_ask_status() -> dict[str, Any]:
    return ask.status()


@router.post("/api/ask")
def api_ask(body: AskIn) -> StreamingResponse:
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="empty question")
    limit = max(1, min(int(body.limit), 20))

    def gen():
        for event in ask.stream_answer(question, limit=limit):
            yield "data: " + json.dumps(event) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
