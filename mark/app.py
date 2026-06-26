"""FastAPI application factory and lifespan wiring.

Builds the app from the domain routers in :mod:`mark.api`, starts background
indexing on startup, and mounts the static UI last so ``/api/*`` routes win.
Binds to localhost only by default; this is a personal, single-user app.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import background, config, db
from .api import api_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    background.start()
    try:
        yield
    finally:
        background.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Mark", version="0.1.0", lifespan=lifespan)
    app.include_router(api_router)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(config.WEB_DIR / "index.html")

    # Mounted last so /api/* wins over the static catch-all.
    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
    return app


app = create_app()
