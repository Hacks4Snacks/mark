"""Pydantic request and response models for the HTTP API.

Request models validate incoming JSON bodies. Response models document and
lightly type the responses; nested collections are kept as ``dict``/``list``
of dicts on purpose so that adding a field to a query result never silently
drops it from the API payload.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# --- request bodies ----------------------------------------------------------


class AskIn(BaseModel):
    question: str
    limit: int = 6


class RenderIn(BaseModel):
    text: str


class CollectionIn(BaseModel):
    name: str
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    rule: dict[str, Any] | None = None
    pinned: bool = False


class CollectionPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    rule: dict[str, Any] | None = None
    pinned: bool | None = None


class MemberIn(BaseModel):
    session_id: str
    state: str = "include"


class CollAskIn(BaseModel):
    question: str
    limit: int = 8


class TagIn(BaseModel):
    tag: str


class NoteIn(BaseModel):
    title: str = "Untitled note"
    text: str = ""


# --- response bodies ---------------------------------------------------------


class OkResponse(BaseModel):
    ok: bool = True


class OkCountResponse(BaseModel):
    ok: bool = True
    count: int


class IdResponse(BaseModel):
    id: str


class TagResponse(BaseModel):
    tag: str


class RenderResponse(BaseModel):
    html: str


class StatsResponse(BaseModel):
    sessions: int
    automation: int
    by_source: dict[str, int]
    turns: int
    files: int
    tags: int
    total_cost_usd: float
    premium_requests: int
    total_duration_seconds: float | None = None
    date_min: str | None = None
    date_max: str | None = None
    embed_model: str | None = None
    last_ingest: str | None = None


class StatusResponse(BaseModel):
    running: bool
    message: str
    last_result: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    embed_model: str = ""
    semantic: bool = False
    auto_sync: bool = False
    sync_interval: int = 0
    last_ingest: str | None = None
    # Only present on the POST /api/reindex response.
    started: bool | None = None


class SourceInfo(BaseModel):
    key: str
    label: str
    kind: str
    enabled: bool
    roots: list[str]
    exists: bool
    indexed: int


class FacetsResponse(BaseModel):
    repositories: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    date_min: str | None = None
    date_max: str | None = None


class SearchResponse(BaseModel):
    query: str
    mode: str
    count: int
    results: list[dict[str, Any]]


class SnippetsResponse(BaseModel):
    snippets: list[dict[str, Any]]


class UsageResponse(BaseModel):
    totals: dict[str, Any]
    by_day: list[dict[str, Any]]
    by_model: list[dict[str, Any]]
    by_repo: list[dict[str, Any]]
    by_source: list[dict[str, Any]]
