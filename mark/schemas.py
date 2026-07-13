from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class AskIn(BaseModel):
    question: str
    limit: int | None = None


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
    limit: int | None = None


class TagIn(BaseModel):
    tag: str


class NoteIn(BaseModel):
    title: str = "Untitled note"
    text: str = ""


class OkResponse(BaseModel):
    ok: bool = True


class OkCountResponse(BaseModel):
    ok: bool = True
    count: int


class HiddenResponse(BaseModel):
    ok: bool = True
    hidden: bool


class IdResponse(BaseModel):
    id: str


class TagResponse(BaseModel):
    tag: str


class RenderResponse(BaseModel):
    html: str


class StatsResponse(BaseModel):
    sessions: int
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
    queued: bool = False
    message: str
    last_result: dict[str, Any] | None = None
    last_error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    embed_model: str = ""
    semantic: bool = False
    semantic_active: bool = False
    semantic_pending: bool = False
    semantic_generation: int = 0
    semantic_fingerprint: str | None = None
    semantic_target_fingerprint: str | None = None
    semantic_error: str | None = None
    auto_sync: bool = False
    sync_interval: int = 0
    last_ingest: str | None = None
    resume_cmd: str = "copilot --resume {id}"
    ask_enabled: bool = False
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
