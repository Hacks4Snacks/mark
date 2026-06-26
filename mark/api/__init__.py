from __future__ import annotations

from fastapi import APIRouter

from . import ask, collections, search, sessions, sources, uploads, usage

api_router = APIRouter()
api_router.include_router(search.router)
api_router.include_router(sessions.router)
api_router.include_router(collections.router)
api_router.include_router(usage.router)
api_router.include_router(ask.router)
api_router.include_router(sources.router)
api_router.include_router(uploads.router)

__all__ = ["api_router"]
