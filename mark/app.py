from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import background, config, db
from .api import build_api_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.validate()
    db.init_db()
    background.start(wait_for_http=True)
    try:
        yield
    finally:
        background.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Mark", version="0.1.0", lifespan=lifespan)
    app.include_router(build_api_router())

    @app.middleware("http")
    async def _revalidate_assets(request, call_next):
        # Asset filenames aren't content-hashed, and StaticFiles sends an ETag
        # but no Cache-Control, so browsers heuristically cache the CSS/JS and a
        # rebuilt container's changes don't show up until a hard refresh. Ask
        # for revalidation instead: the ETag makes it a cheap 304 when nothing
        # changed, and reloads pick up new builds automatically.
        response = await call_next(request)
        prior_background = response.background

        async def after_response() -> None:
            if prior_background is not None:
                await prior_background()
            background.mark_http_ready()

        response.background = BackgroundTask(after_response)
        path = request.url.path
        if request.method in ("GET", "HEAD") and not path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(config.WEB_DIR / "index.html")

    # Mounted last so /api/* wins over the static catch-all.
    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
    return app


app = create_app()
