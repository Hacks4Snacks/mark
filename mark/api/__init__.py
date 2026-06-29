from __future__ import annotations

from fastapi import APIRouter

from .. import config
from . import ask, collections, search, sessions, sources, uploads, usage


def build_api_router() -> APIRouter:
    """Assemble the API router fresh per app instance so feature flags (e.g. the
    Ask feature) are evaluated at app-creation time rather than import time."""
    api_router = APIRouter()
    api_router.include_router(search.router)
    api_router.include_router(sessions.router)
    api_router.include_router(collections.router)
    api_router.include_router(usage.router)
    # Ask is feature-flagged off by default; only mount its routes when enabled.
    if config.ENABLE_ASK:
        api_router.include_router(ask.router)
    api_router.include_router(sources.router)
    api_router.include_router(uploads.router)
    return api_router


__all__ = ["build_api_router"]
