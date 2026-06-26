from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from . import config, ingest

_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "running": False,
    "message": "idle",
    "last_result": None,
    "started_at": None,
    "finished_at": None,
}

_sync_stop = threading.Event()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def status_snapshot() -> dict[str, Any]:
    """Thread-safe copy of the current indexing status."""
    with _status_lock:
        return dict(_status)


def _run_reindex(rebuild: bool) -> None:
    with _status_lock:
        if _status["running"]:
            return
        _status.update(
            running=True, message="Starting...", started_at=_now(), finished_at=None
        )

    def progress(msg: str) -> None:
        with _status_lock:
            _status["message"] = msg

    try:
        result = ingest.ingest_all(rebuild=rebuild, progress=progress)
        added = result.get("added", 0)
        updated = result.get("updated", 0)
        msg = f"Indexed {added} new, {updated} updated"
        with _status_lock:
            _status.update(last_result=result, message=msg)
    except Exception as exc:  # surface, don't crash the server
        with _status_lock:
            _status["message"] = f"Error: {exc}"
    finally:
        with _status_lock:
            _status.update(running=False, finished_at=_now())


def start_reindex(rebuild: bool = False) -> bool:
    """Start a reindex in a daemon thread; ``False`` if one is already running."""
    with _status_lock:
        if _status["running"]:
            return False
    threading.Thread(target=_run_reindex, args=(rebuild,), daemon=True).start()
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
    try:
        last_fp = ingest.sources_fingerprint()
    except Exception:
        last_fp = ""
    start_reindex(rebuild=False)  # pick up anything new since last launch

    pending_fp: str | None = None
    while not _sync_stop.wait(config.SYNC_INTERVAL):
        try:
            fp = ingest.sources_fingerprint()
        except Exception:
            continue
        if not fp or fp == last_fp:
            pending_fp = None  # nothing new (or settled back to the synced state)
            continue
        if fp == pending_fp:
            # Changed, then held steady for a full interval: the source has
            # paused or ended, so an incremental import is now worthwhile.
            last_fp = fp
            pending_fp = None
            start_reindex(rebuild=False)
        else:
            pending_fp = fp  # newly changed (or still changing) — let it settle


def start() -> None:
    """Start background work according to config (auto-sync loop or one import)."""
    _sync_stop.clear()
    if config.AUTO_SYNC:
        threading.Thread(target=_sync_loop, daemon=True).start()
    else:
        # Auto-sync off: still import once on startup so new sessions appear.
        start_reindex(rebuild=False)


def stop() -> None:
    """Signal the auto-sync loop to exit."""
    _sync_stop.set()
