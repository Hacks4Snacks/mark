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
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    ask,
    collections,
    config,
    db,
    exporting,
    ingest,
    render,
    search,
    uploads,
)

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
                "kind": "watched",
                "enabled": cfg.enabled,
                "roots": [str(r) for r in cfg.roots],
                "exists": any(Path(r).exists() for r in cfg.roots),
                "indexed": sum(by_source.get(n, 0) for n in s.row_sources),
            }
        )
    for imp in ingest.IMPORT_SOURCES:
        out.append(
            {
                "key": imp.key,
                "label": imp.label or imp.key,
                "kind": "import",
                "enabled": True,
                "roots": [],
                "exists": True,
                "indexed": by_source.get(imp.key, 0),
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
        include_automation=include_automation,
        sort=sort,
        limit=min(limit, 500),
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


@app.get("/api/sessions/{session_id}/related")
def api_related(session_id: str) -> list[dict[str, Any]]:
    return search.related_sessions(session_id)


@app.get("/api/sessions/{session_id}/export.md")
def api_export_markdown(session_id: str) -> PlainTextResponse:
    session = search.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    body = exporting.session_to_markdown(session)
    fname = exporting.slug(session.get("title") or "", session_id) + ".md"
    return PlainTextResponse(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# Shell-ish languages treated as runnable "commands" in the library.
_SHELL_LANGS = (
    "bash",
    "sh",
    "shell",
    "shellscript",
    "zsh",
    "console",
    "shell-session",
    "sh-session",
    "shellsession",
    "powershell",
    "ps1",
)


@app.get("/api/snippets/languages")
def api_snippet_languages() -> list[dict[str, Any]]:
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT cb.language AS language, COUNT(*) AS count "
            "FROM code_blocks cb JOIN sessions s ON s.id = cb.session_id "
            "WHERE s.source != 'automation' AND cb.language IS NOT NULL "
            "  AND cb.language != '' "
            "GROUP BY cb.language ORDER BY count DESC, language"
        ).fetchall()
    return [{"language": r["language"], "count": r["count"]} for r in rows]


@app.get("/api/snippets")
def api_snippets(
    q: str = "", language: str = "", commands: bool = False, limit: int = 80
) -> dict[str, Any]:
    where = [
        "s.source != 'automation'",
        "cb.content IS NOT NULL",
        "LENGTH(TRIM(cb.content)) > 1",
    ]
    params: list[Any] = []
    if commands:
        where.append("LOWER(cb.language) IN (%s)" % ",".join("?" * len(_SHELL_LANGS)))
        params.extend(_SHELL_LANGS)
    elif language:
        where.append("cb.language = ?")
        params.append(language)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("cb.content LIKE ? ESCAPE '\\'")
        params.append(f"%{esc}%")
    sql = (
        "SELECT cb.id, cb.session_id, cb.turn_index, cb.language, cb.content, "
        "  s.title AS session_title, s.source, s.repository, s.updated_at "
        "FROM code_blocks cb JOIN sessions s ON s.id = cb.session_id "
        "WHERE "
        + " AND ".join(where)
        + " ORDER BY s.updated_at DESC, cb.id DESC LIMIT ?"
    )
    params.append(max(1, min(limit, 300)))
    with db.cursor() as cur:
        rows = cur.execute(sql, params).fetchall()
    return {
        "snippets": [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "session_title": r["session_title"],
                "source": r["source"],
                "repository": r["repository"],
                "language": r["language"],
                "content": r["content"],
                "turn_index": r["turn_index"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    }


@app.get("/api/usage")
def api_usage(include_automation: bool = False) -> dict[str, Any]:
    auto = "" if include_automation else " WHERE source != 'automation'"
    auto_and = "" if include_automation else " AND source != 'automation'"
    with db.cursor() as cur:
        t = cur.execute(
            "SELECT COUNT(*) sessions, COALESCE(SUM(est_cost_usd),0) cost, "
            "COALESCE(SUM(premium_requests),0) premium, COALESCE(SUM(input_tokens),0) input_tokens, "
            "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(duration_seconds),0) duration, "
            "COALESCE(SUM(aiu),0) aiu FROM sessions" + auto
        ).fetchone()
        by_day = cur.execute(
            "SELECT substr(COALESCE(updated_at, created_at),1,10) day, COUNT(*) sessions, "
            "COALESCE(SUM(est_cost_usd),0) cost, COALESCE(SUM(premium_requests),0) premium "
            "FROM sessions WHERE COALESCE(updated_at, created_at) IS NOT NULL"
            + auto_and
            + " GROUP BY day ORDER BY day"
        ).fetchall()
        by_model = cur.execute(
            "SELECT COALESCE(NULLIF(model,''),'(unknown)') model, COUNT(*) sessions, "
            "COALESCE(SUM(est_cost_usd),0) cost, COALESCE(SUM(premium_requests),0) premium "
            "FROM sessions"
            + auto
            + " GROUP BY model ORDER BY cost DESC, sessions DESC LIMIT 12"
        ).fetchall()
        by_repo = cur.execute(
            "SELECT COALESCE(NULLIF(repository,''),'(none)') repository, COUNT(*) sessions, "
            "COALESCE(SUM(est_cost_usd),0) cost FROM sessions"
            + auto
            + " GROUP BY repository ORDER BY cost DESC, sessions DESC LIMIT 12"
        ).fetchall()
        by_source = cur.execute(
            "SELECT source, COUNT(*) sessions, COALESCE(SUM(est_cost_usd),0) cost, "
            "COALESCE(SUM(premium_requests),0) premium FROM sessions"
            + auto
            + " GROUP BY source ORDER BY cost DESC"
        ).fetchall()
    return {
        "totals": {
            "sessions": t["sessions"],
            "cost": round(t["cost"], 2),
            "premium": int(t["premium"]),
            "input_tokens": int(t["input_tokens"]),
            "output_tokens": int(t["output_tokens"]),
            "duration": t["duration"] or 0,
            "aiu": round(t["aiu"], 2),
        },
        "by_day": [
            {
                "day": r["day"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 4),
                "premium": int(r["premium"]),
            }
            for r in by_day
        ],
        "by_model": [
            {
                "model": r["model"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 2),
                "premium": int(r["premium"]),
            }
            for r in by_model
        ],
        "by_repo": [
            {
                "repository": r["repository"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 2),
            }
            for r in by_repo
        ],
        "by_source": [
            {
                "source": r["source"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 2),
                "premium": int(r["premium"]),
            }
            for r in by_source
        ],
    }


class AskIn(BaseModel):
    question: str
    limit: int = 6


class RenderIn(BaseModel):
    text: str


@app.get("/api/ask/status")
def api_ask_status() -> dict[str, Any]:
    return ask.status()


@app.post("/api/ask")
def api_ask(body: AskIn) -> StreamingResponse:
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="empty question")
    limit = max(1, min(int(body.limit), 20))

    def gen():
        for event in ask.stream_answer(question, limit=limit):
            yield "data: " + json.dumps(event) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/render")
def api_render(body: RenderIn) -> dict[str, str]:
    return {"html": render.render_markdown(body.text or "")}


# --- collections -------------------------------------------------------------


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


@app.get("/api/collections")
def api_collections() -> list[dict[str, Any]]:
    return collections.list_collections()


@app.post("/api/collections")
def api_create_collection(body: CollectionIn) -> dict[str, Any]:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    cid = collections.create(
        name, body.description, body.icon, body.color, body.rule, body.pinned
    )
    return collections.get_collection(cid)


@app.get("/api/collections/{cid}")
def api_collection(cid: str) -> dict[str, Any]:
    coll = collections.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    coll["members"] = collections.members_as_cards(cid)
    coll["overview"] = collections.overview(cid)
    coll["count"] = len(coll["members"])
    return coll


@app.patch("/api/collections/{cid}")
def api_update_collection(cid: str, body: CollectionPatch) -> dict[str, Any]:
    if not collections.get_collection(cid):
        raise HTTPException(status_code=404, detail="collection not found")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and not (fields["name"] or "").strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    collections.update(cid, fields)
    return collections.get_collection(cid)


@app.delete("/api/collections/{cid}")
def api_delete_collection(cid: str) -> dict[str, Any]:
    if not collections.delete(cid):
        raise HTTPException(status_code=404, detail="collection not found")
    return {"ok": True}


@app.post("/api/collections/{cid}/members")
def api_add_member(cid: str, body: MemberIn) -> dict[str, Any]:
    coll = collections.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    with db.cursor() as cur:
        if not cur.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (body.session_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="session not found")
    collections.set_member(cid, body.session_id, body.state)
    return {"ok": True, "count": len(collections.resolve_member_ids(coll))}


@app.delete("/api/collections/{cid}/members/{session_id}")
def api_remove_member(cid: str, session_id: str) -> dict[str, Any]:
    coll = collections.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    collections.remove_member(cid, session_id)
    return {"ok": True, "count": len(collections.resolve_member_ids(coll))}


@app.get("/api/sessions/{session_id}/collections")
def api_session_collections(session_id: str) -> list[dict[str, Any]]:
    return collections.collections_for_session(session_id)


@app.post("/api/collections/{cid}/ask")
def api_collection_ask(cid: str, body: CollAskIn) -> StreamingResponse:
    coll = collections.get_collection(cid)
    if not coll:
        raise HTTPException(status_code=404, detail="collection not found")
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="empty question")
    limit = max(1, min(int(body.limit), 20))
    member_ids = collections.resolve_member_ids(coll)

    def gen():
        for event in ask.stream_answer(question, limit=limit, session_ids=member_ids):
            yield "data: " + json.dumps(event) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sync_session_fts_tags(cur, session_id: str) -> None:
    """Refresh a session's FTS ``tags`` column so manual topics are keyword-searchable."""
    tag_text = " ".join(
        r["tag"]
        for r in cur.execute("SELECT tag FROM tags WHERE session_id = ?", (session_id,))
    )
    cur.execute(
        "UPDATE search_index SET tags = ? WHERE session_id = ?", (tag_text, session_id)
    )


class TagIn(BaseModel):
    tag: str


@app.post("/api/sessions/{session_id}/tags")
def api_add_tag(session_id: str, body: TagIn) -> dict[str, Any]:
    tag = " ".join(body.tag.strip().lower().split())[:40]
    if not tag:
        raise HTTPException(status_code=400, detail="empty topic")
    with db.cursor() as cur:
        if not cur.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="session not found")
        cur.execute(
            "INSERT INTO tags(session_id, tag, score, manual) VALUES (?,?,?,1) "
            "ON CONFLICT(session_id, tag) DO UPDATE SET manual = 1",
            (session_id, tag, 100.0),
        )
        _sync_session_fts_tags(cur, session_id)
    return {"tag": tag}


@app.delete("/api/sessions/{session_id}/tags/{tag}")
def api_remove_tag(session_id: str, tag: str) -> dict[str, Any]:
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM tags WHERE session_id = ? AND tag = ?",
            (session_id, tag.strip().lower()),
        )
        _sync_session_fts_tags(cur, session_id)
    return {"ok": True}


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
    filename = file.filename or "upload.bin"
    # A recognised export (e.g. ChatGPT conversations.json) is imported as many
    # sessions; anything else becomes a single searchable document.
    imported = await run_in_threadpool(ingest.import_export, filename, data)
    if imported.get("matched"):
        return imported
    sid = await run_in_threadpool(uploads.add_file, filename, data, file.content_type)
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
