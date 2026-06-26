from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..repositories import snippets as snippets_repo
from ..repositories import stats as stats_repo
from ..repositories import usage as usage_repo
from ..schemas import SnippetsResponse, StatsResponse, UsageResponse

router = APIRouter()


@router.get("/api/stats", response_model=StatsResponse)
def api_stats() -> dict[str, Any]:
    return stats_repo.overview()


@router.get("/api/usage", response_model=UsageResponse)
def api_usage() -> dict[str, Any]:
    return usage_repo.usage()


@router.get("/api/snippets/languages")
def api_snippet_languages() -> list[dict[str, Any]]:
    return snippets_repo.languages()


@router.get("/api/snippets", response_model=SnippetsResponse)
def api_snippets(
    q: str = "", language: str = "", commands: bool = False, limit: int = 80
) -> dict[str, Any]:
    return {
        "snippets": snippets_repo.snippets(
            q=q, language=language, commands=commands, limit=limit
        )
    }
