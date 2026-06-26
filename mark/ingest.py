from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import config, db, embeddings, persist
from .sources import IMPORT_SOURCES, WATCHED_SOURCES
from .sources.base import ProgressCb

__all__ = ["import_export", "ingest_all", "sources_fingerprint"]


def _embed_pending(progress: ProgressCb | None = None, batch: int = 256) -> int:
    """Embed chunks that lack a vector, capped to the first N chunks per session.

    Keyword search indexes every chunk, but semantic search loads all vectors into
    memory, so embeddings are bounded per session (earliest chunks — user prompts
    first — win). The cap is applied by chunk rank within each session, so it is
    stable across incremental runs.
    """
    emb = embeddings.get_embedder()
    with db.transaction() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT r.id, r.session_id, r.content FROM ("
            "  SELECT c.id, c.session_id, c.content, "
            "         ROW_NUMBER() OVER (PARTITION BY c.session_id ORDER BY c.id) AS rn "
            "  FROM chunks c"
            ") r "
            "LEFT JOIN embeddings e ON e.chunk_id = r.id "
            "WHERE e.chunk_id IS NULL AND r.rn <= ?",
            (config.MAX_EMBED_CHUNKS_PER_SESSION,),
        ).fetchall()
        total = len(rows)
        for i in range(0, total, batch):
            part = rows[i : i + batch]
            vectors = emb.embed([r["content"] for r in part])
            cur.executemany(
                "INSERT OR REPLACE INTO embeddings(chunk_id, session_id, model, dim, vector) VALUES (?,?,?,?,?)",
                [
                    (r["id"], r["session_id"], emb.name, emb.dim, embeddings.to_blob(v))
                    for r, v in zip(part, vectors, strict=False)
                ],
            )
            conn.commit()
            if progress:
                progress(f"Embedding {min(i + batch, total)}/{total} chunks...")
    return total


def sources_fingerprint() -> str:
    """A cheap signature of all enabled on-disk sources.

    Joins each enabled source's stat-only fingerprint so a background loop can
    detect when a session was written or ended without doing a full import.
    Changes whenever any source is added, grows, is rewritten, or is toggled.
    Disabled sources contribute nothing (and so never trigger a sync).
    """
    parts: list[str] = []
    for s in WATCHED_SOURCES:
        cfg = config.resolve_source_config(s.default_config())
        if not cfg.enabled:
            continue
        parts.append(f"{s.key}={s.fingerprint(cfg)}")
    return "|".join(parts)


def _seed_tombstones(cur, existing: dict[str, str]) -> None:
    """Make permanently deleted sessions look already-present-and-unchanged.

    A purge leaves a tombstone but removes the row, so a re-scan would otherwise
    re-parse the on-disk session and report a phantom "added". Seeding the
    deletion-time hash lets the adapters skip it as unchanged; ``write_session``
    still hard-blocks any that genuinely changed, so the deletion always sticks.
    """
    for row in cur.execute("SELECT session_id, content_hash FROM tombstones"):
        existing.setdefault(row["session_id"], row["content_hash"])


def ingest_all(
    *,
    rebuild: bool = False,
    do_embed: bool = True,
    progress: ProgressCb | None = None,
) -> dict[str, int]:
    """Index every registered source into mark.

    Returns counts of added/updated/skipped sessions.
    """
    db.init_db()

    # A change of embedding model invalidates all existing vectors.
    current_model = (
        embeddings.get_embedder().name
        if do_embed
        else (db.get_meta("embed_model", "") or "")
    )
    model_changed = do_embed and db.get_meta("embed_model") not in (None, current_model)
    if model_changed:
        rebuild = True

    counts: Counter[str] = Counter()
    with db.transaction() as conn:
        cur = conn.cursor()
        if model_changed:
            cur.execute("DELETE FROM embeddings")
        existing = {
            row["id"]: row["content_hash"]
            for row in cur.execute("SELECT id, content_hash FROM sessions")
        }
        _seed_tombstones(cur, existing)
        src_fps = persist.load_file_signatures(cur, prefix="srcfp:")
        for source in WATCHED_SOURCES:
            cfg = config.resolve_source_config(source.default_config())
            if not cfg.enabled:
                # Non-destructive: keep already-indexed rows, just stop importing.
                continue
            # Skip a source entirely when its own cheap fingerprint is unchanged.
            # A reindex is triggered by ANY source changing (e.g. an active CLI
            # session), so without this every idle source would re-open and
            # re-parse its whole store each pass
            try:
                fp = source.fingerprint(cfg)
            except Exception:
                fp = ""
            fp_key = f"srcfp:{source.key}"
            if not rebuild and fp and src_fps.get(fp_key) == fp:
                continue
            if progress:
                progress(f"Reading {cfg.label or source.key}...")
            res = source.ingest(cur, existing, cfg, rebuild=rebuild, progress=progress)
            counts.update(res)
            if fp:
                persist.record_file_signature(cur, fp_key, fp)
            conn.commit()

    result = {"added": 0, "updated": 0, "skipped": 0}
    result.update(counts)

    changed = bool(result.get("added") or result.get("updated"))
    if changed:
        db.set_meta("last_ingest", datetime.now(timezone.utc).isoformat())

    if do_embed:
        # Only scan for chunks needing vectors when something actually changed
        # (or a rebuild/model switch invalidated them, or a prior embed pass was
        # interrupted). Otherwise every idle sync would run a full window-function
        # scan of the chunks table just to discover there is nothing to do.
        if changed or rebuild or model_changed or db.get_meta("embed_pending") == "1":
            db.set_meta("embed_pending", "1")
            _embed_pending(progress)
            db.set_meta("embed_pending", "0")
        db.set_meta("embed_model", current_model)
    if rebuild:
        # A full rebuild deletes many rows; reclaim the freed pages.
        vac = db.connect()
        try:
            vac.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            vac.execute("VACUUM")
        finally:
            vac.close()

    return result


def import_export(
    filename: str,
    data: bytes,
    *,
    do_embed: bool = True,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Import a user-supplied export file (ChatGPT, ...) into mark.

    Detects which :data:`IMPORT_SOURCES` adapter recognises the bytes, writes one
    session per conversation (dedup by content hash), then embeds new chunks.
    Returns ``{matched, added, updated, skipped, imported}``; ``matched`` is
    ``None`` when no importer claims the file.
    """
    db.init_db()
    src = next((s for s in IMPORT_SOURCES if s.detect(filename, data)), None)
    if src is None:
        return {"matched": None, "added": 0, "updated": 0, "skipped": 0, "imported": 0}

    counts: Counter[str] = Counter()
    with db.transaction() as conn:
        cur = conn.cursor()
        existing = {
            row["id"]: row["content_hash"]
            for row in cur.execute("SELECT id, content_hash FROM sessions")
        }
        _seed_tombstones(cur, existing)
        n = 0
        for session in src.parse_export(data):
            if not session or not session.get("turns"):
                continue
            prior = existing.get(session["id"])
            if prior is not None and prior == session["content_hash"]:
                counts["skipped"] += 1
                continue
            persist.write_session(cur, session)
            counts["added" if prior is None else "updated"] += 1
            n += 1
            if progress and n % 50 == 0:
                progress(f"Imported {n} {src.key} conversations...")
        conn.commit()

    # Embed only when this import actually wrote something (or a prior pass was
    # interrupted), so re-importing an unchanged export doesn't rescan chunks.
    if do_embed and (
        counts["added"] or counts["updated"] or db.get_meta("embed_pending") == "1"
    ):
        db.set_meta("embed_pending", "1")
        _embed_pending(progress)
        db.set_meta("embed_pending", "0")
        db.set_meta("embed_model", embeddings.get_embedder().name)
    db.set_meta("last_ingest", datetime.now(timezone.utc).isoformat())

    return {
        "matched": src.key,
        "added": counts["added"],
        "updated": counts["updated"],
        "skipped": counts["skipped"],
        "imported": counts["added"] + counts["updated"],
    }
