from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import search
from ..schemas import FacetsResponse, SearchResponse

router = APIRouter()


@router.get("/api/search", response_model=SearchResponse)
def api_search(
    q: str = "",
    mode: str = "hybrid",
    repo: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "recent",
    limit: int = 30,
) -> dict[str, Any]:
    tag_list = [t for t in (tags.split(",") if tags else []) if t]
    results = search.search(
        q,
        mode=mode,
        repo=repo,
        source=source,
        tags=tag_list,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        limit=max(1, min(limit, 500)),
    )
    return {"query": q, "mode": mode, "count": len(results), "results": results}


@router.get("/api/facets", response_model=FacetsResponse)
def api_facets() -> dict[str, Any]:
    return search.facets()
