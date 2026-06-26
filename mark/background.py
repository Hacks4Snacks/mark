"""Background indexing: manual reindex + continuous auto-sync.

The web app kicks off imports off the request path. A single shared status
record (guarded by a lock) is surfaced through ``/api/status`` so the UI can
show progress, and an optional auto-sync loop cheaply fingerprints the on-disk
sources every few seconds and only triggers a real (incremental) import when
something actually changed.
"""

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
    """Import on startup, then re-import whenever a session changes or ends.

    Cheap source fingerprints are compared every ``SYNC_INTERVAL`` seconds; a
    real (incremental) import only runs when something actually changed, so an
    idle machine does almost no work.
    """
    try:
        last_fp = ingest.sources_fingerprint()
    except Exception:
        last_fp = ""
    start_reindex(rebuild=False)  # pick up anything new since last launch

    while not _sync_stop.wait(config.SYNC_INTERVAL):
        try:
            fp = ingest.sources_fingerprint()
        except Exception:
            continue
        if fp and fp != last_fp:
            last_fp = fp
            start_reindex(rebuild=False)


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
