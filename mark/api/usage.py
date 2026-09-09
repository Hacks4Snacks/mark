from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response

from ..repositories import snippets as snippets_repo
from ..repositories import stats as stats_repo
from ..repositories import usage as usage_repo
from ..schemas import (
    OkResponse,
    SnippetsResponse,
    SolutionIn,
    SolutionSourceIn,
    SolutionUpdate,
    StatsResponse,
    UsageResponse,
)

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


@router.post("/api/solutions/preview")
def api_solution_preview(body: SolutionSourceIn) -> dict[str, Any]:
    """Read source content for explicit save confirmation; never creates a copy."""
    try:
        return snippets_repo.solution_preview(body.model_dump())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


@router.get("/api/solutions")
def api_solutions(
    q: str = Query(default="", max_length=2_000),
    tag: str = Query(default="", max_length=40),
    status: Literal["", "needs_review", "verified", "outdated"] = "",
    favorite: bool = False,
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    return snippets_repo.list_solutions(
        q=q, tag=tag, status=status, favorite=favorite, offset=offset, limit=limit
    )


@router.get("/api/solutions/{solution_id}")
def api_solution(solution_id: str) -> dict[str, Any]:
    solution = snippets_repo.get_solution(solution_id)
    if not solution:
        raise HTTPException(status_code=404, detail="Saved solution not found")
    return solution


@router.post("/api/solutions")
def api_create_solution(body: SolutionIn, response: Response) -> dict[str, Any]:
    try:
        solution_id, created = snippets_repo.create_solution(
            body.source.model_dump(),
            body.source_sha256,
            body.model_dump(exclude={"source", "source_sha256"}),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.status_code = 201 if created else 200
    return {"id": solution_id, "created": created}


@router.put("/api/solutions/{solution_id}", response_model=OkResponse)
def api_update_solution(solution_id: str, body: SolutionUpdate) -> dict[str, bool]:
    """Replace editable metadata, never original content or provenance."""
    try:
        updated = snippets_repo.update_solution(
            solution_id, body.revision, body.model_dump(exclude={"revision"})
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Saved solution not found")
    return {"ok": True}


@router.delete("/api/solutions/{solution_id}", response_model=OkResponse)
def api_delete_solution(
    solution_id: str, revision: int = Query(ge=1, le=2**63 - 1)
) -> dict[str, bool]:
    try:
        deleted = snippets_repo.delete_solution(solution_id, revision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved solution not found")
    return {"ok": True}
