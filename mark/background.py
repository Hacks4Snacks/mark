from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

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
    "retry_required": False,
    "retry_attempt": 0,
    "retry_at": None,
    "sync_error": None,
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
_retry_repair_semantic = False
_retry_attempt = 0
_retry_at: float | None = None
_work_ready = threading.Event()

_monotonic = time.monotonic
_random = random.random
_UNSET = object()

AdmissionResult = Literal["accepted", "covered", "stopping"]


@dataclass
class _Job:
    rebuild: bool = False
    fingerprint: str | None = None
    fingerprint_complete: bool = True
    repair_semantic: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_delay(attempt: int) -> float:
    exponent = min(max(0, attempt - 1), 30)
    delay = min(config.SYNC_RETRY_BASE, config.SYNC_RETRY_MAX)
    delay = min(config.SYNC_RETRY_MAX, delay * (2**exponent))
    # One process owns one coordinator, but light jitter still avoids many Mark
    # instances retrying a shared unavailable dependency on the same boundary.
    return min(config.SYNC_RETRY_MAX, delay * (0.9 + 0.2 * _random()))


def _set_retry_deadline_locked(delay: float | None) -> None:
    global _retry_at
    if delay is None:
        _retry_at = None
        _status["retry_at"] = None
        return
    _retry_at = _monotonic() + delay
    _status["retry_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=delay)
    ).isoformat()


def _schedule_retry_locked(job: _Job) -> None:
    """Retain failed work and schedule its next automatic attempt."""
    global _retry_attempt, _retry_rebuild, _retry_repair_semantic, _retry_required
    if _stopping:
        return
    _retry_required = True
    _retry_rebuild = _retry_rebuild or job.rebuild
    _retry_repair_semantic = _retry_repair_semantic or job.repair_semantic
    _retry_attempt += 1
    _status["retry_required"] = True
    _status["retry_attempt"] = _retry_attempt
    _set_retry_deadline_locked(
        _retry_delay(_retry_attempt) if config.AUTO_SYNC else None
    )


def _clear_retry_locked() -> None:
    global _retry_attempt, _retry_rebuild, _retry_repair_semantic, _retry_required
    _retry_required = False
    _retry_rebuild = False
    _retry_repair_semantic = False
    _retry_attempt = 0
    _status["retry_required"] = False
    _status["retry_attempt"] = 0
    _set_retry_deadline_locked(None)


def status_snapshot() -> dict[str, Any]:
    """Thread-safe copy of the current indexing status."""
    with _state:
        snapshot = dict(_status)
        snapshot["sync_worker_alive"] = bool(
            _sync_worker is not None and _sync_worker.is_alive()
        )
        snapshot["ingest_worker_alive"] = bool(
            _worker is not None and _worker.is_alive()
        )
        return snapshot


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


def _finish_job_locked() -> None:
    """Publish one terminal attempt atomically with active-state cleanup."""
    global _active
    _active = None
    _status.update(
        running=False,
        queued=_pending is not None,
        finished_at=_now(),
    )
    _state.notify_all()


def _worker_loop() -> None:
    global _active, _last_successful_fingerprint, _pending
    while True:
        _work_ready.wait()
        with _state:
            while _pending is None and not _stopping:
                _state.wait()
            if _stopping:
                return
            job = _pending
            assert job is not None
            _pending = None
            job.rebuild = job.rebuild or _retry_rebuild
            job.repair_semantic = job.repair_semantic or _retry_repair_semantic
            if _retry_required:
                _set_retry_deadline_locked(None)
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
            has_observed_fingerprint = "fingerprint" in result
            if (
                job.repair_semantic
                and has_observed_fingerprint
                and "semantic_index" not in errors
                and ingest.semantic_repair_needed()
                and not ingest.ensure_index_ready(progress, initialize=False)
            ):
                errors["semantic_index"] = (
                    ingest.semantic_status()["error"] or "embedding failed"
                )
            observed_fingerprint = result.get("fingerprint")
            observed_complete = bool(result.get("fingerprint_complete"))
            post_snapshot = (
                ingest.sources_fingerprint_snapshot()
                if has_observed_fingerprint
                else None
            )
            if post_snapshot is not None:
                for key, value in post_snapshot.errors.items():
                    errors.setdefault(key, f"post-pass fingerprint: {value}")
            result = dict(result)
            result["errors"] = errors
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
                    _clear_retry_locked()
                elif errors:
                    _schedule_retry_locked(job)
                elif post_snapshot is not None:
                    # Sources changed during the pass. The latest snapshot
                    # supersedes any older queued fingerprint.
                    follow_up = _Job(
                        fingerprint=post_snapshot.value,
                        fingerprint_complete=not post_snapshot.errors,
                    )
                    if not _stopping:
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
                _finish_job_locked()
        except Exception as exc:  # surface, don't crash the server
            with _state:
                _schedule_retry_locked(job)
                error = str(exc)
                _status.update(
                    last_result=None,
                    message=f"Error: {error}",
                    last_error=error,
                )
                _finish_job_locked()


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
    if requested.repair_semantic and not _pending.repair_semantic:
        _pending.repair_semantic = True
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


def _admit_locked(requested: _Job, *, manual: bool) -> AdmissionResult:
    """Atomically admit or merge work; caller holds ``_state``."""
    if _stopping:
        return "stopping"
    requested.rebuild = requested.rebuild or _retry_rebuild
    if _pending is not None:
        accepted = _merge_pending_locked(requested)
        if manual:
            _set_retry_deadline_locked(None)
        _ensure_worker_locked()
        _state.notify_all()
        return "accepted" if accepted else "covered"
    if _active is not None:
        active_covers_rebuild = _active.rebuild or not requested.rebuild
        active_covers_semantic = (
            _active.repair_semantic or not requested.repair_semantic
        )
        active_covers_fingerprint = requested.fingerprint is None or (
            requested.fingerprint == _active.fingerprint
            and (_active.fingerprint_complete or not requested.fingerprint_complete)
        )
        if (
            active_covers_rebuild
            and active_covers_semantic
            and active_covers_fingerprint
        ):
            if manual:
                _set_retry_deadline_locked(None)
            return "covered"
    _merge_pending_locked(requested)
    if manual:
        _set_retry_deadline_locked(None)
    _ensure_worker_locked()
    _state.notify_all()
    return "accepted"


def _admit_due_retry_locked(snapshot: ingest.FingerprintSnapshot) -> bool:
    """Claim and admit a due automatic retry in one critical section."""
    if (
        not _retry_required
        or _retry_at is None
        or _monotonic() < _retry_at
        or _stopping
    ):
        return False
    _set_retry_deadline_locked(None)
    return (
        _admit_locked(
            _Job(
                rebuild=_retry_rebuild,
                fingerprint=snapshot.value,
                fingerprint_complete=not snapshot.errors,
                repair_semantic=_retry_repair_semantic,
            ),
            manual=False,
        )
        == "accepted"
    )


def request_reindex(
    rebuild: bool = False,
    *,
    fingerprint: str | None = None,
    fingerprint_complete: bool = True,
    repair_semantic: bool = False,
    manual: bool = True,
) -> AdmissionResult:
    """Atomically classify a reindex request for API and coordinator callers."""
    with _state:
        return _admit_locked(
            _Job(
                rebuild=rebuild,
                fingerprint=fingerprint,
                fingerprint_complete=fingerprint_complete,
                repair_semantic=repair_semantic,
            ),
            manual=manual,
        )


def request_semantic_repair() -> AdmissionResult:
    """Queue repair after any active job so newly written chunks cannot strand."""
    with _state:
        requested = _Job(repair_semantic=True)
        if _stopping:
            return "stopping"
        if _active is not None:
            accepted = _merge_pending_locked(requested)
            _ensure_worker_locked()
            _state.notify_all()
            return "accepted" if accepted else "covered"
        return _admit_locked(requested, manual=False)


def start_reindex(
    rebuild: bool = False,
    *,
    fingerprint: str | None = None,
    fingerprint_complete: bool = True,
    repair_semantic: bool = False,
    manual: bool = True,
) -> bool:
    """Queue one reindex, coalescing requests while the single worker is busy.

    Returns ``True`` when this request added or upgraded pending work and
    ``False`` when an equivalent request was already queued.
    """
    return (
        request_reindex(
            rebuild=rebuild,
            fingerprint=fingerprint,
            fingerprint_complete=fingerprint_complete,
            repair_semantic=repair_semantic,
            manual=manual,
        )
        == "accepted"
    )


def _sync_loop(on_success: Callable[[], None] | None = None) -> None:
    """Import on startup, then re-import once a source change settles.

    Cheap source fingerprints are compared every ``SYNC_INTERVAL`` seconds, but a
    real (incremental) import only runs after a change has *stabilised* for a
    full interval. A session that is actively being written churns its SQLite
    write-ahead log (and so its fingerprint) on every tick; debouncing on
    stability means we sync it once it pauses or ends rather than re-indexing
    continuously while it is in use — which is what spiked CPU before.
    """
    _work_ready.wait()
    snapshot = ingest.sources_fingerprint_snapshot()
    if on_success is not None:
        on_success()
    with _state:
        if _stopping or _sync_stop.is_set():
            return
        _status["sync_error"] = None
        _admit_locked(
            _Job(
                fingerprint=snapshot.value,
                fingerprint_complete=not snapshot.errors,
                repair_semantic=True,
            ),
            manual=False,
        )

    pending_fp: object | str = _UNSET
    while True:
        with _state:
            if _stopping or _sync_stop.is_set():
                return
            delay = float(config.SYNC_INTERVAL)
            if _retry_required and _retry_at is not None:
                delay = min(delay, max(0.0, _retry_at - _monotonic()))
            _state.wait(timeout=delay)
            if _stopping or _sync_stop.is_set():
                return
        snapshot = ingest.sources_fingerprint_snapshot()
        if on_success is not None:
            on_success()
        fp = snapshot.value
        with _state:
            if _stopping or _sync_stop.is_set():
                return
            _status["sync_error"] = None
            synced_fp = _last_successful_fingerprint
            retry_required = _retry_required
            retry_due = _retry_at is not None and _monotonic() >= _retry_at
            if retry_due:
                _admit_due_retry_locked(snapshot)
        if retry_due:
            pending_fp = _UNSET
            continue
        if retry_required:
            pending_fp = _UNSET
            continue
        if fp == synced_fp:
            pending_fp = _UNSET
            continue
        if pending_fp is not _UNSET and fp == pending_fp:
            # Changed, then held steady for a full interval: the source has
            # paused or ended, so an incremental import is now worthwhile.
            pending_fp = None
            with _state:
                _admit_locked(
                    _Job(
                        fingerprint=fp,
                        fingerprint_complete=not snapshot.errors,
                    ),
                    manual=False,
                )
        else:
            pending_fp = fp  # newly changed (or still changing) — let it settle


def _supervised_sync_loop() -> None:
    """Restart coordinator-level sync failures with bounded delay."""
    attempt = 0

    def mark_success() -> None:
        nonlocal attempt
        attempt = 0
        with _state:
            _status["sync_error"] = None

    while True:
        try:
            _sync_loop(mark_success)
            return
        except Exception as exc:
            with _state:
                if _stopping or _sync_stop.is_set():
                    return
                attempt += 1
                delay = _retry_delay(attempt)
                error = str(exc)
                _status.update(
                    sync_error=error,
                    message=f"Sync monitor error: {error}; retrying",
                )
                _state.notify_all()
                if _state.wait_for(
                    lambda: _stopping or _sync_stop.is_set(), timeout=delay
                ):
                    return


def start(*, wait_for_http: bool = False) -> None:
    """Start background work according to config (auto-sync loop or one import)."""
    global _stopping, _sync_worker
    with _lifecycle_lock:
        with _state:
            _stopping = False
        ingest.mark_semantic_unverified()
        if wait_for_http:
            _work_ready.clear()
        else:
            _work_ready.set()
        _sync_stop.clear()
        if config.AUTO_SYNC:
            if _sync_worker is None or not _sync_worker.is_alive():
                _sync_worker = threading.Thread(
                    target=_supervised_sync_loop, name="mark-sync"
                )
                _sync_worker.start()
        else:
            # Auto-sync off: still import once on startup so new sessions appear.
            start_reindex(rebuild=False, repair_semantic=True, manual=False)


def mark_http_ready() -> None:
    """Allow startup work after the first HTTP response has been produced."""
    _work_ready.set()


def stop() -> None:
    """Stop accepting work and join both background threads before returning."""
    global _active, _pending, _stopping, _sync_worker, _worker
    with _lifecycle_lock:
        _sync_stop.set()
        _work_ready.set()
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
            _pending = None
            _active = None
            _clear_retry_locked()
            _status.update(running=False, queued=False, sync_error=None)
            _sync_worker = None
            _worker = None
            _state.notify_all()
