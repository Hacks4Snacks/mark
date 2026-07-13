from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import config


class CollectionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    q: str | None = Field(default=None, max_length=config.MAX_COLLECTION_QUERY_CHARS)
    mode: Literal["hybrid", "semantic", "keyword"] = "hybrid"
    repo: str | None = Field(
        default=None, max_length=config.MAX_COLLECTION_FILTER_CHARS
    )
    source: str | None = Field(
        default=None, max_length=config.MAX_COLLECTION_FILTER_CHARS
    )
    tags: list[str] = Field(default_factory=list, max_length=config.MAX_COLLECTION_TAGS)
    date_from: date | None = None
    date_to: date | None = None
    sort: Literal["recent", "oldest", "turns", "title"] = "recent"

    @field_validator("q", "repo", "source", mode="after")
    @classmethod
    def empty_string_to_none(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("tags", mode="after")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in tags:
            tag = " ".join(raw.strip().lower().split())
            if not tag:
                continue
            if len(tag) > config.MAX_TAG_CHARS:
                raise ValueError(
                    f"topics must be at most {config.MAX_TAG_CHARS} characters"
                )
            if tag not in normalized:
                normalized.append(tag)
        return normalized

    @model_validator(mode="after")
    def validate_dates(self) -> CollectionRule:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=config.MAX_ASK_QUESTION_CHARS)
    limit: int | None = Field(default=None, ge=1, le=config.MAX_ASK_SESSION_LIMIT)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be empty")
        return value.strip()


class RenderIn(BaseModel):
    text: str = Field(max_length=config.MAX_RENDER_TEXT_CHARS)


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=config.MAX_COLLECTION_NAME_CHARS)
    description: str | None = Field(
        default=None, max_length=config.MAX_COLLECTION_DESCRIPTION_CHARS
    )
    icon: str | None = Field(default=None, max_length=80)
    color: Literal["purple", "cyan", "green", "amber", "rose"] | None = None
    rule: CollectionRule | None = None
    pinned: bool = False


class CollectionPatch(BaseModel):
    name: str | None = Field(default=None, max_length=config.MAX_COLLECTION_NAME_CHARS)
    description: str | None = Field(
        default=None, max_length=config.MAX_COLLECTION_DESCRIPTION_CHARS
    )
    icon: str | None = Field(default=None, max_length=80)
    color: Literal["purple", "cyan", "green", "amber", "rose"] | None = None
    rule: CollectionRule | None = None
    pinned: bool | None = None


class MemberIn(BaseModel):
    session_id: str
    state: Literal["include", "exclude"] = "include"


class CollAskIn(BaseModel):
    question: str = Field(min_length=1, max_length=config.MAX_ASK_QUESTION_CHARS)
    limit: int | None = Field(default=None, ge=1, le=config.MAX_ASK_SESSION_LIMIT)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be empty")
        return value.strip()


class TagIn(BaseModel):
    tag: str = Field(min_length=1, max_length=config.MAX_TAG_CHARS)


class NoteIn(BaseModel):
    title: str = Field(default="Untitled note", max_length=config.MAX_NOTE_TITLE_CHARS)
    text: str = Field(default="", max_length=config.MAX_NOTE_TEXT_CHARS)


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
    retry_required: bool = False
    retry_attempt: int = 0
    retry_at: str | None = None
    sync_error: str | None = None
    sync_worker_alive: bool = False
    ingest_worker_alive: bool = False
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


class ReindexStatusResponse(StatusResponse):
    started: bool
    admission: Literal["accepted", "covered", "stopping"]


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
