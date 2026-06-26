"""Orchestration: index every registered source into the mindex database.

The per-source readers live in :mod:`mindex.sources`; persistence lives in
:mod:`mindex.persist`. This module just loops the source registry for change
detection (:func:`sources_fingerprint`) and importing (:func:`ingest_all`), then
embeds any new chunks.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from . import config, db, embeddings
from .sources import WATCHED_SOURCES
from .sources.base import ProgressCb

__all__ = ["ingest_all", "sources_fingerprint"]


# --- embeddings (batched) ----------------------------------------------------


def _embed_pending(progress: ProgressCb | None = None, batch: int = 256) -> int:
    """Embed every chunk that does not yet have a vector, in batches."""
    emb = embeddings.get_embedder()
    with db.connect() as conn:
        cur = conn.cursor()
        automation_clause = (
            "" if config.EMBED_AUTOMATION else " AND s.source != 'automation'"
        )
        rows = cur.execute(
            "SELECT c.id, c.session_id, c.content FROM chunks c "
            "JOIN sessions s ON s.id = c.session_id "
            "LEFT JOIN embeddings e ON e.chunk_id = c.id "
            "WHERE e.chunk_id IS NULL" + automation_clause
        ).fetchall()
        total = len(rows)
        for i in range(0, total, batch):
            part = rows[i : i + batch]
            vectors = emb.embed([r["content"] for r in part])
            cur.executemany(
                "INSERT OR REPLACE INTO embeddings(chunk_id, session_id, model, dim, vector) VALUES (?,?,?,?,?)",
                [
                    (r["id"], r["session_id"], emb.name, emb.dim, embeddings.to_blob(v))
                    for r, v in zip(part, vectors)
                ],
            )
            conn.commit()
            if progress:
                progress(f"Embedding {min(i + batch, total)}/{total} chunks…")
    return total


# --- public API --------------------------------------------------------------


def sources_fingerprint() -> str:
    """A cheap signature of all on-disk sources.

    Joins each registered source's stat-only fingerprint so a background loop can
    detect when a session was written or ended without doing a full import.
    Changes whenever any source is added, grows, or is rewritten.
    """
    return "|".join(s.fingerprint() for s in WATCHED_SOURCES)


def ingest_all(
    *,
    rebuild: bool = False,
    do_embed: bool = True,
    progress: ProgressCb | None = None,
) -> dict[str, int]:
    """Index every registered source into mindex.

    Returns counts of added/updated/skipped sessions (plus automation).
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
    with db.connect() as conn:
        cur = conn.cursor()
        if model_changed:
            cur.execute("DELETE FROM embeddings")
        existing = {
            row["id"]: row["content_hash"]
            for row in cur.execute("SELECT id, content_hash FROM sessions")
        }
        for source in WATCHED_SOURCES:
            if progress:
                progress(f"Reading {source.key}…")
            res = source.ingest(cur, existing, rebuild=rebuild, progress=progress)
            counts.update(res)
            conn.commit()

    if do_embed:
        _embed_pending(progress)
        db.set_meta("embed_model", current_model)
    if rebuild:
        # A full rebuild deletes many rows; reclaim the freed pages.
        vac = db.connect()
        try:
            vac.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            vac.execute("VACUUM")
        finally:
            vac.close()
    db.set_meta("last_ingest", datetime.now(timezone.utc).isoformat())

    result = {"added": 0, "updated": 0, "skipped": 0, "automation": 0}
    result.update(counts)
    return result
