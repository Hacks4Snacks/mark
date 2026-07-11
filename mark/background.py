from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import config, ingest

_state = threading.Condition()
_lifecycle_lock = threading.Lock()
_status: dict[str, Any] = {
    "running": False,
    "queued": False,
    "message": "idle",
    "last_result": None,
    "last_error": None,
    "started_at": None,
    "finished_at": None,
}

_sync_stop = threading.Event()
_worker: threading.Thread | None = None
_sync_worker: threading.Thread | None = None
_pending: _Job | None = None
_active: _Job | None = None
_stopping = False
_last_successful_fingerprint: str | None = None
_retry_required = False
_retry_rebuild = False


@dataclass
class _Job:
    rebuild: bool = False
    fingerprint: str | None = None
    fingerprint_complete: bool = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def status_snapshot() -> dict[str, Any]:
    """Thread-safe copy of the current indexing status."""
    with _state:
        return dict(_status)


def wait_for_idle(timeout: float | None = None) -> bool:
    """Wait until no ingest is running or queued; return ``False`` on timeout."""
    with _state:
        return _state.wait_for(
            lambda: not _status["running"] and _pending is None,
            timeout=timeout,
        )


def _ensure_worker_locked() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = threading.Thread(target=_worker_loop, name="mark-ingest")
    _worker.start()


def _worker_loop() -> None:
    global _active, _last_successful_fingerprint, _pending
    global _retry_rebuild, _retry_required
    while True:
        with _state:
            while _pending is None and not _stopping:
                _state.wait()
            if _stopping:
                return
            job = _pending
            _pending = None
            job.rebuild = job.rebuild or _retry_rebuild
            _active = job
            _status.update(
                running=True,
                queued=False,
                message="Starting...",
                started_at=_now(),
                finished_at=None,
            )

        def progress(msg: str) -> None:
            with _state:
                _status["message"] = msg

        try:
            result = ingest.ingest_all(rebuild=job.rebuild, progress=progress)
            added = result.get("added", 0)
            updated = result.get("updated", 0)
            errors = dict(result.get("errors") or {})
            observed_fingerprint = result.get("fingerprint")
            observed_complete = bool(result.get("fingerprint_complete"))
            has_observed_fingerprint = "fingerprint" in result
            post_snapshot = (
                ingest.sources_fingerprint_snapshot()
                if has_observed_fingerprint
                else None
            )
            if post_snapshot is not None:
                for key, value in post_snapshot.errors.items():
                    errors.setdefault(key, f"post-pass fingerprint: {value}")
            fingerprint_matches = bool(
                post_snapshot is not None
                and observed_complete
                and not post_snapshot.errors
                and observed_fingerprint == post_snapshot.value
            )
            # Coordinator unit tests may substitute a legacy result without
            # fingerprint metadata; production ingest always returns it.
            complete_success = not errors and (
                fingerprint_matches
                if has_observed_fingerprint
                else job.fingerprint_complete
            )
            msg = f"Indexed {added} new, {updated} updated"
            if errors:
                msg += f"; {len(errors)} source error(s)"
            with _state:
                if complete_success:
                    acknowledged = (
                        post_snapshot.value
                        if post_snapshot is not None
                        else job.fingerprint
                    )
                    if acknowledged is not None:
                        _last_successful_fingerprint = acknowledged
                    _retry_required = False
                    _retry_rebuild = False
                elif errors:
                    _retry_required = True
                    _retry_rebuild = _retry_rebuild or job.rebuild
                elif post_snapshot is not None:
                    # Sources changed during the pass. The latest snapshot
                    # supersedes any older queued fingerprint.
                    follow_up = _Job(
                        fingerprint=post_snapshot.value or None,
                        fingerprint_complete=not post_snapshot.errors,
                    )
                    _merge_pending_locked(follow_up)
                    msg += "; source changed, follow-up queued"
                error = "; ".join(f"{key}: {value}" for key, value in errors.items())
                _status.update(
                    last_result=result,
                    message=msg,
                    last_error=(
                        None if complete_success else (error or _status["last_error"])
                    ),
                )
        except Exception as exc:  # surface, don't crash the server
            with _state:
                _retry_required = True
                _retry_rebuild = _retry_rebuild or job.rebuild
                error = str(exc)
                _status.update(
                    last_result=None,
                    message=f"Error: {error}",
                    last_error=error,
                )
        finally:
            with _state:
                _active = None
                _status.update(
                    running=False,
                    queued=_pending is not None,
                    finished_at=_now(),
                )
                _state.notify_all()


def _merge_pending_locked(requested: _Job) -> bool:
    """Merge work into the bounded pending slot; caller holds ``_state``."""
    global _pending
    if _pending is None:
        _pending = requested
        _status["queued"] = True
        return True
    accepted = False
    if requested.rebuild and not _pending.rebuild:
        _pending.rebuild = True
        accepted = True
    if (
        requested.fingerprint is not None
        and requested.fingerprint != _pending.fingerprint
    ):
        _pending.fingerprint = requested.fingerprint
        _pending.fingerprint_complete = requested.fingerprint_complete
        accepted = True
    elif (
        requested.fingerprint is not None
        and requested.fingerprint_complete != _pending.fingerprint_complete
    ):
        _pending.fingerprint_complete = requested.fingerprint_complete
        accepted = True
    _status["queued"] = True
    return accepted


def start_reindex(
    rebuild: bool = False,
    *,
    fingerprint: str | None = None,
    fingerprint_complete: bool = True,
) -> bool:
    """Queue one reindex, coalescing requests while the single worker is busy.

    Returns ``True`` when this request added or upgraded pending work and
    ``False`` when an equivalent request was already queued.
    """
    with _state:
        if _stopping:
            return False
        requested = _Job(
            rebuild=rebuild or _retry_rebuild,
            fingerprint=fingerprint,
            fingerprint_complete=fingerprint_complete,
        )
        if _pending is not None:
            accepted = _merge_pending_locked(requested)
            _ensure_worker_locked()
            _state.notify_all()
            return accepted
        if _active is not None:
            active_covers_rebuild = _active.rebuild or not requested.rebuild
            active_covers_fingerprint = requested.fingerprint is None or (
                requested.fingerprint == _active.fingerprint
                and (_active.fingerprint_complete or not requested.fingerprint_complete)
            )
            if active_covers_rebuild and active_covers_fingerprint:
                return False
        _merge_pending_locked(requested)
        _ensure_worker_locked()
        _state.notify_all()
        return True


def _sync_loop() -> None:
    """Import on startup, then re-import once a source change settles.

    Cheap source fingerprints are compared every ``SYNC_INTERVAL`` seconds, but a
    real (incremental) import only runs after a change has *stabilised* for a
    full interval. A session that is actively being written churns its SQLite
    write-ahead log (and so its fingerprint) on every tick; debouncing on
    stability means we sync it once it pauses or ends rather than re-indexing
    continuously while it is in use — which is what spiked CPU before.
    """
    snapshot = ingest.sources_fingerprint_snapshot()
    start_reindex(
        rebuild=False,
        fingerprint=snapshot.value or None,
        fingerprint_complete=not snapshot.errors,
    )

    pending_fp: str | None = None
    while not _sync_stop.wait(config.SYNC_INTERVAL):
        snapshot = ingest.sources_fingerprint_snapshot()
        fp = snapshot.value
        with _state:
            synced_fp = _last_successful_fingerprint
            retry_required = _retry_required
            retry_rebuild = _retry_rebuild
        if not fp or (fp == synced_fp and not retry_required):
            pending_fp = None  # nothing new (or settled back to the synced state)
            continue
        if fp == pending_fp:
            # Changed, then held steady for a full interval: the source has
            # paused or ended, so an incremental import is now worthwhile.
            pending_fp = None
            start_reindex(
                rebuild=retry_rebuild,
                fingerprint=fp,
                fingerprint_complete=not snapshot.errors,
            )
        else:
            pending_fp = fp  # newly changed (or still changing) — let it settle


def start() -> None:
    """Start background work according to config (auto-sync loop or one import)."""
    global _stopping, _sync_worker
    with _lifecycle_lock:
        with _state:
            _stopping = False
        _sync_stop.clear()
        if config.AUTO_SYNC:
            if _sync_worker is None or not _sync_worker.is_alive():
                _sync_worker = threading.Thread(target=_sync_loop, name="mark-sync")
                _sync_worker.start()
        else:
            # Auto-sync off: still import once on startup so new sessions appear.
            start_reindex(rebuild=False)


def stop() -> None:
    """Stop accepting work and join both background threads before returning."""
    global _pending, _stopping, _sync_worker, _worker
    with _lifecycle_lock:
        _sync_stop.set()
        with _state:
            _stopping = True
            _pending = None
            _status["queued"] = False
            _state.notify_all()
        if _sync_worker is not None and _sync_worker is not threading.current_thread():
            _sync_worker.join()
        if _worker is not None and _worker is not threading.current_thread():
            _worker.join()
        with _state:
            _sync_worker = None
            _worker = None
