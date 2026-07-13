from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from . import attachments, config, db, embeddings, persist
from .sources import IMPORT_SOURCES, WATCHED_SOURCES
from .sources.base import ProgressCb

__all__ = [
    "ensure_index_ready",
    "exclusive_ingest",
    "import_export",
    "ingest_all",
    "sources_fingerprint",
    "sources_fingerprint_snapshot",
]

_ingest_gate = threading.RLock()
_EMBED_ERROR_KEY = "embed_error"


@contextmanager
def exclusive_ingest() -> Iterator[None]:
    """Serialize every operation that creates sessions, chunks, or vectors."""
    with _ingest_gate, embeddings.writer_lock():
        yield


@dataclass(frozen=True)
class FingerprintSnapshot:
    value: str
    errors: dict[str, str]


def _embed_pending(progress: ProgressCb | None = None, batch: int = 256) -> int:
    """Embed chunks that lack a vector, capped to the first N chunks per session.

    Keyword search indexes every chunk, but semantic search loads all vectors into
    memory, so embeddings are bounded per session (earliest chunks — user prompts
    first — win). The cap is applied by chunk rank within each session, so it is
    stable across incremental runs.
    """
    emb = embeddings.get_embedder()
    with embeddings.writer_lock(), db.transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO meta(key, value) VALUES('embed_pending', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        embeddings.prepare_index(cur, emb)
        # Persist the inactive target + retry marker before invoking the model.
        # A first-batch inference failure must not roll compatibility state back.
        conn.commit()
        rows = cur.execute(
            "SELECT r.id, r.session_id, r.content FROM ("
            "  SELECT c.id, c.session_id, c.content, "
            "         ROW_NUMBER() OVER (PARTITION BY c.session_id ORDER BY c.id) AS rn "
            "  FROM chunks c"
            ") r "
            "LEFT JOIN embeddings e ON e.chunk_id = r.id AND e.fingerprint = ? "
            "AND e.model = ? AND e.dim = ? AND length(e.vector) = ? "
            "WHERE e.chunk_id IS NULL AND r.rn <= ?",
            (
                emb.fingerprint,
                emb.name,
                emb.dim,
                emb.dim * 4,
                config.MAX_EMBED_CHUNKS_PER_SESSION,
            ),
        ).fetchall()
        total = len(rows)
        for i in range(0, total, batch):
            part = rows[i : i + batch]
            vectors = emb.embed([r["content"] for r in part])
            expected_shape = (len(part), emb.dim)
            if vectors.shape != expected_shape or not np.isfinite(vectors).all():
                raise RuntimeError(
                    "embedding backend returned invalid vectors: "
                    f"shape {vectors.shape}, expected {expected_shape}"
                )
            cur.executemany(
                "INSERT OR REPLACE INTO embeddings"
                "(chunk_id, session_id, model, dim, fingerprint, vector) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (
                        r["id"],
                        r["session_id"],
                        emb.name,
                        emb.dim,
                        emb.fingerprint,
                        embeddings.to_blob(v),
                    )
                    for r, v in zip(part, vectors, strict=False)
                ],
            )
            embeddings.bump_generation(cur)
            conn.commit()
            if progress:
                progress(f"Embedding {min(i + batch, total)}/{total} chunks...")
        remaining = cur.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT c.id, ROW_NUMBER() OVER "
            "         (PARTITION BY c.session_id ORDER BY c.id) AS rn "
            "  FROM chunks c"
            ") r LEFT JOIN embeddings e ON e.chunk_id = r.id "
            "AND e.fingerprint = ? AND e.model = ? AND e.dim = ? "
            "AND length(e.vector) = ? "
            "WHERE e.chunk_id IS NULL AND r.rn <= ?",
            (
                emb.fingerprint,
                emb.name,
                emb.dim,
                emb.dim * 4,
                config.MAX_EMBED_CHUNKS_PER_SESSION,
            ),
        ).fetchone()[0]
        if remaining == 0 and not embeddings.index_is_active(cur, emb):
            embeddings.activate_index(cur, emb)
            conn.commit()
        elif remaining == 0:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES('embed_pending', '0') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            conn.commit()
    return total


def _try_embed_pending(progress: ProgressCb | None = None) -> bool:
    """Resume semantic work without turning a durable content write into failure."""
    try:
        _embed_pending(progress)
    except Exception as exc:
        db.set_meta("embed_pending", "1")
        db.set_meta(_EMBED_ERROR_KEY, str(exc))
        return False
    db.set_meta(_EMBED_ERROR_KEY, "")
    return True


def ensure_index_ready(
    progress: ProgressCb | None = None, *, initialize: bool = True
) -> bool:
    """Prepare or resume the active embedder's index for web/MCP startup."""
    with exclusive_ingest():
        if initialize:
            db.init_db()
        try:
            embedder = embeddings.get_embedder()
        except Exception as exc:
            db.set_meta("embed_pending", "1")
            db.set_meta(_EMBED_ERROR_KEY, str(exc))
            return False
        with db.transaction() as conn:
            cur = conn.cursor()
            embeddings.prepare_index(cur, embedder)
            active = embeddings.index_is_active(cur, embedder)
            pending = cur.execute(
                "SELECT value FROM meta WHERE key = 'embed_pending'"
            ).fetchone()
            missing = cur.execute(
                "SELECT 1 FROM ("
                "  SELECT c.id, ROW_NUMBER() OVER "
                "         (PARTITION BY c.session_id ORDER BY c.id) AS rn "
                "  FROM chunks c"
                ") r LEFT JOIN embeddings e ON e.chunk_id = r.id "
                "AND e.fingerprint = ? AND e.model = ? AND e.dim = ? "
                "AND length(e.vector) = ? "
                "WHERE e.chunk_id IS NULL AND r.rn <= ? LIMIT 1",
                (
                    embedder.fingerprint,
                    embedder.name,
                    embedder.dim,
                    embedder.dim * 4,
                    config.MAX_EMBED_CHUNKS_PER_SESSION,
                ),
            ).fetchone()
            if missing is not None:
                embeddings.mark_index_dirty(cur)
        if active and missing is None and (pending is None or pending["value"] != "1"):
            return True
        return _try_embed_pending(progress)


def semantic_status() -> dict[str, Any]:
    embedder = None
    runtime_error: str | None = None
    try:
        embedder = embeddings.get_embedder()
    except Exception as exc:
        runtime_error = str(exc)
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        cur = conn.cursor()
        fingerprint, generation = embeddings.index_state(cur)
        target = embeddings.target_fingerprint(cur)
        pending = cur.execute(
            "SELECT value FROM meta WHERE key = 'embed_pending'"
        ).fetchone()
        error = cur.execute(
            "SELECT value FROM meta WHERE key = ?", (_EMBED_ERROR_KEY,)
        ).fetchone()
    finally:
        conn.close()
    return {
        "active": bool(embedder and fingerprint == embedder.fingerprint),
        "model": (
            embedder.name if embedder and fingerprint == embedder.fingerprint else ""
        ),
        "fingerprint": fingerprint,
        "target_fingerprint": target,
        "generation": generation,
        "pending": bool(target) or bool(pending and pending["value"] == "1"),
        "error": runtime_error
        or (error["value"] if error and error["value"] else None),
    }


def sources_fingerprint() -> str:
    """A cheap signature of all enabled on-disk sources.

    Joins each enabled source's stat-only fingerprint so a background loop can
    detect when a session was written or ended without doing a full import.
    Changes whenever any source is added, grows, is rewritten, or is toggled.
    Disabled sources contribute nothing (and so never trigger a sync).
    """
    return sources_fingerprint_snapshot().value


def sources_fingerprint_snapshot() -> FingerprintSnapshot:
    """Aggregate enabled source signatures without one broken source blocking all."""
    parts: list[str] = []
    errors: dict[str, str] = {}
    for s in WATCHED_SOURCES:
        try:
            cfg = config.resolve_source_config(s.default_config())
            if not cfg.enabled:
                continue
            parts.append(f"{s.key}={s.fingerprint(cfg)}")
        except Exception as exc:
            errors[s.key] = str(exc)
            parts.append(f"{s.key}=!error")
    return FingerprintSnapshot("|".join(parts), errors)


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
) -> dict[str, Any]:
    """Index every registered source into mark.

    Returns counts of added/updated/skipped sessions.
    """
    with exclusive_ingest():
        return _ingest_all(rebuild=rebuild, do_embed=do_embed, progress=progress)


def _ingest_all(
    *,
    rebuild: bool,
    do_embed: bool,
    progress: ProgressCb | None,
) -> dict[str, Any]:
    db.init_db()

    counts: Counter[str] = Counter()
    source_results: dict[str, dict[str, Any]] = {}
    source_errors: dict[str, str] = {}
    fingerprint_parts: list[str] = []
    fingerprint_errors: dict[str, str] = {}
    with db.transaction() as conn:
        cur = conn.cursor()
        existing = {
            row["id"]: row["content_hash"]
            for row in cur.execute("SELECT id, content_hash FROM sessions")
        }
        _seed_tombstones(cur, existing)
        src_fps = persist.load_file_signatures(cur, prefix="srcfp:")
        for index, source in enumerate(WATCHED_SOURCES):
            savepoint = f"source_{index}"
            fp_key = f"srcfp:{source.key}"
            fingerprint_recorded = False
            cur.execute(f"SAVEPOINT {savepoint}")
            try:
                cfg = config.resolve_source_config(source.default_config())
                if not cfg.enabled:
                    source_results[source.key] = {"status": "disabled"}
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                    continue
                # Skip a source entirely when its own cheap fingerprint is
                # unchanged. A pass triggered by another active source should
                # not reopen and reparse every idle store.
                fingerprint_error: str | None = None
                try:
                    fp = source.fingerprint(cfg)
                    fingerprint_parts.append(f"{source.key}={fp}")
                    fingerprint_recorded = True
                except Exception as exc:
                    fp = ""
                    fingerprint_error = f"fingerprint: {exc}"
                    fingerprint_errors[source.key] = str(exc)
                    fingerprint_parts.append(f"{source.key}=!error")
                    fingerprint_recorded = True
                if not rebuild and fp and src_fps.get(fp_key) == fp:
                    source_results[source.key] = {"status": "unchanged"}
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                    continue
                if progress:
                    progress(f"Reading {cfg.label or source.key}...")
                res = source.ingest(
                    cur, existing, cfg, rebuild=rebuild, progress=progress
                )
                if fp:
                    persist.record_file_signature(cur, fp_key, fp)
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                counts.update(res)
                if fingerprint_error:
                    source_errors[source.key] = fingerprint_error
                    source_results[source.key] = {
                        "status": "degraded",
                        "error": fingerprint_error,
                        **res,
                    }
                else:
                    source_results[source.key] = {"status": "ok", **res}
            except Exception as exc:
                cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                # Invalidate a prior source-level acknowledgement outside the
                # rolled-back savepoint so an unchanged source is retried.
                cur.execute("DELETE FROM source_file_stat WHERE path = ?", (fp_key,))
                error = str(exc)
                source_errors[source.key] = error
                source_results[source.key] = {"status": "error", "error": error}
                if not fingerprint_recorded:
                    fingerprint_errors[source.key] = error
                    fingerprint_parts.append(f"{source.key}=!error")
                if progress:
                    progress(f"Error reading {source.key}: {error}")
            conn.commit()

    # Database references are now authoritative; reclaim snapshots replaced by
    # reingest and any orphan captures left by a failed adapter/savepoint.
    attachments.cleanup_unreferenced()

    result: dict[str, Any] = {"added": 0, "updated": 0, "skipped": 0}
    result.update(counts)
    result["sources"] = source_results
    result["errors"] = source_errors
    result["fingerprint"] = "|".join(fingerprint_parts)
    result["fingerprint_complete"] = not fingerprint_errors

    changed = bool(result.get("added") or result.get("updated"))
    if changed:
        db.set_meta("last_ingest", datetime.now(timezone.utc).isoformat())

    # Only scan for chunks needing vectors when something actually changed
    # (or a rebuild/model switch invalidated them, or a prior embed pass was
    # interrupted). Otherwise every idle sync would run a full window-function
    # scan of the chunks table just to discover there is nothing to do.
    if (
        do_embed
        and (changed or rebuild or db.get_meta("embed_pending") == "1")
        and not _try_embed_pending(progress)
    ):
        source_errors["semantic_index"] = (
            db.get_meta(_EMBED_ERROR_KEY) or "embedding failed"
        )
        result["errors"] = source_errors
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
    with exclusive_ingest():
        return _import_export(
            filename,
            data,
            do_embed=do_embed,
            progress=progress,
        )


def _import_export(
    filename: str,
    data: bytes,
    *,
    do_embed: bool,
    progress: ProgressCb | None,
) -> dict[str, Any]:
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
            persist._write_session(cur, session)
            counts["added" if prior is None else "updated"] += 1
            n += 1
            if progress and n % 50 == 0:
                progress(f"Imported {n} {src.key} conversations...")
        conn.commit()

    attachments.cleanup_unreferenced()

    # Embed only when this import actually wrote something (or a prior pass was
    # interrupted), so re-importing an unchanged export doesn't rescan chunks.
    if do_embed and (
        counts["added"] or counts["updated"] or db.get_meta("embed_pending") == "1"
    ):
        _try_embed_pending(progress)
    db.set_meta("last_ingest", datetime.now(timezone.utc).isoformat())

    return {
        "matched": src.key,
        "added": counts["added"],
        "updated": counts["updated"],
        "skipped": counts["skipped"],
        "imported": counts["added"] + counts["updated"],
    }
