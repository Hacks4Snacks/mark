from __future__ import annotations

import threading
from pathlib import Path

import pytest

from scripts.package_smoke import verify_web_assets


def test_all_referenced_web_assets_are_served(client):
    from mark import config

    checked = verify_web_assets(client, config.WEB_DIR)

    assert "/fonts/inter-400.woff2" in checked
    assert "/icons/og.png" in checked
    assert "/js/views/detail.js" in checked


def test_app_lifespan_defers_semantic_repair_until_after_readiness(monkeypatch):
    from fastapi.testclient import TestClient

    from mark import background, db, ingest
    from mark.app import create_app

    calls = []
    monkeypatch.setattr(
        ingest,
        "ensure_index_ready",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("semantic repair ran during lifespan")
        ),
    )
    monkeypatch.setattr(
        background,
        "start",
        lambda **kwargs: calls.append(("start", kwargs)),
    )
    monkeypatch.setattr(background, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(background, "mark_http_ready", lambda: calls.append("ready"))

    with TestClient(create_app()) as client:
        assert client.get("/api/status").status_code == 200
        assert db.get_meta("embed_pending") is None
        assert calls == [("start", {"wait_for_http": True}), "ready"]
    assert calls == [("start", {"wait_for_http": True}), "ready", "stop"]


def test_first_http_response_releases_startup_worker(monkeypatch):
    from fastapi.testclient import TestClient

    from mark import background
    from mark.app import create_app

    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(background.config, "AUTO_SYNC", False)

    def ingest_all(**kwargs):
        started.set()
        assert release.wait(2)
        return {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": {},
            "fingerprint": "",
            "fingerprint_complete": True,
        }

    monkeypatch.setattr(background.ingest, "ingest_all", ingest_all)
    monkeypatch.setattr(background.ingest, "semantic_repair_needed", lambda: False)
    monkeypatch.setattr(
        background.ingest,
        "sources_fingerprint_snapshot",
        lambda: background.ingest.FingerprintSnapshot("", {}),
    )

    with TestClient(create_app()) as app_client:
        assert not started.is_set()
        assert app_client.get("/api/status").status_code == 200
        assert started.wait(2)
        release.set()
        assert background.wait_for_idle(2)


@pytest.fixture
def ingest_coordinator():
    """Reset the module-level coordinator around each direct state-machine test."""
    from mark import background

    background.stop()
    with background._state:
        background._stopping = False
        background._pending = None
        background._active = None
        background._last_successful_fingerprint = None
        background._retry_required = False
        background._retry_rebuild = False
        background._retry_repair_semantic = False
        background._retry_attempt = 0
        background._retry_at = None
        background._status.update(
            running=False,
            queued=False,
            message="idle",
            last_result=None,
            last_error=None,
            started_at=None,
            finished_at=None,
            retry_required=False,
            retry_attempt=0,
            retry_at=None,
            sync_error=None,
        )
    yield background
    background.stop()


def test_ingest_coordinator_coalesces_follow_up_and_rebuild(
    ingest_coordinator, monkeypatch
):
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    active = 0
    max_active = 0
    lock = threading.Lock()
    timed_out = threading.Event()

    def fake_ingest_all(*, rebuild, progress):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(rebuild)
            call_number = len(calls)
        try:
            progress(f"run {call_number}")
            if call_number == 1:
                first_started.set()
                if not release_first.wait(2):
                    timed_out.set()
            return {"added": call_number, "updated": 0, "skipped": 0}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)

    assert ingest_coordinator.start_reindex(fingerprint="before") is True
    assert first_started.wait(2)
    assert ingest_coordinator.start_reindex(fingerprint="before") is False
    assert ingest_coordinator.start_reindex(fingerprint="after") is True
    assert ingest_coordinator.start_reindex(fingerprint="after") is False
    assert ingest_coordinator.start_reindex(rebuild=True, fingerprint="after") is True

    queued = ingest_coordinator.status_snapshot()
    assert queued["running"] is True
    assert queued["queued"] is True

    release_first.set()
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == [False, True]
    assert max_active == 1
    assert not timed_out.is_set()
    assert ingest_coordinator._last_successful_fingerprint == "after"
    status = ingest_coordinator.status_snapshot()
    assert status["running"] is False
    assert status["queued"] is False
    assert status["last_error"] is None
    assert status["last_result"]["added"] == 2


def test_ingest_coordinator_pending_tracks_latest_fingerprint(
    ingest_coordinator, monkeypatch
):
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            release_first.wait(2)
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    assert ingest_coordinator.start_reindex(fingerprint="A") is True
    assert first_started.wait(2)
    assert ingest_coordinator.start_reindex(fingerprint="B") is True
    assert ingest_coordinator.start_reindex(fingerprint="A") is True

    release_first.set()
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == 2
    assert ingest_coordinator._last_successful_fingerprint == "A"


def test_ingest_coordinator_acknowledges_post_pass_source_state(
    ingest_coordinator, monkeypatch
):
    calls = 0
    snapshots = iter(
        [
            ingest_coordinator.ingest.FingerprintSnapshot("B", {}),
            ingest_coordinator.ingest.FingerprintSnapshot("B", {}),
        ]
    )

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        observed = "A" if calls == 1 else "B"
        return {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "sources": {},
            "errors": {},
            "fingerprint": observed,
            "fingerprint_complete": True,
        }

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: next(snapshots),
    )

    assert ingest_coordinator.start_reindex(fingerprint="A") is True
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == 2
    assert ingest_coordinator._last_successful_fingerprint == "B"


def test_ingest_coordinator_admits_one_identical_first_request(
    ingest_coordinator, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(3)
    accepted = []

    def fake_ingest_all(*, rebuild, progress):
        entered.set()
        release.wait(2)
        return {"added": 0, "updated": 0, "skipped": 0}

    def submit():
        barrier.wait()
        accepted.append(ingest_coordinator.start_reindex(fingerprint="same"))

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    callers = [threading.Thread(target=submit) for _ in range(2)]
    for caller in callers:
        caller.start()
    barrier.wait()
    assert entered.wait(2)
    for caller in callers:
        caller.join()
    assert sorted(accepted) == [False, True]
    release.set()
    assert ingest_coordinator.wait_for_idle(2)


def test_ingest_coordinator_acknowledges_fingerprint_only_after_success(
    ingest_coordinator, monkeypatch
):
    calls = 0

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("source unavailable")
        return {"added": 0, "updated": 1, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)

    assert ingest_coordinator.start_reindex(fingerprint="changed") is True
    assert ingest_coordinator.wait_for_idle(2)
    assert ingest_coordinator._last_successful_fingerprint is None
    failed = ingest_coordinator.status_snapshot()
    assert failed["last_error"] == "source unavailable"

    assert ingest_coordinator.start_reindex(fingerprint="changed") is True
    assert ingest_coordinator.wait_for_idle(2)
    assert ingest_coordinator._last_successful_fingerprint == "changed"
    assert ingest_coordinator.status_snapshot()["last_error"] is None


def test_ingest_coordinator_does_not_ack_partial_source_failure(
    ingest_coordinator, monkeypatch
):
    def fake_ingest_all(*, rebuild, progress):
        return {
            "added": 1,
            "updated": 0,
            "skipped": 0,
            "sources": {
                "healthy": {"status": "ok", "added": 1},
                "broken": {"status": "error", "error": "cannot read"},
            },
            "errors": {"broken": "cannot read"},
        }

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    assert ingest_coordinator.start_reindex(fingerprint="mixed") is True
    assert ingest_coordinator.wait_for_idle(2)

    assert ingest_coordinator._last_successful_fingerprint is None
    status = ingest_coordinator.status_snapshot()
    assert status["last_error"] == "broken: cannot read"
    assert status["last_result"]["sources"]["healthy"]["status"] == "ok"


def test_ingest_coordinator_does_not_ack_incomplete_fingerprint(
    ingest_coordinator, monkeypatch
):
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "ingest_all",
        lambda **kwargs: {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "sources": {},
            "errors": {},
        },
    )

    assert (
        ingest_coordinator.start_reindex(
            fingerprint="healthy=changed|broken=!error",
            fingerprint_complete=False,
        )
        is True
    )
    assert ingest_coordinator.wait_for_idle(2)
    assert ingest_coordinator._last_successful_fingerprint is None


def test_ingest_coordinator_retries_failed_unchanged_rebuild(
    ingest_coordinator, monkeypatch
):
    calls = []

    def fake_ingest_all(*, rebuild, progress):
        calls.append(rebuild)
        return {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "sources": {
                "source": (
                    {"status": "error", "error": "failed"}
                    if len(calls) == 1
                    else {"status": "ok"}
                )
            },
            "errors": {"source": "failed"} if len(calls) == 1 else {},
            "fingerprint": "same",
            "fingerprint_complete": True,
        }

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: ingest_coordinator.ingest.FingerprintSnapshot("same", {}),
    )

    assert ingest_coordinator.start_reindex(rebuild=True, fingerprint="same") is True
    assert ingest_coordinator.wait_for_idle(2)
    assert ingest_coordinator._retry_required is True
    assert ingest_coordinator._retry_rebuild is True

    assert ingest_coordinator.start_reindex(fingerprint="same") is True
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == [True, True]
    assert ingest_coordinator._retry_required is False
    assert ingest_coordinator._last_successful_fingerprint == "same"


def test_ingest_coordinator_retry_backoff_increases_and_caps(
    ingest_coordinator, monkeypatch
):
    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_BASE", 2.0)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_MAX", 5.0)
    monkeypatch.setattr(ingest_coordinator, "_monotonic", lambda: 100.0)
    monkeypatch.setattr(ingest_coordinator, "_random", lambda: 0.5)

    with ingest_coordinator._state:
        job = ingest_coordinator._Job(rebuild=True, fingerprint="same")
        ingest_coordinator._schedule_retry_locked(job)
        assert ingest_coordinator._retry_attempt == 1
        assert ingest_coordinator._retry_at == 102.0
        assert ingest_coordinator._retry_rebuild is True

        ingest_coordinator._schedule_retry_locked(job)
        assert ingest_coordinator._retry_attempt == 2
        assert ingest_coordinator._retry_at == 104.0

        ingest_coordinator._schedule_retry_locked(job)
        assert ingest_coordinator._retry_attempt == 3
        assert ingest_coordinator._retry_at == 105.0
        assert ingest_coordinator._status["retry_attempt"] == 3

        ingest_coordinator._clear_retry_locked()
        assert ingest_coordinator._retry_required is False
        assert ingest_coordinator._retry_attempt == 0
        assert ingest_coordinator._retry_at is None


def test_ingest_coordinator_manual_retry_bypasses_backoff(
    ingest_coordinator, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    rebuilds = []

    def fake_ingest_all(*, rebuild, progress):
        rebuilds.append(rebuild)
        entered.set()
        release.wait(2)
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_BASE", 60.0)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_MAX", 60.0)
    monkeypatch.setattr(ingest_coordinator, "_random", lambda: 0.5)
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    with ingest_coordinator._state:
        ingest_coordinator._schedule_retry_locked(
            ingest_coordinator._Job(rebuild=True, fingerprint="same")
        )
        assert ingest_coordinator._retry_at is not None

    assert ingest_coordinator.start_reindex(fingerprint="same") is True
    assert entered.wait(2)
    with ingest_coordinator._state:
        assert ingest_coordinator._retry_at is None
        assert ingest_coordinator._retry_required is True
    release.set()
    assert ingest_coordinator.wait_for_idle(2)
    assert rebuilds == [True]
    assert ingest_coordinator._retry_required is False


def test_ingest_coordinator_manual_and_due_retry_admit_one_job(
    ingest_coordinator, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(3)
    rebuilds = []
    accepted = []

    def fake_ingest_all(*, rebuild, progress):
        rebuilds.append(rebuild)
        entered.set()
        release.wait(2)
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator, "_monotonic", lambda: 100.0)
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    with ingest_coordinator._state:
        ingest_coordinator._retry_required = True
        ingest_coordinator._retry_rebuild = True
        ingest_coordinator._retry_attempt = 1
        ingest_coordinator._set_retry_deadline_locked(0)

    def admit_manual():
        barrier.wait()
        accepted.append(ingest_coordinator.start_reindex())

    def admit_automatic():
        barrier.wait()
        with ingest_coordinator._state:
            accepted.append(
                ingest_coordinator._admit_due_retry_locked(
                    ingest_coordinator.ingest.FingerprintSnapshot("same", {})
                )
            )

    callers = [
        threading.Thread(target=admit_manual),
        threading.Thread(target=admit_automatic),
    ]
    for caller in callers:
        caller.start()
    barrier.wait()
    assert entered.wait(2)
    for caller in callers:
        caller.join()
    release.set()
    assert ingest_coordinator.wait_for_idle(2)
    assert accepted.count(True) == 1
    assert rebuilds == [True]
    assert ingest_coordinator._pending is None


def test_ingest_coordinator_failed_terminal_state_cannot_strand_manual_retry(
    ingest_coordinator, monkeypatch
):
    finishing = threading.Event()
    release_finish = threading.Event()
    second_finished = threading.Event()
    calls = 0
    admission = []
    real_finish = ingest_coordinator._finish_job_locked

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "added": 0,
                "updated": 0,
                "skipped": 0,
                "errors": {"source": "offline"},
            }
        second_finished.set()
        return {"added": 0, "updated": 1, "skipped": 0}

    def paused_finish():
        if calls == 1:
            finishing.set()
            release_finish.wait(2)
        real_finish()

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    monkeypatch.setattr(ingest_coordinator, "_finish_job_locked", paused_finish)

    assert ingest_coordinator.start_reindex(fingerprint="same") is True
    assert finishing.wait(2)
    requester = threading.Thread(
        target=lambda: admission.append(
            ingest_coordinator.request_reindex(fingerprint="same")
        )
    )
    requester.start()
    assert requester.is_alive()
    release_finish.set()
    requester.join(timeout=2)
    assert not requester.is_alive()
    assert admission == ["accepted"]
    assert second_finished.wait(2)
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == 2
    assert ingest_coordinator._retry_required is False
    assert ingest_coordinator._pending is None


def test_ingest_coordinator_automatically_retries_empty_fingerprint(
    ingest_coordinator, monkeypatch
):
    calls = 0
    recovered = threading.Event()

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "added": 0,
                "updated": 0,
                "skipped": 0,
                "errors": {"semantic_index": "offline"},
                "fingerprint": "",
                "fingerprint_complete": True,
            }
        recovered.set()
        return {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": {},
            "fingerprint": "",
            "fingerprint_complete": True,
        }

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_INTERVAL", 60)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_BASE", 0.01)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_MAX", 0.01)
    monkeypatch.setattr(ingest_coordinator, "_random", lambda: 0.5)
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: ingest_coordinator.ingest.FingerprintSnapshot("", {}),
    )

    ingest_coordinator.start()
    assert recovered.wait(2)
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == 2
    assert ingest_coordinator._last_successful_fingerprint == ""
    assert ingest_coordinator._retry_required is False
    assert ingest_coordinator.status_snapshot()["retry_attempt"] == 0


def test_ingest_coordinator_promotes_queued_work_after_rebuild_failure(
    ingest_coordinator, monkeypatch
):
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    snapshots = iter(
        [
            ingest_coordinator.ingest.FingerprintSnapshot("B", {}),
            ingest_coordinator.ingest.FingerprintSnapshot("B", {}),
        ]
    )

    def fake_ingest_all(*, rebuild, progress):
        calls.append(rebuild)
        if len(calls) == 1:
            first_started.set()
            release_first.wait(2)
            return {
                "added": 0,
                "updated": 0,
                "skipped": 0,
                "sources": {"source": {"status": "error", "error": "failed"}},
                "errors": {"source": "failed"},
                "fingerprint": "A",
                "fingerprint_complete": True,
            }
        return {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "sources": {"source": {"status": "ok"}},
            "errors": {},
            "fingerprint": "B",
            "fingerprint_complete": True,
        }

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: next(snapshots),
    )

    assert ingest_coordinator.start_reindex(rebuild=True, fingerprint="A") is True
    assert first_started.wait(2)
    assert ingest_coordinator.start_reindex(fingerprint="B") is True
    release_first.set()
    assert ingest_coordinator.wait_for_idle(2)

    assert calls == [True, True]
    assert ingest_coordinator._retry_rebuild is False
    assert ingest_coordinator._last_successful_fingerprint == "B"


def test_ingest_coordinator_status_tracks_latest_attempt(
    ingest_coordinator, monkeypatch
):
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"added": 1, "updated": 0, "skipped": 0}
        if calls == 2:
            raise RuntimeError("latest failure")
        first_started.set()
        release_first.wait(2)
        return {"added": 0, "updated": 1, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    assert ingest_coordinator.start_reindex() is True
    assert ingest_coordinator.wait_for_idle(2)
    assert ingest_coordinator.status_snapshot()["last_result"]["added"] == 1

    assert ingest_coordinator.start_reindex() is True
    assert ingest_coordinator.wait_for_idle(2)
    failed = ingest_coordinator.status_snapshot()
    assert failed["last_result"] is None
    assert failed["last_error"] == "latest failure"

    assert ingest_coordinator.start_reindex() is True
    assert first_started.wait(2)
    running = ingest_coordinator.status_snapshot()
    assert running["last_result"] is None
    assert running["last_error"] == "latest failure"
    release_first.set()
    assert ingest_coordinator.wait_for_idle(2)
    succeeded = ingest_coordinator.status_snapshot()
    assert succeeded["last_result"]["updated"] == 1
    assert succeeded["last_error"] is None


def test_ingest_coordinator_records_post_pass_errors_in_result(
    ingest_coordinator, monkeypatch
):
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "ingest_all",
        lambda **kwargs: {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "sources": {"healthy": {"status": "ok"}},
            "errors": {},
            "fingerprint": "healthy=A",
            "fingerprint_complete": True,
        },
    )
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: ingest_coordinator.ingest.FingerprintSnapshot(
            "healthy=A|broken=!error", {"broken": "offline"}
        ),
    )

    assert ingest_coordinator.start_reindex(fingerprint="healthy=A") is True
    assert ingest_coordinator.wait_for_idle(2)
    status = ingest_coordinator.status_snapshot()
    assert status["last_result"]["errors"] == {
        "broken": "post-pass fingerprint: offline"
    }
    assert status["last_error"] == "broken: post-pass fingerprint: offline"
    assert status["retry_required"] is True


def test_ingest_coordinator_stop_joins_active_worker(ingest_coordinator, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    timed_out = threading.Event()

    def fake_ingest_all(*, rebuild, progress):
        started.set()
        if not release.wait(2):
            timed_out.set()
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    assert ingest_coordinator.start_reindex() is True
    assert started.wait(2)

    stopped = threading.Event()

    def stop():
        ingest_coordinator.stop()
        stopped.set()

    stopper = threading.Thread(target=stop)
    stopper.start()
    with ingest_coordinator._state:
        assert ingest_coordinator._state.wait_for(
            lambda: ingest_coordinator._stopping, timeout=2
        )
    assert not stopped.is_set()
    release.set()
    assert stopped.wait(2)
    stopper.join()
    assert not timed_out.is_set()
    assert ingest_coordinator._worker is None


def test_ingest_coordinator_stop_drops_changed_follow_up(
    ingest_coordinator, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    restarted = threading.Event()
    calls = 0

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            release.wait(2)
            return {
                "added": 0,
                "updated": 0,
                "skipped": 0,
                "errors": {},
                "fingerprint": "A",
                "fingerprint_complete": True,
            }
        restarted.set()
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", False)
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: ingest_coordinator.ingest.FingerprintSnapshot("B", {}),
    )
    assert ingest_coordinator.start_reindex(fingerprint="A") is True
    assert started.wait(2)

    stopped = threading.Event()
    stopper = threading.Thread(
        target=lambda: (ingest_coordinator.stop(), stopped.set())
    )
    stopper.start()
    with ingest_coordinator._state:
        assert ingest_coordinator._state.wait_for(
            lambda: ingest_coordinator._stopping, timeout=2
        )
    release.set()
    assert stopped.wait(2)
    stopper.join()
    status = ingest_coordinator.status_snapshot()
    assert ingest_coordinator._pending is None
    assert ingest_coordinator._active is None
    assert status["queued"] is False
    assert status["running"] is False

    ingest_coordinator.start()
    assert restarted.wait(2)
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == 2


def test_ingest_coordinator_stop_during_initial_fingerprint_is_prompt(
    ingest_coordinator, monkeypatch
):
    fingerprint_started = threading.Event()
    release_fingerprint = threading.Event()
    stopped = threading.Event()
    ingest_calls = 0

    def fingerprint():
        fingerprint_started.set()
        release_fingerprint.wait(2)
        return ingest_coordinator.ingest.FingerprintSnapshot("stable", {})

    def fake_ingest_all(**kwargs):
        nonlocal ingest_calls
        ingest_calls += 1
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_INTERVAL", 60)
    monkeypatch.setattr(
        ingest_coordinator.ingest, "sources_fingerprint_snapshot", fingerprint
    )
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)

    ingest_coordinator.start()
    assert fingerprint_started.wait(2)
    stopper = threading.Thread(
        target=lambda: (ingest_coordinator.stop(), stopped.set())
    )
    stopper.start()
    with ingest_coordinator._state:
        assert ingest_coordinator._state.wait_for(
            lambda: ingest_coordinator._stopping, timeout=2
        )
    release_fingerprint.set()
    assert stopped.wait(2)
    stopper.join()
    assert ingest_calls == 0


def test_ingest_coordinator_sync_monitor_recovers_after_failure(
    ingest_coordinator, monkeypatch
):
    fingerprint_failed = threading.Event()
    recovered = threading.Event()
    observed_errors = []
    calls = 0

    def fingerprint():
        nonlocal calls
        calls += 1
        if calls == 1:
            fingerprint_failed.set()
            raise RuntimeError("monitor unavailable")
        return ingest_coordinator.ingest.FingerprintSnapshot("stable", {})

    def fake_ingest_all(**kwargs):
        recovered.set()
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_INTERVAL", 60)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_BASE", 0.01)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_RETRY_MAX", 0.01)
    monkeypatch.setattr(ingest_coordinator, "_random", lambda: 0.5)
    real_wait_for = ingest_coordinator._state.wait_for

    def record_wait_for(predicate, timeout=None):
        if ingest_coordinator._status["sync_error"]:
            observed_errors.append(ingest_coordinator._status["sync_error"])
        return real_wait_for(predicate, timeout)

    monkeypatch.setattr(ingest_coordinator._state, "wait_for", record_wait_for)
    monkeypatch.setattr(
        ingest_coordinator.ingest, "sources_fingerprint_snapshot", fingerprint
    )
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)

    ingest_coordinator.start()
    assert fingerprint_failed.wait(2)
    assert recovered.wait(2)
    assert ingest_coordinator.wait_for_idle(2)
    status = ingest_coordinator.status_snapshot()
    assert "monitor unavailable" in observed_errors
    assert status["sync_error"] is None
    assert status["sync_worker_alive"] is True


def test_ingest_coordinator_sync_monitor_backoff_resets_after_success(
    ingest_coordinator, monkeypatch
):
    calls = 0
    attempts = []
    second_failure = threading.Event()
    recovered = threading.Event()

    def fingerprint():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first failure")
        if calls == 3:
            second_failure.set()
            raise RuntimeError("second failure")
        return ingest_coordinator.ingest.FingerprintSnapshot("stable", {})

    def retry_delay(attempt):
        attempts.append(attempt)
        return 0.01

    def fake_ingest_all(**kwargs):
        if calls >= 4:
            recovered.set()
        return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(ingest_coordinator.config, "SYNC_INTERVAL", 0.01)
    monkeypatch.setattr(ingest_coordinator, "_retry_delay", retry_delay)
    monkeypatch.setattr(
        ingest_coordinator.ingest, "sources_fingerprint_snapshot", fingerprint
    )
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)

    ingest_coordinator.start()
    assert second_failure.wait(2)
    assert recovered.wait(2)
    assert ingest_coordinator.wait_for_idle(2)
    assert attempts[:2] == [1, 1]


def test_ingest_coordinator_auto_sync_off_waits_for_manual_retry(
    ingest_coordinator, monkeypatch
):
    calls = 0

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("offline")
        return {"added": 0, "updated": 1, "skipped": 0}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", False)
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)

    ingest_coordinator.start()
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == 1
    assert ingest_coordinator._sync_worker is None
    status = ingest_coordinator.status_snapshot()
    assert status["retry_required"] is True
    assert status["retry_at"] is None

    assert ingest_coordinator.start_reindex() is True
    assert ingest_coordinator.wait_for_idle(2)
    assert calls == 2
    assert ingest_coordinator.status_snapshot()["retry_required"] is False


def test_ingest_coordinator_runs_startup_semantic_repair_in_worker(
    ingest_coordinator, monkeypatch
):
    repair_started = threading.Event()
    release_repair = threading.Event()

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", False)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "ingest_all",
        lambda **kwargs: {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": {},
            "fingerprint": "stable",
            "fingerprint_complete": True,
        },
    )
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: ingest_coordinator.ingest.FingerprintSnapshot("stable", {}),
    )
    monkeypatch.setattr(
        ingest_coordinator.ingest, "semantic_repair_needed", lambda: True
    )

    def repair(progress, *, initialize):
        assert initialize is False
        repair_started.set()
        assert release_repair.wait(2)
        return True

    monkeypatch.setattr(ingest_coordinator.ingest, "ensure_index_ready", repair)

    ingest_coordinator.start()
    assert repair_started.wait(2)
    assert ingest_coordinator.status_snapshot()["running"] is True
    release_repair.set()
    assert ingest_coordinator.wait_for_idle(2)
    assert ingest_coordinator.status_snapshot()["last_error"] is None


def test_ingest_coordinator_retry_preserves_semantic_repair(
    ingest_coordinator, monkeypatch
):
    calls = 0
    repairs = 0

    def ingest_all(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("initial source failure")
        return {
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": {},
            "fingerprint": "stable",
            "fingerprint_complete": True,
        }

    def repair(*args, **kwargs):
        nonlocal repairs
        repairs += 1
        return True

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", False)
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", ingest_all)
    monkeypatch.setattr(
        ingest_coordinator.ingest, "semantic_repair_needed", lambda: True
    )
    monkeypatch.setattr(ingest_coordinator.ingest, "ensure_index_ready", repair)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: ingest_coordinator.ingest.FingerprintSnapshot("stable", {}),
    )

    ingest_coordinator.start()
    assert ingest_coordinator.wait_for_idle(2)
    assert ingest_coordinator._retry_repair_semantic is True
    assert ingest_coordinator.start_reindex() is True
    assert ingest_coordinator.wait_for_idle(2)
    assert repairs == 1
    assert ingest_coordinator._retry_repair_semantic is False


def test_ingest_coordinator_auto_sync_stops_and_restarts(
    ingest_coordinator, monkeypatch
):
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    calls = 0

    def fake_ingest_all(*, rebuild, progress):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            release_first.wait(2)
        else:
            second_started.set()
        return {"added": 0, "updated": 0, "skipped": 0, "errors": {}}

    monkeypatch.setattr(ingest_coordinator.config, "AUTO_SYNC", True)
    monkeypatch.setattr(
        ingest_coordinator.ingest,
        "sources_fingerprint_snapshot",
        lambda: ingest_coordinator.ingest.FingerprintSnapshot("stable", {}),
    )
    monkeypatch.setattr(ingest_coordinator.ingest, "ingest_all", fake_ingest_all)

    ingest_coordinator.start()
    assert first_started.wait(2)
    with ingest_coordinator._state:
        first_sync_worker = ingest_coordinator._sync_worker
        first_ingest_worker = ingest_coordinator._worker
    assert first_sync_worker is not None
    assert first_ingest_worker is not None

    stopped = threading.Event()
    stopper = threading.Thread(
        target=lambda: (ingest_coordinator.stop(), stopped.set())
    )
    stopper.start()
    with ingest_coordinator._state:
        assert ingest_coordinator._state.wait_for(
            lambda: ingest_coordinator._stopping, timeout=2
        )
    release_first.set()
    assert stopped.wait(2)
    stopper.join(timeout=2)
    assert not stopper.is_alive()
    assert not first_sync_worker.is_alive()
    assert not first_ingest_worker.is_alive()

    ingest_coordinator.start()
    assert second_started.wait(2)
    assert ingest_coordinator.wait_for_idle(2)
    with ingest_coordinator._state:
        second_sync_worker = ingest_coordinator._sync_worker
        second_ingest_worker = ingest_coordinator._worker
    assert second_sync_worker is not None and second_sync_worker.is_alive()
    assert second_ingest_worker is not None and second_ingest_worker.is_alive()
    assert second_sync_worker is not first_sync_worker
    assert second_ingest_worker is not first_ingest_worker

    ingest_coordinator.stop()
    assert not second_sync_worker.is_alive()
    assert not second_ingest_worker.is_alive()


def test_read_endpoints_ok(client):
    for path in [
        "/api/stats",
        "/api/status",
        "/api/facets",
        "/api/sources",
        "/api/usage",
        "/api/snippets",
        "/api/snippets/languages",
        "/api/collections",
        "/api/ask/status",
    ]:
        assert client.get(path).status_code == 200, path


def test_status_and_reindex_response_contract(client, monkeypatch):
    from mark import background

    status = client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert "started" not in body
    assert isinstance(body["sync_worker_alive"], bool)
    assert isinstance(body["ingest_worker_alive"], bool)

    monkeypatch.setattr(background, "request_reindex", lambda **kwargs: "accepted")
    response = client.post("/api/reindex")
    assert response.status_code == 200
    assert response.json()["started"] is True
    assert response.json()["admission"] == "accepted"

    # Admission is authoritative even when covered work finishes before the
    # separate status snapshot is serialized.
    monkeypatch.setattr(background, "request_reindex", lambda **kwargs: "covered")
    response = client.post("/api/reindex")
    assert response.status_code == 200
    assert response.json()["started"] is False
    assert response.json()["admission"] == "covered"
    assert response.json()["running"] is False
    assert response.json()["queued"] is False


def test_ask_enabled_exposes_routes(client):
    # The `client` fixture enables the Ask feature, so its routes are mounted
    # and /api/status advertises it.
    assert client.get("/api/status").json()["ask_enabled"] is True
    assert client.get("/api/ask/status").status_code == 200


def test_api_note_returns_before_semantic_backfill(client):
    response = client.post(
        "/api/notes", json={"title": "Status", "text": "semantic status body"}
    )
    assert response.status_code == 200
    status = client.get("/api/status").json()
    assert status["semantic_pending"] is True
    assert status["semantic_active"] is False
    assert status["semantic"] is False


def test_ask_disabled_by_default_hides_routes(monkeypatch):
    # With the feature flag off (the shipped default) the ask routes are not
    # mounted and the collection-scoped ask is guarded at request time.
    from fastapi.testclient import TestClient

    from mark import background, config
    from mark.app import create_app

    monkeypatch.setattr(background, "start", lambda **kwargs: None)
    monkeypatch.setattr(background, "stop", lambda: None)
    monkeypatch.setattr(background, "mark_http_ready", lambda: None)
    monkeypatch.setattr(config, "ENABLE_ASK", False)

    with TestClient(create_app()) as c:
        assert c.get("/api/status").json()["ask_enabled"] is False
        # The ask routes are not mounted, so requests fall through to the static
        # mount: it 404s unknown GETs and 405s the methods it doesn't serve.
        assert c.get("/api/ask/status").status_code == 404
        assert c.post("/api/ask", json={"question": "hi"}).status_code in (404, 405)
        # Collection-scoped ask stays mounted but is guarded at request time.
        cid = c.post("/api/collections", json={"name": "Flagless"}).json()["id"]
        r = c.post(f"/api/collections/{cid}/ask", json={"question": "hi"})
        assert r.status_code == 404


def test_render_endpoint():
    # Uses the module directly so it doesn't need the client fixture's lifespan.
    from mark import render

    html = render.render_markdown("# Title\n\nsome **bold** text")
    assert "<h1>" in html and "<strong>" in html


def test_note_create_then_searchable(client):
    r = client.post(
        "/api/notes", json={"title": "Note", "text": "hello searchable world"}
    )
    assert r.status_code == 200
    sid = r.json()["id"]

    res = client.get("/api/search", params={"q": "searchable"}).json()
    assert any(x["id"] == sid for x in res["results"])

    detail = client.get(f"/api/sessions/{sid}")
    assert detail.status_code == 200
    assert detail.json()["title"] == "Note"


def test_note_and_render_fields_are_bounded(client):
    from mark import config

    note = client.post(
        "/api/notes",
        json={"title": "N", "text": "x" * (config.MAX_NOTE_TEXT_CHARS + 1)},
    )
    title = client.post(
        "/api/notes",
        json={"title": "x" * (config.MAX_NOTE_TITLE_CHARS + 1), "text": "body"},
    )
    render = client.post(
        "/api/render",
        json={"text": "x" * (config.MAX_RENDER_TEXT_CHARS + 1)},
    )

    assert note.status_code == 422
    assert title.status_code == 422
    assert render.status_code == 422


def test_search_api_requires_all_selected_topics(client):
    both = client.post(
        "/api/notes", json={"title": "Both", "text": "topic api probe"}
    ).json()["id"]
    alpha_only = client.post(
        "/api/notes", json={"title": "Alpha", "text": "topic api probe"}
    ).json()["id"]
    for sid, tags in ((both, ("alpha", "beta")), (alpha_only, ("alpha",))):
        for tag in tags:
            assert (
                client.post(f"/api/sessions/{sid}/tags", json={"tag": tag}).status_code
                == 200
            )

    response = client.get(
        "/api/search", params={"q": "topic api probe", "tags": "alpha,beta"}
    )

    assert response.status_code == 200
    assert {result["id"] for result in response.json()["results"]} == {both}


def test_note_write_succeeds_when_semantic_backfill_fails(client, monkeypatch):
    from mark import background, embeddings

    monkeypatch.setattr(
        embeddings,
        "get_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("model loaded in request")),
    )
    repairs = []
    monkeypatch.setattr(
        background, "request_semantic_repair", lambda: repairs.append(True)
    )
    response = client.post(
        "/api/notes",
        json={"title": "Durable", "text": "saved despite embedding failure"},
    )
    assert response.status_code == 200
    assert repairs == [True]
    sid = response.json()["id"]
    assert client.get(f"/api/sessions/{sid}").status_code == 200
    status = client.get("/api/status").json()
    assert status["semantic_pending"] is True


def test_status_does_not_initialize_embedding_model(client, monkeypatch):
    from mark import embeddings

    monkeypatch.setattr(
        embeddings,
        "get_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("model loaded by status")),
    )

    assert client.get("/api/status").status_code == 200


def test_session_detail_paginates_rendered_turns(client, make_session, persist_session):
    session = make_session(sid="paged-detail")
    session["turns"] = [
        {
            "turn_index": index,
            "user_message": f"question {index}",
            "assistant_response": f"answer {index}",
            "thinking": None,
            "tools": ["search"] if index == 2 else [],
            "timestamp": f"2026-01-01T00:00:0{index}+00:00",
            "files": [],
            "urls": [],
            "code_blocks": [],
        }
        for index in range(5)
    ]
    persist_session(session)

    detail = client.get("/api/sessions/paged-detail", params={"turns_limit": 2}).json()
    assert [turn["turn_index"] for turn in detail["turns"]] == [0, 1]
    assert detail["turns_offset"] == 0
    assert detail["turns_limit"] == 2
    assert detail["has_more_turns"] is True
    assert "question 0" in detail["turns"][0]["user_html"]
    assert "user_message" not in detail["turns"][0]
    assert "assistant_response" not in detail["turns"][0]

    page = client.get(
        "/api/sessions/paged-detail/turns", params={"offset": 2, "limit": 2}
    ).json()
    assert [turn["turn_index"] for turn in page["turns"]] == [2, 3]
    assert page["turns"][0]["tools"] == ["search"]
    assert page["total"] == 5
    assert page["has_more"] is True

    final_page = client.get(
        "/api/sessions/paged-detail/turns", params={"offset": 4, "limit": 2}
    ).json()
    assert [turn["turn_index"] for turn in final_page["turns"]] == [4]
    assert final_page["has_more"] is False
    assert (
        client.get(
            "/api/sessions/paged-detail", params={"turns_limit": 101}
        ).status_code
        == 422
    )


def test_session_detail_defers_oversized_turn(
    client, make_session, persist_session, monkeypatch
):
    from mark import config

    monkeypatch.setattr(config, "DETAIL_INLINE_TURN_CHARS", 1_000)
    session = make_session(
        sid="deferred-detail",
        user="x" * 100_000,
        asst="large response",
    )
    persist_session(session)

    response = client.get("/api/sessions/deferred-detail")
    turn = response.json()["turns"][0]
    assert turn["deferred"] is True
    assert turn["content_chars"] > 100_000
    assert "user_html" not in turn
    assert "user_message" not in turn
    assert len(response.content) < 5_000
    assert len(response.json()["summary"]) <= config.DETAIL_SUMMARY_CHARS + 3

    loaded = client.get("/api/sessions/deferred-detail/turns/0").json()
    assert loaded["deferred"] is False
    assert "x" * 100 in loaded["user_html"]
    assert "user_message" not in loaded
    assert client.get("/api/sessions/deferred-detail/turns/99").status_code == 404


def test_session_detail_defers_oversized_document(client, monkeypatch):
    from mark import config, uploads

    monkeypatch.setattr(config, "DETAIL_INLINE_TURN_CHARS", 1_000)
    sid = uploads.add_note("Large note", "x" * 100_000)

    detail_response = client.get(f"/api/sessions/{sid}")
    document = detail_response.json()["document"]
    assert document["deferred"] is True
    assert document["content"] is None
    assert document["content_chars"] == 100_000
    assert len(detail_response.content) < 5_000

    loaded = client.get(f"/api/sessions/{sid}/document")
    assert loaded.status_code == 200
    assert "x" * 100 in loaded.json()["html"]

    exported = client.get(f"/api/sessions/{sid}/export.md")
    assert exported.status_code == 200
    assert "x" * 1_000 in exported.text


def test_document_pagination_uses_actual_turn_rows(client):
    from mark import uploads

    sid = uploads.add_note("Document", "body", do_embed=False)

    detail = client.get(f"/api/sessions/{sid}").json()
    page = client.get(f"/api/sessions/{sid}/turns").json()

    assert detail["turn_count"] == 1
    assert detail["turns_total"] == 0
    assert detail["turns"] == []
    assert detail["has_more_turns"] is False
    assert page["total"] == 0
    assert page["has_more"] is False


def test_session_detail_metadata_is_counted_and_pageable(
    client, make_session, persist_session, monkeypatch
):
    import hashlib

    from mark import config

    monkeypatch.setattr(config, "DETAIL_FILE_LIMIT", 1)
    monkeypatch.setattr(config, "DETAIL_LINK_LIMIT", 1)
    monkeypatch.setattr(config, "DETAIL_ATTACHMENT_LIMIT", 1)
    session = make_session(sid="paged-metadata")
    session["turns"][0]["files"] = ["/repo/a.py", "/repo/b.py"]
    session["turns"][0]["urls"] = ["https://a.example", "https://b.example"]
    session["attachments"] = [
        {
            "filename": filename,
            "stored_path": None,
            "mime": "text/plain",
            "size_bytes": len(content),
            "content": content,
            "storage_kind": "inline",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "capture_version": 2,
        }
        for filename, content in (("a.txt", "alpha"), ("b.txt", "beta"))
    ]
    persist_session(session)

    detail = client.get("/api/sessions/paged-metadata").json()
    assert (len(detail["files"]), detail["files_total"]) == (1, 2)
    assert (len(detail["refs"]), detail["refs_total"]) == (1, 2)
    assert (len(detail["attachments"]), detail["attachments_total"]) == (1, 2)

    files = client.get(
        "/api/sessions/paged-metadata/files", params={"offset": 1, "limit": 1}
    ).json()
    refs = client.get(
        "/api/sessions/paged-metadata/refs", params={"offset": 1, "limit": 1}
    ).json()
    attachments_page = client.get(
        "/api/sessions/paged-metadata/attachments",
        params={"offset": 1, "limit": 1},
    ).json()

    assert files["items"][0]["file_path"] == "/repo/b.py"
    assert refs["items"][0]["ref_value"] == "https://b.example"
    assert attachments_page["items"][0]["filename"] == "b.txt"
    assert attachments_page["items"][0]["content"] is None
    assert attachments_page["has_more"] is False
    overflow_id = attachments_page["items"][0]["id"]
    assert (
        client.get(
            f"/api/sessions/paged-metadata/attachments/{overflow_id}"
        ).status_code
        == 200
    )


def test_nul_prefixed_document_is_deferred(client, monkeypatch):
    from mark import config, uploads

    monkeypatch.setattr(config, "DETAIL_INLINE_TURN_CHARS", 1_000)
    sid = uploads.add_note("NUL", "\x00" + "x" * 10_000, do_embed=False)

    detail = client.get(f"/api/sessions/{sid}").json()

    assert detail["document"]["deferred"] is True
    assert detail["document"]["content_chars"] > 10_000
    assert len(client.get(f"/api/sessions/{sid}").content) < 5_000


def test_nul_prefixed_turn_is_deferred(
    client, make_session, persist_session, monkeypatch
):
    from mark import config, db

    monkeypatch.setattr(config, "DETAIL_INLINE_TURN_CHARS", 1_000)
    persist_session(make_session(sid="nul-turn", user="placeholder"))
    with db.cursor() as cur:
        cur.execute(
            "UPDATE turns SET user_message = ? WHERE session_id = ?",
            ("\x00" + "x" * 100_000, "nul-turn"),
        )

    detail_response = client.get("/api/sessions/nul-turn")
    turn = detail_response.json()["turns"][0]

    assert turn["deferred"] is True
    assert turn["content_chars"] > 100_000
    assert "user_html" not in turn
    assert len(detail_response.content) < 5_000


def test_deferred_turn_text_is_not_rendered_until_exact_load(
    client, make_session, persist_session, monkeypatch
):
    from mark import config, render

    monkeypatch.setattr(config, "DETAIL_INLINE_TURN_CHARS", 1_000)
    persist_session(make_session(sid="sql-deferred", user="x" * 100_000))
    rendered = []

    def record_render(text):
        rendered.append(len(text or ""))
        return "<p>rendered</p>"

    monkeypatch.setattr(render, "render_markdown", record_render)

    detail = client.get("/api/sessions/sql-deferred")
    assert detail.status_code == 200
    assert detail.json()["turns"][0]["deferred"] is True
    assert rendered == []

    loaded = client.get("/api/sessions/sql-deferred/turns/0")
    assert loaded.status_code == 200
    assert max(rendered) == 100_000


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/paged-detail/turns?offset=9223372036854775808",
        "/api/sessions/paged-detail/turns/9223372036854775808",
        "/api/sessions/paged-detail/turns/-1",
    ],
)
def test_session_turn_integer_bounds_are_validated(
    client, make_session, persist_session, path
):
    persist_session(make_session(sid="paged-detail"))
    assert client.get(path).status_code == 422


def test_missing_session_is_404(client):
    assert client.get("/api/sessions/nope").status_code == 404


def test_attachment_download_uses_immutable_snapshot(
    client, make_session, persist_session, tmp_path
):
    from mark import attachments

    workspace = tmp_path / "repo"
    workspace.mkdir()
    original = workspace / "artifact.bin"
    original_bytes = b"\x00captured binary\xff"
    original.write_bytes(original_bytes)
    attachment = attachments.snapshot_file(
        str(original), workspace=str(workspace), session_id="attachment-session"
    )
    assert attachment is not None
    session = make_session(sid="attachment-session")
    session["attachments"] = [attachment]
    persist_session(session)

    doc_id = client.get("/api/sessions/attachment-session").json()["attachments"][0][
        "id"
    ]
    detail_attachment = client.get("/api/sessions/attachment-session").json()[
        "attachments"
    ][0]
    assert "stored_path" not in detail_attachment
    assert detail_attachment["category"] == "agent"
    assert detail_attachment["downloadable"] is True
    assert detail_attachment["content_available"] is True
    assert detail_attachment["content"] is None
    lazy_content = client.get(f"/api/sessions/attachment-session/attachments/{doc_id}")
    assert lazy_content.status_code == 404
    original.write_bytes(b"changed live file")
    response = client.get(
        f"/api/sessions/attachment-session/attachments/{doc_id}/download"
    )
    assert response.status_code == 200
    assert response.content == original_bytes

    snapshot = attachments.managed_snapshot(
        attachment["stored_path"],
        sha256=attachment["sha256"],
        size_bytes=attachment["size_bytes"],
    )
    assert snapshot is not None
    snapshot.unlink()
    unavailable = client.get(
        f"/api/sessions/attachment-session/attachments/{doc_id}/download"
    )
    assert unavailable.status_code == 404
    assert unavailable.json()["detail"] == (
        "attachment content was not captured or is no longer available"
    )


def test_attachment_download_rejects_legacy_live_path(
    client, make_session, persist_session, tmp_path
):
    live = tmp_path / "legacy-secret.bin"
    live.write_bytes(b"must never be served")
    session = make_session(sid="legacy-attachment")
    session["attachments"] = [
        {
            "filename": live.name,
            "stored_path": str(live),
            "mime": "application/octet-stream",
            "size_bytes": live.stat().st_size,
            "content": None,
        }
    ]
    persist_session(session)

    doc_id = client.get("/api/sessions/legacy-attachment").json()["attachments"][0][
        "id"
    ]
    response = client.get(
        f"/api/sessions/legacy-attachment/attachments/{doc_id}/download"
    )
    assert response.status_code == 404
    assert response.content != live.read_bytes()


def test_attachment_download_rejects_legacy_inline_content(
    client, make_session, persist_session
):
    session = make_session(sid="legacy-inline")
    session["attachments"] = [
        {
            "filename": "legacy-secret.txt",
            "stored_path": "/tmp/legacy-secret.txt",
            "mime": "text/plain",
            "size_bytes": len("legacy secret"),
            "content": "legacy secret",
        }
    ]
    persist_session(session)

    detail = client.get("/api/sessions/legacy-inline").json()
    attachment = detail["attachments"][0]
    assert attachment["content"] is None
    assert "stored_path" not in attachment
    assert attachment["downloadable"] is False
    assert attachment["content_available"] is False
    response = client.get(
        f"/api/sessions/legacy-inline/attachments/{attachment['id']}/download"
    )
    assert response.status_code == 404
    assert b"legacy secret" not in response.content


def test_attachment_content_is_rendered_only_when_requested(
    client, make_session, persist_session
):
    import hashlib

    text = "# Lazy attachment\n\nRendered on demand."
    session = make_session(sid="lazy-attachment")
    session["attachments"] = [
        {
            "filename": "note.md",
            "stored_path": None,
            "mime": "text/markdown",
            "size_bytes": len(text.encode()),
            "content": text,
            "storage_kind": "inline",
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "capture_version": 2,
        }
    ]
    persist_session(session)

    detail_response = client.get("/api/sessions/lazy-attachment")
    attachment = detail_response.json()["attachments"][0]
    assert attachment["content"] is None
    assert "html" not in attachment
    assert b"Rendered on demand" not in detail_response.content

    content = client.get(
        f"/api/sessions/lazy-attachment/attachments/{attachment['id']}"
    )
    assert content.status_code == 200
    assert "<h1>Lazy attachment</h1>" in content.json()["html"]


def test_attachment_download_rejects_corrupted_snapshot(
    client, make_session, persist_session, tmp_path
):
    from mark import attachments

    workspace = tmp_path / "repo"
    workspace.mkdir()
    original = workspace / "artifact.bin"
    original.write_bytes(b"captured bytes")
    attachment = attachments.snapshot_file(
        str(original), workspace=str(workspace), session_id="corrupt-session"
    )
    assert attachment is not None
    session = make_session(sid="corrupt-session")
    session["attachments"] = [attachment]
    persist_session(session)
    Path(attachment["stored_path"]).write_bytes(b"tampered bytes")

    doc_id = client.get("/api/sessions/corrupt-session").json()["attachments"][0]["id"]
    response = client.get(
        f"/api/sessions/corrupt-session/attachments/{doc_id}/download"
    )
    assert response.status_code == 404


def test_collection_crud_and_membership(client):
    sid = client.post("/api/notes", json={"title": "N", "text": "body"}).json()["id"]

    created = client.post("/api/collections", json={"name": "My collection"})
    assert created.status_code == 200
    cid = created.json()["id"]

    assert client.get(f"/api/collections/{cid}").status_code == 200

    add = client.post(f"/api/collections/{cid}/members", json={"session_id": sid})
    assert add.status_code == 200
    assert add.json()["count"] == 1

    remove = client.delete(f"/api/collections/{cid}/members/{sid}")
    assert remove.status_code == 200

    patched = client.patch(
        f"/api/collections/{cid}",
        json={
            "rule": {"q": "body", "source": "upload"},
            "color": "cyan",
            "pinned": True,
        },
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["rule"]["q"] == "body"
    assert body["color"] == "cyan"
    assert body["pinned"] is True

    assert client.delete(f"/api/collections/{cid}").status_code == 200
    assert client.get(f"/api/collections/{cid}").status_code == 404


@pytest.mark.parametrize(
    "rule",
    [
        {"unknown": True},
        {"q": "x", "mode": "invalid"},
        {"q": "x", "sort": "invalid"},
        {"date_from": "2026-02-01", "date_to": "2026-01-01"},
        {"tags": ["x" * 41]},
        {"tags": [f"tag-{index}" for index in range(21)]},
        {"q": "x", "limit": 1000},
    ],
)
def test_collection_rule_rejects_invalid_contracts(client, rule):
    response = client.post("/api/collections", json={"name": "Invalid", "rule": rule})

    assert response.status_code == 422


def test_collection_member_state_is_typed(client):
    sid = client.post("/api/notes", json={"title": "N", "text": "body"}).json()["id"]
    cid = client.post("/api/collections", json={"name": "C"}).json()["id"]

    response = client.post(
        f"/api/collections/{cid}/members",
        json={"session_id": sid, "state": "unknown"},
    )

    assert response.status_code == 422


def test_collection_members_are_paginated(client):
    from mark import collections, db

    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO sessions(id, source, title, hidden) "
            "VALUES (?, 'upload', ?, 0)",
            ((f"member-{index}", f"Member {index}") for index in range(205)),
        )
    cid = collections.create("Paged")
    for index in range(205):
        collections.set_member(cid, f"member-{index}")

    first = client.get(f"/api/collections/{cid}").json()
    second = client.get(
        f"/api/collections/{cid}",
        params={"members_offset": 100, "members_limit": 100},
    ).json()
    last = client.get(
        f"/api/collections/{cid}",
        params={"members_offset": 200, "members_limit": 100},
    ).json()

    assert first["count"] == 205
    assert len(first["members"]) == 100
    assert first["has_more_members"] is True
    assert len(second["members"]) == 100
    assert second["has_more_members"] is True
    assert len(last["members"]) == 5
    assert last["has_more_members"] is False


def test_collection_member_sort_is_global_across_pages(client):
    from mark import collections, db

    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO sessions(id, source, title, hidden) "
            "VALUES (?, 'upload', ?, 0)",
            ((f"sorted-{index:03d}", f"{204 - index:03d}") for index in range(205)),
        )
    cid = collections.create("Sorted")
    for index in range(205):
        collections.set_member(cid, f"sorted-{index:03d}")

    first = client.get(
        f"/api/collections/{cid}",
        params={"members_sort": "title", "members_limit": 100},
    ).json()
    second = client.get(
        f"/api/collections/{cid}",
        params={
            "members_sort": "title",
            "members_offset": 100,
            "members_limit": 100,
            "include_overview": False,
        },
    ).json()

    titles = [member["title"] for member in first["members"] + second["members"]]
    assert titles == sorted(titles)
    assert second["overview"] is None


def test_collection_recent_sort_normalizes_timestamp_offsets(client):
    from mark import collections, db

    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO sessions(id, source, title, updated_at, hidden) "
            "VALUES (?, 'upload', ?, ?, 0)",
            [
                ("older", "Older", "2026-07-14T00:30:00+02:00"),
                ("newer", "Newer", "2026-07-13T23:30:00Z"),
            ],
        )
    cid = collections.create("Chronological")
    collections.set_member(cid, "older")
    collections.set_member(cid, "newer")

    response = client.get(
        f"/api/collections/{cid}", params={"members_sort": "recent"}
    ).json()

    assert [member["id"] for member in response["members"]] == ["newer", "older"]


def test_collection_rule_sort_round_trips_on_patch(client):
    created = client.post(
        "/api/collections",
        json={"name": "Sorted", "rule": {"repo": "repo", "sort": "title"}},
    ).json()

    patched = client.patch(
        f"/api/collections/{created['id']}",
        json={
            "name": "Renamed",
            "rule": created["rule"],
        },
    ).json()

    assert patched["rule"]["sort"] == "title"


def test_global_and_collection_ask_fields_are_bounded(client):
    from mark import config

    too_long = "x" * (config.MAX_ASK_QUESTION_CHARS + 1)
    global_ask = client.post("/api/ask", json={"question": too_long})
    bad_limit = client.post(
        "/api/ask",
        json={"question": "valid", "limit": config.MAX_ASK_SESSION_LIMIT + 1},
    )
    cid = client.post("/api/collections", json={"name": "C"}).json()["id"]
    collection_ask = client.post(
        f"/api/collections/{cid}/ask", json={"question": too_long}
    )

    assert global_ask.status_code == 422
    assert bad_limit.status_code == 422
    assert collection_ask.status_code == 422


def test_add_and_remove_tag(client):
    sid = client.post("/api/notes", json={"title": "Tagged", "text": "content"}).json()[
        "id"
    ]

    r = client.post(f"/api/sessions/{sid}/tags", json={"tag": "My Topic"})
    assert r.status_code == 200
    assert r.json()["tag"] == "my topic"

    detail = client.get(f"/api/sessions/{sid}").json()
    assert "my topic" in detail["tags"]

    assert client.delete(f"/api/sessions/{sid}/tags/my%20topic").status_code == 200
    detail = client.get(f"/api/sessions/{sid}").json()
    assert "my topic" not in detail["tags"]
