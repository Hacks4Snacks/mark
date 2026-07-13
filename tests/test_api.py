from __future__ import annotations

import threading
from pathlib import Path

import pytest


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
        background._status.update(
            running=False,
            queued=False,
            message="idle",
            last_result=None,
            last_error=None,
            started_at=None,
            finished_at=None,
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


def test_ask_enabled_exposes_routes(client):
    # The `client` fixture enables the Ask feature, so its routes are mounted
    # and /api/status advertises it.
    assert client.get("/api/status").json()["ask_enabled"] is True
    assert client.get("/api/ask/status").status_code == 200


def test_status_distinguishes_active_builtin_semantic_index(client):
    response = client.post(
        "/api/notes", json={"title": "Status", "text": "semantic status body"}
    )
    assert response.status_code == 200
    status = client.get("/api/status").json()
    assert status["semantic_pending"] is False
    assert status["semantic_fingerprint"]
    assert status["semantic_target_fingerprint"] is None
    assert status["semantic_generation"] > 0
    # Historical ``semantic`` means transformer quality; active builtin search
    # is represented independently by the active fingerprint + pending fields.
    assert status["semantic"] is False
    assert status["semantic_active"] is True


def test_ask_disabled_by_default_hides_routes(monkeypatch):
    # With the feature flag off (the shipped default) the ask routes are not
    # mounted and the collection-scoped ask is guarded at request time.
    from fastapi.testclient import TestClient

    from mark import background, config
    from mark.app import create_app

    monkeypatch.setattr(background, "start", lambda: None)
    monkeypatch.setattr(background, "stop", lambda: None)
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
    from mark import embeddings

    monkeypatch.setattr(
        embeddings,
        "get_embedder",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    response = client.post(
        "/api/notes",
        json={"title": "Durable", "text": "saved despite embedding failure"},
    )
    assert response.status_code == 200
    sid = response.json()["id"]
    assert client.get(f"/api/sessions/{sid}").status_code == 200
    status = client.get("/api/status").json()
    assert status["semantic_pending"] is True
    assert status["semantic_error"] == "offline"


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
    response = client.get(
        f"/api/sessions/legacy-inline/attachments/{attachment['id']}/download"
    )
    assert response.status_code == 404
    assert b"legacy secret" not in response.content


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
