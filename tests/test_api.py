from __future__ import annotations

import threading
from pathlib import Path

import pytest


def test_all_packaged_web_assets_are_served(client):
    from mark import config

    checked = set()
    for source in sorted(path for path in config.WEB_DIR.rglob("*") if path.is_file()):
        relative = source.relative_to(config.WEB_DIR).as_posix()
        url = "/" if relative == "index.html" else f"/{relative}"
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.content == source.read_bytes(), url
        checked.add(url)

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


@pytest.fixture
def health_source(tmp_path, monkeypatch):
    from mark import config, ingest
    from mark.sources.base import WatchedSource

    root = tmp_path / "source-store"
    root.mkdir()

    class HealthSource(WatchedSource):
        key = "health-fixture"
        row_sources = ("health-fixture",)
        enabled = True
        fail = False
        bad_config = False
        scans = 0
        fingerprint_value = "fixture-v1"

        def __init__(self):
            self.roots = [root]

        def default_config(self):
            if self.bad_config:
                raise ValueError("Invalid roots configuration")
            return config.SourceConfig(
                self.key, enabled=self.enabled, roots=self.roots, label="Fixture source"
            )

        def fingerprint(self, cfg):
            return self.fingerprint_value

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            self.scans += 1
            if self.fail:
                raise PermissionError("Cannot read fixture history")
            return {"added": 0, "updated": 0, "skipped": 0}

    source = HealthSource()
    monkeypatch.setattr(ingest, "WATCHED_SOURCES", [source])
    monkeypatch.setattr(ingest, "IMPORT_SOURCES", [])
    return source


def test_source_health_persists_failure_success_and_unchanged_checks(
    client, health_source
):
    from mark import db, ingest

    assert client.get("/api/sources").json()[0]["health"] == "detected"
    ingest.ingest_all(do_embed=False)
    first = client.get("/api/sources").json()[0]
    assert first["health"] == "healthy"
    success = first["history"]["last_success_at"]
    assert success
    ingest.ingest_all(do_embed=False)
    unchanged = client.get("/api/sources").json()[0]
    assert health_source.scans == 1
    assert unchanged["history"]["status"] == "unchanged"
    assert unchanged["history"]["last_success_at"] == success
    health_source.fail = True
    ingest.ingest_all(rebuild=True, do_embed=False)
    db.init_db()
    failed = client.get("/api/sources").json()[0]
    assert failed["health"] == "error"
    assert failed["history"]["last_success_at"] == success
    assert failed["error"] == "Cannot read fixture history"
    assert failed["history"]["last_error_at"]
    health_source.fail = False
    ingest.ingest_all(do_embed=False)
    recovered = client.get("/api/sources").json()[0]
    assert recovered["health"] == "healthy"
    assert recovered["error"] is None
    assert recovered["history"]["last_error"] == "Cannot read fixture history"
    assert recovered["history"]["last_success_at"] >= success


def test_source_health_missing_partial_disabled_and_config_changes(
    client, health_source, tmp_path
):
    from mark import ingest

    ingest.ingest_all(do_embed=False)
    health_source.roots = [tmp_path / "missing"]
    missing = client.get("/api/sources").json()[0]
    assert missing["health"] == "missing"
    assert missing["configuration_changed"] is True
    assert missing["root_status"][0]["state"] == "missing"
    assert missing["history"]["last_success_at"]
    health_source.roots.append(tmp_path / "source-store")
    assert client.get("/api/sources").json()[0]["health"] == "degraded"
    health_source.enabled = False
    assert client.get("/api/sources").json()[0]["health"] == "disabled"
    before = health_source.scans
    ingest.ingest_all(do_embed=False)
    assert health_source.scans == before
    health_source.enabled = True
    health_source.roots = [tmp_path / "another-store"]
    health_source.roots[0].mkdir()
    assert client.get("/api/sources").json()[0]["health"] == "detected"
    ingest.ingest_all(do_embed=False)
    assert health_source.scans == before + 1
    assert client.get("/api/sources").json()[0]["health"] == "healthy"


def test_source_health_isolates_config_and_permission_failures(
    client, health_source, monkeypatch
):
    from mark import ingest
    from mark.api import sources

    health_source.bad_config = True
    response = client.get("/api/health")
    assert response.status_code == 200
    broken = response.json()["sources"][0]
    assert broken["health"] == "error" and "configuration" in broken["error"]
    result = ingest.ingest_all(do_embed=False)
    assert result["errors"][health_source.key] == "Invalid roots configuration"
    health_source.bad_config = False
    monkeypatch.setattr(sources.os, "access", lambda *args: False)
    unreadable = client.get("/api/sources").json()[0]
    assert unreadable["health"] == "error"
    assert unreadable["root_status"][0]["state"] == "unreadable"
    assert "permissions" in unreadable["action"]


def test_source_health_counts_stable_adapter_and_hidden_rows(
    client, health_source, make_session, persist_session
):
    from mark.repositories import sessions

    s = make_session(sid="dynamic-owner", source="custom-label")
    s["source_adapter"] = health_source.key
    persist_session(s)
    persist_session(make_session(sid="legacy-owner", source=health_source.key))
    sessions.set_hidden("dynamic-owner", True)
    health_source.enabled = False
    source = client.get("/api/sources").json()[0]
    assert source["indexed"] == 2
    assert source["health"] == "disabled"


def test_health_does_not_load_models_or_scan_sources(
    client, health_source, monkeypatch
):
    from mark import embeddings

    def forbidden(*args, **kwargs):
        pytest.fail("health GET must not start model loading or source scans")

    monkeypatch.setattr(embeddings, "get_embedder", forbidden)
    monkeypatch.setattr(health_source, "fingerprint", forbidden)
    monkeypatch.setattr(health_source, "ingest", forbidden)
    payload = client.get("/api/health").json()
    coverage = payload["index"]["coverage"]
    assert coverage["total_chunks"] == coverage["keyword_chunks"] == 0
    assert coverage["eligible_chunks"] == 0
    assert coverage["identity"] is None
    assert coverage["embedded_chunks"] is None
    assert payload["sources"][0]["history"] is None


def test_health_counts_compatible_vectors_against_capped_policy(
    client, health_source, monkeypatch, make_session, persist_session
):
    from mark import config, db, embeddings, ingest

    monkeypatch.setattr(config, "MAX_EMBED_CHUNKS_PER_SESSION", 2)
    session = make_session(sid="coverage")
    turn = session["turns"][0]
    session["turns"] = [{**turn, "turn_index": i} for i in range(5)]
    persist_session(session)
    assert ingest.ensure_index_ready()
    ready = client.get("/api/health").json()["index"]
    coverage = ready["coverage"]
    assert ready["active"] is True
    assert coverage["identity"]["backend"] == "builtin-hash"
    assert coverage["total_chunks"] == coverage["keyword_chunks"] == 10
    assert coverage["eligible_chunks"] == coverage["embedded_chunks"] == 2
    assert coverage["excluded_by_cap"] == 8
    assert coverage["pending_chunks"] == 0
    with db.cursor() as cur:
        cur.execute(
            "UPDATE embeddings SET vector = ? WHERE chunk_id = (SELECT MIN(chunk_id) FROM embeddings)",
            (b"bad",),
        )
        embeddings.mark_index_dirty(cur)
    db.set_meta("embed_error", "Interrupted inference")
    ingest.mark_semantic_unverified()
    pending = client.get("/api/health").json()["index"]
    assert pending["active"] is False and pending["pending"] is True
    assert pending["error"] == "Interrupted inference"
    assert pending["coverage"]["embedded_chunks"] == 1
    assert pending["coverage"]["pending_chunks"] == 1
    assert pending["coverage"]["identity"]["model"] == "builtin-hash"
    assert ingest.ensure_index_ready()
    recovered = client.get("/api/health").json()["index"]
    assert recovered["active"] is True
    assert recovered["error"] is None
    assert recovered["coverage"]["embedded_chunks"] == 2


def test_health_unknown_fingerprint_and_keyword_gap(
    client, health_source, make_session, persist_session
):
    from mark import db

    persist_session(make_session())
    db.set_meta("embed_target_fingerprint", "not-json")
    with db.cursor() as cur:
        cur.execute("DELETE FROM search_index")
    index = client.get("/api/health").json()["index"]
    assert index["coverage"]["keyword_chunks"] == 0
    assert index["coverage"]["total_chunks"] == 2
    assert index["coverage"]["identity"] is None
    assert index["coverage"]["embedded_chunks"] is None


def test_health_rejects_foreign_process_embedding_identity(
    client, health_source, make_session, persist_session, monkeypatch
):
    from mark import db, embeddings, ingest

    persist_session(make_session())
    assert ingest.ensure_index_ready()
    with db.cursor() as cur:
        embeddings.set_index_fingerprint(cur, embeddings._HashEmbed(dim=16))
    monkeypatch.setattr(
        embeddings, "get_embedder", lambda: pytest.fail("model loaded in diagnostics")
    )
    index = client.get("/api/health").json()["index"]
    assert index["active"] is False
    assert index["pending"] is True
    assert index["coverage"]["identity"]["dim"] == 16
    assert index["coverage"]["embedded_chunks"] == 0


def test_health_post_scan_error_without_prior_history_is_visible(client, health_source):
    from mark import db, persist

    with db.cursor() as cur:
        persist.record_source_health(
            cur,
            health_source.key,
            {"status": "error", "error": "Post-scan check failed"},
        )
    source = client.get("/api/sources").json()[0]
    assert source["health"] == "error"
    assert source["configuration_changed"] is False
    assert source["error"] == "Post-scan check failed"


def test_health_status_poll_keeps_coverage_off_hot_path(client, monkeypatch):
    from mark import embeddings

    monkeypatch.setattr(
        embeddings,
        "index_coverage",
        lambda *args: pytest.fail("expensive coverage on heartbeat"),
    )
    assert client.get("/api/status").status_code == 200


def test_health_history_bounds_errors_and_recovers_malformed_metadata(
    client, health_source
):
    from mark import db, persist

    db.set_meta("source_health:" + health_source.key, "not-json")
    assert client.get("/api/sources").json()[0]["history"] is None
    with db.cursor() as cur:
        persist.record_source_health(
            cur,
            health_source.key,
            {"status": "error", "error": "x" * 5000, "content": "must not persist"},
        )
    history = client.get("/api/sources").json()[0]["history"]
    assert len(history["last_error"]) == 2000
    assert "content" not in history


def test_health_frontend_classification_and_retry_ordering():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend health regression tests")
    script = r"""
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, SourceTextModule, SyntheticModule } from "node:vm";
import { describeIndexHealth } from "./mark/web/js/sidebar.js";
const coverage = {total_chunks: 10, keyword_chunks: 10, eligible_chunks: 2,
    embedded_chunks: 2, pending_chunks: 0, identity: {backend: "fastembed"}};
const ready = {active: true, pending: false, coverage};
assert.equal(describeIndexHealth(ready).label, "Semantic index ready");
assert.equal(describeIndexHealth({...ready, pending: true}).label, "Semantic indexing pending");
assert.equal(describeIndexHealth({...ready, active: false}).label, "Semantic indexing pending");
assert.equal(describeIndexHealth({...ready, error: "broken"}).label, "Index error");
assert.equal(describeIndexHealth({...ready, coverage: {...coverage, total_chunks: 0, keyword_chunks: 0}}).label, "Empty archive");
assert.equal(describeIndexHealth({...ready, coverage: {...coverage, identity: null}}).label, "Semantic indexing pending");
assert.equal(describeIndexHealth({...ready, coverage: {...coverage, keyword_chunks: 9}}).label, "Keyword coverage gap");
assert.equal(describeIndexHealth({...ready, coverage: {...coverage, identity:{backend:"builtin-hash"}}}).label, "Built-in fallback");

// Execute the app shell's actual request ordering, with just its DOM/imports mocked.
const elements = new Map();
const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {disabled:false,hidden:false,dataset:{},value:"",
        listeners:{},classList:{toggle() {},add() {},remove() {}},
        addEventListener(name,fn) {this.listeners[name]=fn;}});
    return elements.get(selector);
};
const observed = [], timers = new Map();
let nextTimer = 0, retry, resolveFirstPoll, resolvePost, startup;
let polls = 0;
const started = new Promise(resolve => startup=resolve);
const idle = {running:false,queued:false,message:"idle",ask_enabled:false};
const api = async (url) => {
    if (url === "/api/status") {
        polls += 1;
        if (polls === 1) return idle;
        return new Promise(resolve => {resolveFirstPoll=resolve;});
    }
    if (url.startsWith("/api/reindex")) return new Promise(resolve => {resolvePost=resolve;});
    throw new Error("Unexpected API " + url);
};
const noop=()=>{};
const modules = {
    "./api.js": {api},
    "./state.js": {state:{view:"sources"}},
    "./sidebar.js": {loadFacets:async()=>{},loadStats:async()=>({sessions:0}),syncFilterUI:noop,
        setupSourceHealth:callback=>{retry=callback;},showSources:noop,
        observeSourceHealth:st=>observed.push(st.message),sourceHealthUnavailable:noop},
    "./utils.js": {$:element,$$:()=>[],srcMeta:noop,toast:noop},
    "./icons.js": {icon:noop}, "./router.js":{routeFromHash:()=>startup()},
    "./views/list.js":{clearAllFilters:noop,doSearch:noop,handleListKey:noop,run:noop,showList:noop},
    "./views/detail.js":{openSession:noop},
    "./views/collections.js":{hideCollMenu:noop,openCollectionDialog:noop,saveCollection:noop,saveCollectionFromFilters:noop,showCollections:noop},
    "./views/library.js":{setupLibrary:noop,showLibrary:noop},
    "./views/usage.js":{loadUsage:noop,showUsage:noop},
    "./views/ask.js":{showAsk:noop,submitAsk:noop},
    "./palette.js":{closePalette:noop,isPaletteOpen:()=>false,openPalette:noop,setupPalette:noop},
};
const context = createContext({document:{documentElement:{dataset:{}},addEventListener:noop,activeElement:null},
    localStorage:{getItem:()=>null},setTimeout:(fn,delay)=>{timers.set(++nextTimer,{fn,delay});return nextTimer;},
    clearTimeout:id=>timers.delete(id)});
const main = new SourceTextModule(readFileSync("mark/web/js/main.js","utf8"),{context});
await main.link(path=>new SyntheticModule(Object.keys(modules[path]),function(){
    for(const [key,value] of Object.entries(modules[path]))this.setExport(key,value);
},{context}));
await main.evaluate();
await started;
const oldGet=[...timers.values()][0].fn();
const post=retry(true);
resolvePost({...idle,queued:true,message:"queued-new",admission:"accepted"});
await post;
assert.equal([...timers.values()].some(timer=>timer.delay===1100),true);
resolveFirstPoll({...idle,message:"stale-idle"});
await oldGet;
assert.deepEqual(observed,["idle","queued-new"]);
"""
    result = subprocess.run(
        [node, "--experimental-vm-modules", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_health_retry_uses_existing_coordinator(client, monkeypatch):
    from mark import background

    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        return "accepted"

    monkeypatch.setattr(background, "request_reindex", request)
    response = client.post("/api/reindex?repair_semantic=true")
    assert response.status_code == 200
    assert calls == [{"rebuild": False, "repair_semantic": True}]
    assert response.json()["admission"] == "accepted"


def test_health_import_outcomes_are_durable_and_not_watched(
    client, health_source, monkeypatch, make_session
):
    from mark import ingest
    from mark.sources.base import ImportSource

    class ImportFixture(ImportSource):
        key = "fixture-import"
        label = "Fixture export"
        fail = False

        def detect(self, filename, data):
            return True

        def parse_export(self, data):
            if self.fail:
                raise ValueError("Invalid export structure")
            yield make_session(sid="imported", source=self.key)

    source = ImportFixture()
    monkeypatch.setattr(ingest, "IMPORT_SOURCES", [source])
    ingest.import_export("fixture.json", b"{}", do_embed=False)
    imported = client.get("/api/sources").json()[1]
    assert imported["health"] == "import"
    assert imported["history"]["last_success_at"]
    assert imported["indexed"] == 1
    source.fail = True
    with pytest.raises(ValueError, match="Invalid export"):
        ingest.import_export("bad.json", b"{}", do_embed=False)
    failed = client.get("/api/sources").json()[1]
    assert failed["health"] == "error"
    assert (
        failed["history"]["last_success_at"] == imported["history"]["last_success_at"]
    )
    assert failed["indexed"] == 1
    assert "not watched" in failed["action"]


def test_read_endpoints_ok(client):
    for path in [
        "/api/stats",
        "/api/status",
        "/api/facets",
        "/api/sources",
        "/api/health",
        "/api/usage",
        "/api/snippets",
        "/api/snippets/languages",
        "/api/snippets/repositories",
        "/api/collections",
        "/api/ask/status",
    ]:
        assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("count", [0, 1, 80, 81, 187])
def test_snippet_pages_report_exact_totals_and_reach_every_result(
    client, make_session, persist_session, count
):
    from mark.repositories import snippets as snippets_repo

    persist_session(
        make_session(
            code_blocks=[
                {"language": "python", "content": f"print({i})"} for i in range(count)
            ]
        )
    )
    ids = []
    for offset in range(0, max(count, 1), 80):
        response = client.get("/api/snippets", params={"offset": offset})
        assert response.status_code == 200
        page = response.json()
        assert page["total"] == count
        assert page["offset"] == offset and page["limit"] == 80
        assert len(page["snippets"]) == min(80, max(0, count - offset))
        assert page["has_more"] is (offset + 80 < count)
        assert client.get("/api/snippets", params={"offset": offset}).json() == page
        ids.extend(row["id"] for row in page["snippets"])
    assert len(set(ids)) == count
    assert ids == sorted(ids, reverse=True)
    assert [row["id"] for row in snippets_repo.snippets()] == ids[:80]
    beyond = client.get("/api/snippets", params={"offset": count + 80}).json()
    assert beyond["snippets"] == [] and beyond["has_more"] is False
    assert beyond["total"] == count


def test_snippet_count_and_rows_use_one_read_snapshot(
    client, make_session, persist_session, monkeypatch
):
    from contextlib import contextmanager

    from mark import db
    from mark.repositories import sessions as sessions_repo

    persist_session(
        make_session(code_blocks=[{"language": "sh", "content": "echo safe"}])
    )
    original_transaction = db.transaction

    class ConcurrentHide:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            cursor = self.connection.execute(sql, params)
            if sql.startswith("SELECT COUNT(*) FROM code_blocks"):
                sessions_repo.set_hidden("s1", True)
            return cursor

    @contextmanager
    def concurrent_transaction():
        with original_transaction() as connection:
            yield ConcurrentHide(connection)

    monkeypatch.setattr(db, "transaction", concurrent_transaction)
    page = client.get("/api/snippets").json()
    assert page["total"] == len(page["snippets"]) == 1
    assert page["has_more"] is False
    # The next request sees the write; it cannot split count and rows above.
    assert client.get("/api/snippets").json()["total"] == 0


def test_snippet_order_uses_utc_instants_created_fallback_and_id_tiebreaker(
    client, make_session, persist_session
):
    for sid, updated, created in (
        ("older", "2026-01-01T23:59:59Z", "2025-01-01T00:00:00Z"),
        ("equal-utc", "2026-01-02T00:00:00Z", "2025-01-01T00:00:00Z"),
        ("equal-offset", "2026-01-01T19:00:00-05:00", "2025-01-01T00:00:00Z"),
        ("created-only", None, "2026-01-03T00:00:00Z"),
        ("undated", None, None),
    ):
        session = make_session(
            sid=sid, code_blocks=[{"language": "sh", "content": f"echo {sid}"}]
        )
        session.update(updated_at=updated, created_at=created)
        persist_session(session)
    actual = [
        client.get("/api/snippets", params={"limit": 1, "offset": offset}).json()[
            "snippets"
        ][0]["session_id"]
        for offset in range(5)
    ]
    assert actual == ["created-only", "equal-offset", "equal-utc", "older", "undated"]


def test_snippet_filters_combine_with_inclusive_utc_dates_and_literal_text(
    client, make_session, persist_session
):
    def add(
        sid, timestamp, repo="project/é", language="bash", text="echo 100%_ready\\v2"
    ):
        session = make_session(
            sid=sid,
            repository=repo,
            code_blocks=[{"language": language, "content": text}],
        )
        session["updated_at"] = timestamp
        persist_session(session)

    add("start", "2026-01-02T00:00:00Z")
    add("end", "2026-01-02T23:59:59.999Z", language="PowerShell")
    add("offset-inside", "2026-01-03T00:30:00+01:00")
    add("before", "2026-01-01T23:59:59Z")
    add("after", "2026-01-03T00:00:00Z")
    add("other-repo", "2026-01-02T12:00:00Z", repo="other")
    add("other-language", "2026-01-02T12:00:00Z", language="python")
    add("other-text", "2026-01-02T12:00:00Z", text="echo 100XXreadyXv2")
    filters = {
        "q": "%_ready\\v2",
        "repo": "project/é",
        "date_from": "2026-01-02",
        "date_to": "2026-01-02",
    }
    page = client.get("/api/snippets", params={**filters, "language": "bash"}).json()
    assert page["total"] == 2
    assert {row["session_id"] for row in page["snippets"]} == {"start", "offset-inside"}
    # Preserve the existing contract: commands wins over a language selection.
    page = client.get(
        "/api/snippets", params={**filters, "commands": True, "language": "python"}
    ).json()
    assert page["total"] == 3 and page["has_more"] is False
    assert {row["session_id"] for row in page["snippets"]} == {
        "start",
        "end",
        "offset-inside",
    }
    assert (
        client.get("/api/snippets", params={**filters, "repo": "missing"}).json()[
            "total"
        ]
        == 0
    )
    assert (
        client.get("/api/snippets", params={"date_from": "2026-01-03"}).json()["total"]
        == 1
    )
    assert (
        client.get("/api/snippets", params={"date_to": "2026-01-01"}).json()["total"]
        == 1
    )
    assert (
        client.get("/api/snippets", params={"date_to": "9999-12-31"}).json()["total"]
        == 8
    )


def test_snippet_pages_and_facets_share_visibility_and_nonempty_scope(
    client, make_session, persist_session, monkeypatch
):
    from mark import db
    from mark.repositories import sessions as sessions_repo

    for sid, repo, language, source in (
        ("visible", "visible-repo", "python", "vscode"),
        ("hidden", "hidden-repo", "rust", "vscode"),
        ("disabled", "disabled-repo", "go", "cline"),
        ("custom-disabled", "custom-repo", "yaml", "custom-fork"),
    ):
        session = make_session(
            sid=sid,
            repository=repo,
            source=source,
            code_blocks=[
                {"language": language, "content": f"snippet {i}"} for i in range(85)
            ],
        )
        if sid == "custom-disabled":
            session["source_adapter"] = "cline"
        persist_session(session)
    sessions_repo.set_hidden("hidden", True)
    monkeypatch.setenv("MARK_SOURCE_CLINE_ENABLED", "0")
    persist_session(make_session(sid="empty", repository="no-snippets"))
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO code_blocks(session_id, turn_index, language, content) VALUES (?,?,?,?)",
            [("empty", 0, "empty-language", value) for value in (None, "", " ", "x")],
        )
    for offset, length in ((0, 80), (80, 5)):
        page = client.get("/api/snippets", params={"offset": offset}).json()
        assert page["total"] == 85 and len(page["snippets"]) == length
        assert {row["session_id"] for row in page["snippets"]} == {"visible"}
    assert client.get("/api/snippets/repositories").json() == [
        {"repository": "visible-repo", "count": 85}
    ]
    assert client.get("/api/snippets/languages").json() == [
        {"language": "python", "count": 85}
    ]
    assert client.get("/api/snippets?repo=hidden-repo").json()["total"] == 0
    sessions_repo.set_hidden("hidden", False)
    monkeypatch.setenv("MARK_SOURCE_CLINE_ENABLED", "1")
    assert client.get("/api/snippets").json()["total"] == 340
    assert len(client.get("/api/snippets/repositories").json()) == 4


@pytest.mark.parametrize(
    "params",
    [
        {"offset": -1},
        {"offset": 2**63},
        {"offset": "invalid"},
        {"date_from": "not-a-date"},
        {"date_to": "2026-02-30"},
        {"date_from": "2026-02-01", "date_to": "2026-01-01"},
    ],
)
def test_snippet_paging_and_date_input_validation(client, params):
    assert client.get("/api/snippets", params=params).status_code == 422


@pytest.mark.parametrize("requested, effective", [(0, 1), (-1, 1), (301, 300)])
def test_snippet_page_reports_effective_legacy_limit(client, requested, effective):
    response = client.get("/api/snippets", params={"limit": requested})
    assert response.status_code == 200
    assert response.json()["limit"] == effective


def test_snippet_frontend_paging_filter_resets_and_async_navigation():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend Library regression tests")
    script = r"""
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, SourceTextModule, SyntheticModule } from "node:vm";
const elements = new Map(), reads = [], timers = [];
const document = {body: {}, activeElement: null};
function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
        dataset: {}, value: "", checked: false, disabled: false, hidden: false,
        innerHTML: "", textContent: "", validity: {valid: true}, attributes: {},
        listeners: {}, classList: {toggle() {}}, scrolls: 0,
        addEventListener(name, fn) {this.listeners[name] = fn;},
        setAttribute(name, value) {this.attributes[name] = value;},
        focus() {document.activeElement = this;},
        scrollIntoView() {this.scrolls += 1;},
    });
    return elements.get(selector);
}
const buttons = ["#libPrevious", "#libNext", "#bottomPrevious", "#bottomNext"].map(element);
buttons.forEach((button, i) => {button.dataset.snippetPage = i % 2 ? "next" : "previous";});
const ranges = [element("#libPageStatus"), element("#bottomRange")];
const all = selector => {
    if (selector === "[data-snippet-page]") return buttons;
    if (selector === "[data-snippet-range]") return ranges;
    const html = element("#libResults").innerHTML;
    if (selector === "#libResults .snip-open" && html.includes("snip-card")) {
        const link = element("#firstOpen");
        link.dataset = {id: html.match(/data-id="([^"]+)"/)[1], turn: "2"};
        return [link];
    }
    if (selector === "#libResults .snip-copy" && html.includes("snip-card")) {
        element("#firstCopy").dataset.idx = "0";
        return [element("#firstCopy")];
    }
    return [];
};
const flush = async () => {for (let i = 0; i < 10; i += 1) await Promise.resolve();};
const runTimers = () => {timers.splice(0).forEach(fn => fn());};
const state = {view: "library", libraryMode: "extracted", currentId: null};
let detailGeneration = 0, finishDetail, copied, holdFacets = false;
const facetReads = [];
const api = (url, options) => {
    if (url.startsWith("/api/snippets?")) return new Promise((resolve, reject) => {
        reads.push({url, params: new URLSearchParams(url.split("?")[1]), resolve, reject});
    });
    if (url.startsWith("/api/snippets/")) {
        if (holdFacets) return new Promise(resolve => facetReads.push({url, resolve}));
        return Promise.resolve(url.endsWith("languages") ? [{language: "bash", count: 187}] : [{repository: "project/é", count: 187}]);
    }
    if (url.startsWith("/api/solutions?")) return Promise.resolve({solutions: [], total: 0, offset: 0, has_more: false});
    throw new Error("Unexpected request: " + url);
};
const modules = {
    "../api.js": {api},
    "../state.js": {state, showOnly() {}, setLayoutWide() {}},
    "../utils.js": {$: element, $$: all, debounce: fn => (...args) => {timers.push(() => fn(...args));},
        esc: value => value ?? "", fmtDate: () => "Jan 2", srcMeta: () => ({icon: "", label: "VS Code"}),
        sessionHash: (id, options) => `#/session/${id}?turn=3&q=${options.q}`, toast() {}, withTransition: fn => fn()},
    "../icons.js": {icon: () => ""},
    "./detail.js": {teardownReading() {detailGeneration += 1;},
        openSession() {const generation = ++detailGeneration; finishDetail = () => {if (generation === detailGeneration) state.view = "detail";};}},
};
const context = createContext({URLSearchParams, document, window: {addEventListener() {}},
    location: {hash: "#/library"}, history: {pushState() {}},
    navigator: {clipboard: {async writeText(text) {copied = text;}}}});
const library = new SourceTextModule(readFileSync("mark/web/js/views/library.js", "utf8"), {context});
await library.link(path => new SyntheticModule(Object.keys(modules[path]), function () {
    for (const [name, value] of Object.entries(modules[path])) this.setExport(name, value);
}, {context}));
await library.evaluate();
const {libState, loadSnippets, showLibrary, setupLibrary} = library.namespace;
setupLibrary();
const last = () => reads.at(-1);
const page = (offset = 0, total = 187) => ({offset, total, limit: 80, has_more: offset + 80 < total,
    snippets: Array.from({length: Math.max(0, Math.min(80, total - offset))}, (_, index) => ({
        id: total - offset - index, session_id: `origin-${total - offset - index}`, turn_index: 2,
        language: "bash", content: `printf ${total - offset - index}`, session_title: "Test", source: "vscode",
        repository: "project/é", updated_at: "2026-01-02T00:00:00Z",
    }))});
const done = async data => {last().resolve(data); await flush();};
const change = (selector, value) => {element(selector).value = value; element(selector).listeners.change();};
const click = selector => element(selector).listeners.click({preventDefault() {}});

const first = loadSnippets();
assert.equal(last().params.get("offset"), "0");
assert.equal(last().params.get("limit"), "80");
await done(page()); await first;
assert.equal(element("#libCount").textContent, "187 snippets");
assert.equal(ranges[0].textContent, "1\u201380 of 187");
assert.equal(buttons[0].disabled, true); assert.equal(buttons[1].disabled, false);

click("#firstOpen");
element("#libNext").focus(); click("#libNext");
const count = reads.length;
click("#libNext");
assert.equal(reads.length, count, "Disable both pagers while a page is pending");
assert.equal(last().params.get("offset"), "80");
finishDetail();
assert.equal(state.view, "library", "Newer page intent cancels a pending source navigation");
await done(page(80));
assert.equal(ranges[0].textContent, "81\u2013160 of 187");
assert.equal(document.activeElement, element("#libPageStatus"));
await element("#firstCopy").listeners.click();
assert.equal(copied, "printf 107", "Copy uses the current page, not the first page cache");
element("#libNext").focus(); click("#libNext"); await done(page(160));
assert.equal(ranges[0].textContent, "161\u2013187 of 187");
assert.equal(buttons[1].disabled, true);
click("#libPrevious"); await done(page(80));
assert.equal(libState.offset, 80);

for (const [selector, value, parameter] of [
    ["#libRepo", "project/é", "repo"], ["#libLang", "python", "language"],
    ["#libDateFrom", "2026-01-01", "date_from"], ["#libDateTo", "2026-01-03", "date_to"],
]) {
    libState.offset = 80;
    change(selector, value);
    assert.equal(last().params.get("offset"), "0", selector + " resets pagination");
    assert.equal(last().params.get(parameter), value);
    await done(page(0, 5));
}
element("#libCommands").checked = true; element("#libCommands").listeners.change();
assert.equal(last().params.get("commands"), "true");
assert.equal(last().params.has("language"), false);
assert.equal(last().params.get("repo"), "project/é");
assert.equal(last().params.get("date_to"), "2026-01-03");
assert.equal(element("#libLang").disabled, true); await done(page());

const older = loadSnippets(), oldRead = last();
element("#libSearch").value = "new query"; element("#libSearch").listeners.input();
oldRead.resolve(page()); await older;
assert.equal(element("#libCount").textContent, "", "Old page cannot paint during the input debounce");
assert.ok(!element("#libResults").innerHTML.includes("snip-card"));
runTimers(); assert.equal(last().params.get("q"), "new query"); await done(page(0, 1));
element("#libSearch").value = "latest"; element("#libSearch").listeners.input();
change("#libRepo", "different");
const newCount = reads.length; runTimers();
assert.equal(reads.length, newCount, "Another filter supersedes queued text work");
await done(page(0, 0)); assert.equal(ranges[0].textContent, "0 of 0");

click("#libClear"); await done(page());
libState.offset = 80;
const failure = loadSnippets(false); last().reject(new Error("Fixture unavailable")); await failure;
assert.equal(element("#libError").hidden, false); assert.equal(element("#libRetry").hidden, false);
assert.equal(element("#libCount").textContent, "");
assert.equal(ranges[0].textContent, "Snippet count unavailable");
click("#libRetry"); assert.equal(last().params.get("offset"), "80"); await done(page(80));
assert.equal(element("#libError").hidden, true);

// Last-page correction must not recapture focus after the user moved to a filter.
libState.offset = 160; element("#libNext").focus();
const shrinking = loadSnippets(false, true);
element("#libSearch").focus(); await done(page(160, 90));
assert.equal(last().params.get("offset"), "80");
await done(page(80, 90)); await shrinking;
assert.equal(document.activeElement, element("#libSearch"));
assert.equal(ranges[0].textContent, "81\u201390 of 90");

const stale = loadSnippets(false), staleRead = last();
await showLibrary({mode: "curated"}); await flush();
staleRead.resolve(page()); await stale;
assert.equal(element("#libCount").textContent, "0 saved solutions");
holdFacets = true;
const returning = showLibrary({mode: "extracted"});
assert.equal(last().params.get("offset"), "80", "Extracted page survives curated/source navigation");
libState.repo = "no-longer-visible";
facetReads.forEach(read => read.resolve([]));
await done(page(80)); await returning;
assert.equal(element("#libRepo").value, "no-longer-visible");
assert.ok(element("#libRepo").innerHTML.includes("not currently available"));
holdFacets = false;

change("#libDateFrom", "2026-02-03"); await done(page());
const beforeInvalid = reads.length;
change("#libDateTo", "2026-02-01");
assert.equal(reads.length, beforeInvalid);
assert.equal(element("#libError").hidden, false);
assert.equal(buttons[1].disabled, true);
click("#libClear");
assert.equal(element("#libDateFrom").value, ""); assert.equal(element("#libRepo").value, "");
assert.equal(libState.commands, false); await done(page());
libState.offset = 80; click("#libRefresh");
assert.equal(last().params.get("offset"), "0"); await done(page());
assert.equal(element("#libResults").attributes["aria-busy"], "false");
"""
    result = subprocess.run(
        [node, "--experimental-vm-modules", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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


def test_evidence_target_resolves_sparse_turn_indices(
    client, make_session, persist_session
):
    session = make_session(sid="sparse-evidence")
    template = session["turns"][0]
    session["turns"] = [{**template, "turn_index": index * 3} for index in range(45)]
    persist_session(session)

    detail = client.get(
        "/api/sessions/sparse-evidence", params={"turn_index": 123}
    ).json()

    assert detail["target_turn_found"] is True
    assert detail["turns_offset"] == 40
    assert [t["turn_index"] for t in detail["turns"]] == [120, 123, 126, 129, 132]
    assert detail["has_more_turns"] is False
    missing = client.get(
        "/api/sessions/sparse-evidence", params={"turn_index": 124}
    ).json()
    assert missing["target_turn_found"] is False
    assert missing["turns_offset"] == 0
    assert missing["turns"][0]["turn_index"] == 0


@pytest.mark.parametrize("chunk_size", [1200, 6000])
def test_evidence_preview_finds_late_text_without_rendering_whole_turn(
    client, make_session, persist_session, monkeypatch, chunk_size
):
    from mark import config, render

    monkeypatch.setattr(config, "DETAIL_INLINE_TURN_CHARS", 1_000)
    monkeypatch.setattr(config, "MAX_CHUNK_CHARS", chunk_size)
    persist_session(
        make_session(
            sid="huge-evidence",
            user="Find the fix",
            asst=("ordinary words " * 10_000) + "orbital evidence solution",
        )
    )
    original_render = render.render_markdown
    lengths = []

    def record_render(text):
        lengths.append(len(text or ""))
        return original_render(text)

    monkeypatch.setattr(render, "render_markdown", record_render)
    initial = client.get("/api/sessions/huge-evidence", params={"turn_index": 0}).json()
    assert initial["turns"][0]["deferred"] is True
    assert lengths == []

    response = client.get(
        "/api/sessions/huge-evidence/turns/0",
        params={"preview": True, "q": '"orbital evidence"'},
    )
    preview = response.json()
    assert preview["preview"] is True
    assert "orbital evidence solution" in preview["assistant_html"]
    assert max(lengths) <= 4_000
    assert len(response.content) < 20_000
    assert preview["content_chars"] > 100_000


def test_conversation_matches_are_bounded_and_available_for_hidden_sessions(
    client, make_session, persist_session, monkeypatch
):
    from mark import render
    from mark.repositories import sessions

    session = make_session(sid="many-matches", user="orbital evidence")
    template = session["turns"][0]
    session["turns"] = [{**template, "turn_index": index} for index in range(125)]
    persist_session(session)
    sessions.set_hidden("many-matches", True)
    monkeypatch.setattr(
        render,
        "render_markdown",
        lambda _text: pytest.fail("finding matches must not render turn bodies"),
    )

    page = client.get(
        "/api/sessions/many-matches/matches", params={"q": '"orbital evidence"'}
    ).json()
    assert page["turn_indices"] == list(range(100))
    assert page["total"] == 125
    assert page["has_more"] is True
    final = client.get(
        "/api/sessions/many-matches/matches",
        params={"q": '"orbital evidence"', "offset": 100},
    ).json()
    assert final["turn_indices"] == list(range(100, 125))
    assert final["has_more"] is False
    around = client.get(
        "/api/sessions/many-matches/matches",
        params={"q": '"orbital evidence"', "turn_index": 120},
    ).json()
    assert around["offset"] == 100
    assert around["target_position"] == 120
    assert around["turn_indices"] == list(range(100, 125))
    assert client.get("/api/sessions/missing/matches?q=orbital").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/sessions/evidence?turn_index=-1",
        "/api/sessions/evidence?turn_index=9223372036854775808",
        "/api/sessions/evidence/matches?offset=-1",
        "/api/sessions/evidence/matches?limit=101",
        "/api/sessions/evidence/matches?q=" + "x" * 2001,
        "/api/sessions/evidence/turns/0?preview=true&q=" + "x" * 2001,
    ],
)
def test_evidence_parameters_are_validated(client, path):
    assert client.get(path).status_code == 422


def test_evidence_url_and_highlight_helpers():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend helper regression tests")
    root = Path(__file__).resolve().parents[1]
    script = r"""
import assert from "node:assert/strict";
import { sessionHash, parseSessionHash, evidencePattern, adjacentMatchPosition, withTransition } from "./mark/web/js/utils.js";
const id = "repo / ? # % café";
const q = '"orbital evidence" naïve <script> & +';
const hash = sessionHash(id, { turnIndex: 0, q });
assert.deepEqual(parseSessionHash(hash), { id, turnIndex: 0, q, invalidTarget: false });
assert.equal(sessionHash("s"), "#/session/s");
assert.equal(parseSessionHash("#/session/s?turn=40").turnIndex, 39);
assert.equal(parseSessionHash("#/session/%GG"), null);
assert.equal(parseSessionHash("#/library"), null);
for (const turn of ["0", "-1", "1.5", "9007199254740992", "oops", ""]) {
  const parsed = parseSessionHash("#/session/s?turn=" + turn);
  assert.equal(parsed.turnIndex, null);
  assert.equal(parsed.invalidTarget, true);
}
assert.equal(parseSessionHash(sessionHash("s", { q: "x".repeat(3000) })).q.length, 2000);
assert.equal(evidencePattern('"orbital evidence"').test("orbital logs then evidence"), false);
assert.equal(evidencePattern('"orbital evidence"').test("ORBITAL evidence"), true);
assert.equal(evidencePattern('"open file"').test("open filename"), false);
assert.equal(evidencePattern("open file").test("open filename"), true);
assert.equal(evidencePattern("orbital").test("suborbital"), false);
assert.equal(evidencePattern("naïve").test("NAÏVE"), true);
assert.equal(evidencePattern("[.*]"), null);
const page = {total: 125, offset: 100, target_position: 100};
assert.equal(adjacentMatchPosition(page, -1, 1), 100);
assert.equal(adjacentMatchPosition(page, -1, -1), 99);
assert.equal(adjacentMatchPosition(page, 0, 1), 101);
assert.equal(adjacentMatchPosition(page, 0, -1), 99);
assert.equal(adjacentMatchPosition({...page, target_position: 125}, -1, 1), 125);
assert.equal(adjacentMatchPosition({...page, target_position: 125}, -1, -1), 124);
let updates = 0, animations = 0;
globalThis.window = {matchMedia: () => ({matches: false})};
globalThis.document = {visibilityState: "hidden", startViewTransition: fn => {
    animations += 1; fn(); return {ready: Promise.reject(new Error("animation skipped"))};
}};
withTransition(() => updates += 1);
assert.equal(updates, 1);
assert.equal(animations, 0);
document.visibilityState = "visible";
withTransition(() => updates += 1);
await Promise.resolve();
assert.equal(updates, 2);
assert.equal(animations, 1);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_evidence_route_cancels_pending_navigation_to_visible_view():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend routing regression tests")
    script = r"""
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, SourceTextModule, SyntheticModule } from "node:vm";
import { parseSessionHash } from "./mark/web/js/utils.js";
const state = {view: "library", askEnabled: true};
const location = {hash: "#/library"};
const calls = [];
const record = name => (...args) => calls.push([name, ...args]);
const modules = {
    "./state.js": {state},
    "./utils.js": {parseSessionHash, toast: record("toast")},
    "./sidebar.js": {showSources: record("sources")},
    "./views/list.js": {showList: record("list")},
    "./views/detail.js": {openSession: record("session"), teardownReading: record("cancel")},
    "./views/library.js": {showLibrary: record("library")},
    "./views/usage.js": {showUsage: record("usage")},
    "./views/ask.js": {showAsk: record("ask")},
    "./views/collections.js": {openCollection: record("collection"), showCollections: record("collections")},
};
const context = createContext({location, window: {addEventListener() {}}});
const router = new SourceTextModule(readFileSync("mark/web/js/router.js", "utf8"), {context});
await router.link(path => new SyntheticModule(Object.keys(modules[path]), function () {
    for (const [name, value] of Object.entries(modules[path])) this.setExport(name, value);
}, {context}));
await router.evaluate();
router.namespace.routeFromHash();
assert.deepEqual(calls, [["cancel"]]); // visible Library must still cancel the pending GET
calls.length = 0;
location.hash = "#/session/s?turn=40&q=%22open%20file%22";
router.namespace.routeFromHash();
assert.equal(calls[0][0], "session");
assert.equal(calls[0][1], "s");
assert.equal(calls[0][2].turnIndex, 39);
assert.equal(calls[0][2].q, '"open file"');
assert.equal(calls[0][2].fromHash, true);
calls.length = 0;
location.hash = "#/session/%GG";
router.namespace.routeFromHash();
assert.equal(calls[0][0], "toast");
assert.equal(calls[1][0], "list");
calls.length = 0;
location.hash = "#/sources";
router.namespace.routeFromHash();
assert.equal(calls[0][0], "cancel");
assert.equal(calls[1][0], "sources");
calls.length = 0;
location.hash = "#att-2";
router.namespace.routeFromHash();
assert.equal(calls.length, 0); // preserve non-app anchors
"""
    result = subprocess.run(
        [node, "--experimental-vm-modules", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def curated_source(make_session, persist_session):
    session = make_session(
        sid="curation-source",
        title="Authentication repair",
        asst="Use a refresh token.\n\n```bash\nprintf '%s' token\n```",
        code_blocks=[{"language": "bash", "content": "printf '%s' token"}],
    )
    persist_session(session)
    return session


def _save_curated(client, reference, **fields):
    preview = client.post("/api/solutions/preview", json=reference)
    assert preview.status_code == 200, preview.text
    response = client.post(
        "/api/solutions",
        json={
            "title": "Known-good token repair",
            **fields,
            "source": reference,
            "source_sha256": preview.json()["source_sha256"],
        },
    )
    assert response.status_code in (200, 201), response.text
    return client.get("/api/solutions/" + response.json()["id"]).json()


def _solution_fields(solution, **overrides):
    return {
        key: overrides.get(key, solution[key])
        for key in (
            "title",
            "notes",
            "tags",
            "favorite",
            "status",
            "prerequisites",
            "revision",
        )
    }


def test_curated_answer_snapshot_metadata_and_duplicate_save(client, curated_source):
    from mark import db

    reference = {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0}
    preview = client.post("/api/solutions/preview", json=reference).json()
    assert client.get("/api/solutions").json()["total"] == 0
    assert preview["content"] == curated_source["turns"][0]["assistant_response"]
    first = _save_curated(
        client,
        reference,
        tags=[" Auth ", "auth", "", "Token Repair"],
        favorite=True,
        notes="Works after rotation",
        status="verified",
        prerequisites="CLI v2; sandbox only",
    )
    assert first["tags"] == ["auth", "token repair"]
    assert first["content"] == preview["content"]
    assert first["source_session_id"] == curated_source["id"]
    assert first["source_turn_index"] == 0
    assert first["source_title"] == "Authentication repair"
    assert first["source_kind"] == "answer"
    assert first["source_status"] == "available"
    assert first["source_hidden"] is False
    assert first["favorite"] is True
    assert first["revision"] == 1
    again = _save_curated(client, reference, notes="Must not replace my notes")
    assert again["id"] == first["id"]
    assert again["notes"] == "Works after rotation"
    assert client.get("/api/solutions").json()["total"] == 1
    with db.cursor() as cur:
        assert cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_curated_metadata_survives_source_reingest_and_removal(
    client, curated_source, persist_session
):
    from mark import db
    from mark.repositories import sessions

    reference = {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0}
    solution = _save_curated(client, reference)
    path = "/api/solutions/" + solution["id"]
    edit = _solution_fields(
        solution,
        title="Curated title",
        notes="Personal findings",
        favorite=True,
        tags=["ops"],
        prerequisites="Python 3.14",
        status="outdated",
    )
    assert client.put(path, json=edit).status_code == 200
    assert client.put(path, json=edit).status_code == 409

    curated_source["turns"][0]["assistant_response"] = "New source advice"
    persist_session(curated_source)
    saved = client.get(path).json()
    assert saved["source_status"] == "changed"
    assert saved["title"] == "Curated title"
    assert saved["notes"] == "Personal findings"
    assert saved["prerequisites"] == "Python 3.14"
    assert saved["favorite"] is True
    assert saved["tags"] == ["ops"]
    assert saved["status"] == "outdated"
    assert saved["content"] == solution["content"]
    assert saved["revision"] == 2
    with db.cursor() as cur:
        cur.execute("DELETE FROM turns WHERE session_id = ?", (curated_source["id"],))
    assert client.get(path).json()["source_status"] == "missing"
    assert sessions.purge(curated_source["id"]) is True
    db.init_db()
    missing = client.get(path).json()
    assert missing["source_status"] == "missing"
    assert missing["content"] == solution["content"]
    assert missing["source_title"] == "Authentication repair"
    assert client.get("/api/solutions").json()["total"] == 1


def test_curated_snippet_keeps_provenance_across_regenerated_ids(
    client, curated_source, persist_session
):
    from mark import db

    snippet = client.get("/api/snippets").json()["snippets"][0]
    reference = {
        "kind": "snippet",
        "session_id": curated_source["id"],
        "snippet_id": snippet["id"],
    }
    saved = _save_curated(client, reference)
    assert saved["content"] == snippet["content"]
    assert saved["language"] == "bash"
    assert saved["source_kind"] == "snippet"
    assert saved["source_status"] == "available"
    persist_session(curated_source)
    new_snippet = client.get("/api/snippets").json()["snippets"][0]
    assert new_snippet["id"] != snippet["id"]
    assert (
        client.get("/api/solutions/" + saved["id"]).json()["source_status"]
        == "available"
    )
    duplicate = _save_curated(client, {**reference, "snippet_id": new_snippet["id"]})
    assert duplicate["id"] == saved["id"]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE code_blocks SET content = 'different' WHERE session_id = ?",
            (curated_source["id"],),
        )
    assert (
        client.get("/api/solutions/" + saved["id"]).json()["source_status"] == "changed"
    )


@pytest.mark.parametrize("changed", [False, True])
def test_curated_snippet_preview_survives_regenerated_ids_without_losing_edits(
    client, curated_source, persist_session, changed
):
    snippet = client.get("/api/snippets").json()["snippets"][0]
    reference = {
        "kind": "snippet",
        "session_id": curated_source["id"],
        "snippet_id": snippet["id"],
    }
    preview = client.post("/api/solutions/preview", json=reference).json()
    if changed:
        curated_source["turns"][0]["code_blocks"][0]["content"] = "different command"
    persist_session(curated_source)
    response = client.post(
        "/api/solutions",
        json={
            "title": "Draft survives sync",
            "notes": "Carefully written annotations",
            "source": reference,
            "source_sha256": preview["source_sha256"],
        },
    )
    if changed:
        assert response.status_code == 404
        assert client.get("/api/solutions").json()["total"] == 0
    else:
        assert response.status_code == 201
        saved = client.get("/api/solutions/" + response.json()["id"]).json()
        assert saved["content"] == snippet["content"]
        assert saved["notes"] == "Carefully written annotations"
        assert saved["source_status"] == "available"


def test_curated_source_change_since_preview_is_not_silently_saved(
    client, curated_source, persist_session
):
    reference = {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0}
    preview = client.post("/api/solutions/preview", json=reference).json()
    curated_source["turns"][0]["assistant_response"] = "Changed while editing"
    persist_session(curated_source)
    response = client.post(
        "/api/solutions",
        json={
            "title": "Do not silently substitute",
            "source": reference,
            "source_sha256": preview["source_sha256"],
        },
    )
    assert response.status_code == 409
    assert client.get("/api/solutions").json()["total"] == 0


@pytest.mark.parametrize("hidden", ["manual", "source"])
def test_curated_copies_have_explicit_independent_visibility(
    client, curated_source, monkeypatch, hidden
):
    from mark import visibility
    from mark.repositories import sessions

    saved = _save_curated(
        client, {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0}
    )
    if hidden == "manual":
        sessions.set_hidden(curated_source["id"], True)
    else:
        monkeypatch.setattr(
            visibility, "disabled_adapters", lambda: ({"vscode"}, {"vscode"})
        )
    listing = client.get("/api/solutions").json()
    assert listing["total"] == 1
    assert listing["solutions"][0]["source_hidden"] is True
    assert client.get("/api/solutions/" + saved["id"]).json()["source_hidden"] is True
    assert client.get("/api/snippets").json()["snippets"] == []


def test_curated_filters_pagination_and_separate_search(client, curated_source):
    first = _save_curated(
        client,
        {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0},
        notes="rareannotation",
        tags=["ops"],
        status="verified",
        favorite=True,
    )
    snippet = client.get("/api/snippets").json()["snippets"][0]
    second = _save_curated(
        client,
        {
            "kind": "snippet",
            "session_id": curated_source["id"],
            "snippet_id": snippet["id"],
        },
        prerequisites="Version100%_ready",
        status="needs_review",
    )
    page = client.get("/api/solutions?limit=1").json()
    assert page["total"] == 2 and page["has_more"] is True
    assert page["solutions"][0]["id"] == first["id"]
    assert "content" not in page["solutions"][0]
    assert len(page["solutions"][0]["content_preview"]) <= 240
    next_page = client.get("/api/solutions?limit=1&offset=1").json()
    assert next_page["solutions"][0]["id"] == second["id"]
    assert next_page["has_more"] is False
    for params in (
        {"q": "rareannotation"},
        {"tag": " OPS "},
        {"status": "verified"},
        {"favorite": True},
    ):
        matches = client.get("/api/solutions", params=params).json()["solutions"]
        assert [row["id"] for row in matches] == [first["id"]]
    assert client.get("/api/solutions", params={"q": "%_"}).json()["total"] == 1
    assert client.get("/api/solutions", params={"q": "token"}).json()["total"] == 2
    assert client.get("/api/solutions?tag=unknown").json()["total"] == 0
    assert client.get("/api/snippets?q=rareannotation").json()["snippets"] == []
    assert (
        client.get("/api/search?q=rareannotation&mode=keyword").json()["results"] == []
    )


def test_curated_delete_affects_only_copy_and_checks_revision(client, curated_source):
    solution = _save_curated(
        client, {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0}
    )
    path = "/api/solutions/" + solution["id"]
    assert client.delete(path + "?revision=2").status_code == 409
    assert client.delete(path + "?revision=1").status_code == 200
    assert client.get(path).status_code == 404
    assert client.delete(path + "?revision=1").status_code == 404
    assert client.get("/api/sessions/" + curated_source["id"]).status_code == 200


@pytest.mark.parametrize("content", ["x" * 100_001, "\x00" + "x" * 500_000])
def test_curated_oversized_sources_are_rejected_without_partial_save(
    client, curated_source, content
):
    from mark import db

    with db.cursor() as cur:
        cur.execute(
            "UPDATE turns SET assistant_response = ? WHERE session_id = ?",
            (content, curated_source["id"]),
        )
    response = client.post(
        "/api/solutions/preview",
        json={"kind": "answer", "session_id": curated_source["id"], "turn_index": 0},
    )
    assert response.status_code == 413
    assert client.get("/api/solutions").json()["total"] == 0


@pytest.mark.parametrize(
    "reference",
    [
        {"kind": "answer", "session_id": "s"},
        {"kind": "answer", "session_id": "s", "turn_index": -1},
        {"kind": "answer", "session_id": "s", "turn_index": 2**63},
        {"kind": "answer", "session_id": "s", "turn_index": 0, "snippet_id": 1},
        {"kind": "snippet", "session_id": "s", "turn_index": 0},
        {"kind": "snippet", "session_id": "s", "snippet_id": 0},
        {"kind": "unknown", "session_id": "s", "turn_index": 0},
    ],
)
def test_curated_source_validation(client, reference):
    assert client.post("/api/solutions/preview", json=reference).status_code == 422


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "   "},
        {"title": "x" * 161},
        {"notes": "x" * 10001},
        {"tags": ["x" * 41]},
        {"tags": ["a,b"]},
        {"tags": ["a"] * 21},
        {"status": "approved"},
        {"content": "Cannot change source copy"},
        {"prerequisites": "x" * 4001},
    ],
)
def test_curated_metadata_validation(client, curated_source, fields):
    reference = {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0}
    preview = client.post("/api/solutions/preview", json=reference).json()
    response = client.post(
        "/api/solutions",
        json={
            "title": "Valid",
            "source": reference,
            "source_sha256": preview["source_sha256"],
            **fields,
        },
    )
    assert response.status_code == 422


def test_curated_update_rejects_immutable_fields(client, curated_source):
    solution = _save_curated(
        client, {"kind": "answer", "session_id": curated_source["id"], "turn_index": 0}
    )
    path = "/api/solutions/" + solution["id"]
    for field in (
        "content",
        "source_session_id",
        "source_title",
        "source_sha256",
        "source_kind",
    ):
        response = client.put(
            path, json={**_solution_fields(solution), field: "replacement"}
        )
        assert response.status_code == 422
    assert client.get(path).json()["content"] == solution["content"]
    assert client.get(path).json()["revision"] == 1


def test_curated_frontend_metadata_payload_and_validation_errors():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend helper regression tests")
    script = r"""
import assert from "node:assert/strict";
import { editableSolutionFields } from "./mark/web/js/views/library.js";
import { api } from "./mark/web/js/api.js";
const original = {id: "s", title: "Title", notes: "Notes", tags: ["auth"], favorite: true,
    status: "verified", prerequisites: "CLI v2", revision: 3, content: "immutable",
    source_sha256: "immutable hash", source_session_id: "origin", source_kind: "answer"};
assert.deepEqual(editableSolutionFields(original, {favorite: false}), {
    title: "Title", notes: "Notes", tags: ["auth"], favorite: false, status: "verified",
    prerequisites: "CLI v2", revision: 3,
});
assert.equal(original.favorite, true);
globalThis.fetch = async () => ({ok: false, statusText: "Unprocessable Entity",
    json: async () => ({detail: [{msg: "Title is too long"}, {msg: "Too many tags"}]})});
await assert.rejects(api("/api/solutions"), {message: "Title is too long; Too many tags"});
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_curated_editor_refreshes_closed_mutations_and_stops_escape():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend editor regression tests")
    script = r"""
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, SourceTextModule, SyntheticModule } from "node:vm";
const elements = new Map();
const focused = {isConnected: true, focus() {}};
function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
        dataset: {}, value: "", checked: false, disabled: false, hidden: false, open: false,
        isConnected: true, listeners: {}, classList: {toggle() {}},
        addEventListener(name, fn) {this.listeners[name] = fn;},
        focus() {}, reset() {}, replaceChildren() {}, setAttribute() {},
        showModal() {this.open = true;},
        close() {this.open = false; this.listeners.close?.();},
    });
    return elements.get(selector);
}
const state = {view: "library", libraryMode: "curated", currentId: null};
const data = {id: "one", title: "Saved", notes: "", tags: [], favorite: false,
    status: "needs_review", prerequisites: "", revision: 1, content: "printf safe",
    source_kind: "snippet", source_session_id: "origin", source_turn_index: 0,
    source_title: "Original", source: "cli", source_status: "available"};
let listReads = 0, finishWrite;
const api = async (url, options = {}) => {
    if (["PUT", "DELETE"].includes(options.method)) return new Promise(resolve => {finishWrite = resolve;});
    if (url === "/api/solutions/one") return {...data};
    if (url.startsWith("/api/solutions?")) {listReads += 1; return {total: 0, solutions: [], offset: 0, has_more: false};}
    throw new Error("Unexpected API request " + url);
};
const modules = {
    "../api.js": {api},
    "../state.js": {state, showOnly() {}, setLayoutWide() {}},
    "../utils.js": {$: element, $$: () => [], debounce: fn => fn, esc: value => value,
        fmtDate: () => "today", sessionHash: () => "#/session/origin?turn=1",
        srcMeta: () => ({label: "CLI"}), toast() {}, withTransition: fn => fn()},
    "../icons.js": {icon: () => ""},
    "./detail.js": {openSession() {}, teardownReading() {}},
};
const context = createContext({URLSearchParams, document: {activeElement: focused, createElement: () => ({})},
    window: {addEventListener() {}, confirm: () => true}, location: {hash: "#/library/curated"},
    history: {pushState() {}}, navigator: {}});
const library = new SourceTextModule(readFileSync("mark/web/js/views/library.js", "utf8"), {context});
await library.link(path => new SyntheticModule(Object.keys(modules[path]), function () {
    for (const [name, value] of Object.entries(modules[path])) this.setExport(name, value);
}, {context}));
await library.evaluate();
library.namespace.setupLibrary();
const dialog = element("#solutionDialog");
await library.namespace.openSolutionDialog({solutionId: "one"});
const saving = element("#solutionForm").listeners.submit({preventDefault() {}});
dialog.close();
finishWrite({ok: true});
await saving;
assert.equal(listReads, 1, "Save must refresh committed state after editor closes");
await library.namespace.openSolutionDialog({solutionId: "one"});
const deleting = element("#solutionDelete").listeners.click();
dialog.close();
finishWrite({ok: true});
await deleting;
assert.equal(listReads, 2, "Delete must refresh committed state after editor closes");
await library.namespace.openSolutionDialog({solutionId: "one"});
let prevented = false, stopped = false;
dialog.listeners.keydown({key: "Escape", preventDefault() {prevented = true;}, stopPropagation() {stopped = true;}});
assert.equal(prevented && stopped, true, "Escape must not reach the page navigation handler");
assert.equal(dialog.open, false);
"""
    result = subprocess.run(
        [node, "--experimental-vm-modules", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_curated_missing_or_mismatched_source_is_404(client, curated_source):
    assert (
        client.post(
            "/api/solutions/preview",
            json={
                "kind": "answer",
                "session_id": curated_source["id"],
                "turn_index": 99,
            },
        ).status_code
        == 404
    )
    snippet = client.get("/api/snippets").json()["snippets"][0]
    assert (
        client.post(
            "/api/solutions/preview",
            json={
                "kind": "snippet",
                "session_id": "wrong-session",
                "snippet_id": snippet["id"],
            },
        ).status_code
        == 404
    )
    assert client.get("/api/solutions/missing").status_code == 404
    assert client.get("/api/solutions?limit=101").status_code == 422
    assert client.get("/api/solutions?status=unknown").status_code == 422


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
