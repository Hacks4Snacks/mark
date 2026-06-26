"""mindex FastAPI application — API + static UI.

Binds to localhost only by default; this is a personal, single-user app.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, ingest, render, search, uploads

# --- background reindex state ------------------------------------------------

_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "running": False,
    "message": "idle",
    "last_result": None,
    "started_at": None,
    "finished_at": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_reindex(rebuild: bool) -> None:
    with _status_lock:
        if _status["running"]:
            return
        _status.update(
            running=True, message="Starting…", started_at=_now(), finished_at=None
        )

    def progress(msg: str) -> None:
        with _status_lock:
            _status["message"] = msg

    try:
        result = ingest.ingest_all(rebuild=rebuild, progress=progress)
        added = result.get("added", 0)
        updated = result.get("updated", 0)
        auto = result.get("automation", 0)
        msg = f"Indexed {added} new, {updated} updated"
        if auto:
            msg += f" · {auto} automation runs (hidden by default)"
        with _status_lock:
            _status.update(last_result=result, message=msg)
    except Exception as exc:  # surface, don't crash the server
        with _status_lock:
            _status["message"] = f"Error: {exc}"
    finally:
        with _status_lock:
            _status.update(running=False, finished_at=_now())


def _start_reindex(rebuild: bool = False) -> bool:
    with _status_lock:
        if _status["running"]:
            return False
    threading.Thread(target=_run_reindex, args=(rebuild,), daemon=True).start()
    return True


# --- background auto-sync ----------------------------------------------------

_sync_stop = threading.Event()


def _sync_loop() -> None:
    """Import on startup, then re-import whenever a session changes or ends.

    Cheap source fingerprints are compared every ``SYNC_INTERVAL`` seconds; a
    real (incremental) import only runs when something actually changed, so an
    idle machine does almost no work.
    """
    try:
        last_fp = ingest.sources_fingerprint()
    except Exception:
        last_fp = ""
    _start_reindex(rebuild=False)  # pick up anything new since last launch

    while not _sync_stop.wait(config.SYNC_INTERVAL):
        try:
            fp = ingest.sources_fingerprint()
        except Exception:
            continue
        if fp and fp != last_fp:
            last_fp = fp
            _start_reindex(rebuild=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    if config.AUTO_SYNC:
        threading.Thread(target=_sync_loop, daemon=True).start()
    else:
        # Auto-sync off: still import once on startup so new sessions appear.
        _start_reindex(rebuild=False)
    try:
        yield
    finally:
        _sync_stop.set()


app = FastAPI(title="mindex", version="0.1.0", lifespan=lifespan)


# --- API ---------------------------------------------------------------------


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    with db.cursor() as cur:
        sources = {
            r["source"]: r["n"]
            for r in cur.execute(
                "SELECT source, COUNT(*) n FROM sessions GROUP BY source"
            ).fetchall()
        }
        visible = cur.execute(
            "SELECT COUNT(*) FROM sessions WHERE source != 'automation'"
        ).fetchone()[0]
        turns = cur.execute(
            "SELECT COUNT(*) FROM turns t JOIN sessions s ON s.id = t.session_id "
            "WHERE s.source != 'automation'"
        ).fetchone()[0]
        files = cur.execute(
            "SELECT COUNT(DISTINCT f.file_path) FROM session_files f "
            "JOIN sessions s ON s.id = f.session_id WHERE s.source != 'automation'"
        ).fetchone()[0]
        tags = cur.execute(
            "SELECT COUNT(DISTINCT t.tag) FROM tags t "
            "JOIN sessions s ON s.id = t.session_id WHERE s.source != 'automation'"
        ).fetchone()[0]
        agg = cur.execute(
            "SELECT COALESCE(SUM(est_cost_usd),0) c, COALESCE(SUM(premium_requests),0) p, "
            "COALESCE(SUM(duration_seconds),0) d FROM sessions WHERE source != 'automation'"
        ).fetchone()
        rng = cur.execute(
            "SELECT MIN(COALESCE(created_at, updated_at)) mn, MAX(COALESCE(updated_at, created_at)) mx FROM sessions"
        ).fetchone()
    return {
        "sessions": visible,
        "automation": sources.get("automation", 0),
        "by_source": sources,
        "turns": turns,
        "files": files,
        "tags": tags,
        "total_cost_usd": round(agg["c"], 2),
        "premium_requests": int(agg["p"]),
        "total_duration_seconds": agg["d"],
        "date_min": rng["mn"],
        "date_max": rng["mx"],
        "embed_model": db.get_meta("embed_model"),
        "last_ingest": db.get_meta("last_ingest"),
    }


@app.get("/api/facets")
def api_facets() -> dict[str, Any]:
    return search.facets()


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    model = db.get_meta("embed_model") or ""
    with _status_lock:
        st = dict(_status)
    st["embed_model"] = model
    st["semantic"] = bool(model) and not model.startswith("builtin")
    st["auto_sync"] = config.AUTO_SYNC
    st["sync_interval"] = config.SYNC_INTERVAL
    st["last_ingest"] = db.get_meta("last_ingest")
    return st


@app.get("/api/sources")
def api_sources() -> list[dict[str, Any]]:
    """Effective per-source config (defaults < sources.toml < env) for the UI.

    ``indexed`` counts existing sessions for the adapter even when it is disabled,
    since disabling keeps already-indexed rows.
    """
    with db.cursor() as cur:
        by_source = {
            r["source"]: r["n"]
            for r in cur.execute(
                "SELECT source, COUNT(*) n FROM sessions GROUP BY source"
            ).fetchall()
        }
    out: list[dict[str, Any]] = []
    for s in ingest.WATCHED_SOURCES:
        cfg = config.resolve_source_config(s.default_config())
        out.append(
            {
                "key": cfg.key,
                "label": cfg.label or cfg.key,
                "enabled": cfg.enabled,
                "roots": [str(r) for r in cfg.roots],
                "exists": any(Path(r).exists() for r in cfg.roots),
                "indexed": sum(by_source.get(n, 0) for n in s.row_sources),
            }
        )
    return out


@app.post("/api/reindex")
def api_reindex(rebuild: bool = False) -> dict[str, Any]:
    started = _start_reindex(rebuild=rebuild)
    with _status_lock:
        st = dict(_status)
    st["started"] = started
    return st


@app.get("/api/search")
def api_search(
    q: str = "",
    mode: str = "hybrid",
    repo: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_automation: bool = False,
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
        include_automation=include_automation,
        limit=min(limit, 100),
    )
    return {"query": q, "mode": mode, "count": len(results), "results": results}


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str) -> dict[str, Any]:
    session = search.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")

    for turn in session["turns"]:
        turn["user_html"] = render.render_markdown(turn.get("user_message"))
        turn["assistant_html"] = render.render_markdown(turn.get("assistant_response"))
        try:
            turn["tools"] = json.loads(turn.get("tools") or "[]")
        except (TypeError, json.JSONDecodeError):
            turn["tools"] = []
    if session.get("document") and session["document"].get("content"):
        session["document"]["html"] = render.render_markdown(
            session["document"]["content"]
        )
    for att in session.get("attachments") or []:
        content = att.get("content")
        if not content:
            continue
        name = (att.get("filename") or "").lower()
        if name.endswith((".md", ".markdown")):
            att["html"] = render.render_markdown(content)
        else:
            lang = name.rsplit(".", 1)[-1] if "." in name else ""
            att["html"] = render.render_markdown(f"```{lang}\n{content}\n```")
    return session


class NoteIn(BaseModel):
    title: str = "Untitled note"
    text: str = ""


@app.post("/api/notes")
def api_add_note(note: NoteIn) -> dict[str, Any]:
    if not note.text.strip() and not note.title.strip():
        raise HTTPException(status_code=400, detail="note is empty")
    sid = uploads.add_note(note.title, note.text)
    return {"id": sid}


@app.post("/api/uploads")
async def api_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    sid = uploads.add_file(file.filename or "upload.bin", data, file.content_type)
    return {"id": sid}


@app.get("/api/pygments.css")
def api_pygments_css() -> PlainTextResponse:
    return PlainTextResponse(render.pygments_css(), media_type="text/css")


# --- static UI (mounted last so /api/* wins) ---------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    config.ensure_dirs()
    print(f"mindex → http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
