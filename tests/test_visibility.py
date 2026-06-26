from __future__ import annotations

from mark import db, ingest, search
from mark.repositories import sessions as sessions_repo
from mark.repositories import stats as stats_repo
from mark.repositories import usage as usage_repo


def _persist(make_session, persist_session, **kw):
    s = make_session(**kw)
    persist_session(s)
    return s["id"]


# ---------- manual hide / unhide ----------


def test_hidden_session_excluded_from_browse_and_search(make_session, persist_session):
    visible = _persist(make_session, persist_session, sid="v1", title="Visible")
    hidden = _persist(make_session, persist_session, sid="h1", title="Hidden")
    sessions_repo.set_hidden(hidden, True)

    browse_ids = {r["id"] for r in search.browse()}
    assert visible in browse_ids
    assert hidden not in browse_ids

    # Both sessions share the default "auth" text, so only visibility differs.
    search_ids = {r["id"] for r in search.search("auth")}
    assert visible in search_ids
    assert hidden not in search_ids


def test_get_session_still_returns_hidden(make_session, persist_session):
    sid = _persist(make_session, persist_session, sid="h1")
    sessions_repo.set_hidden(sid, True)
    s = search.get_session(sid)
    assert s is not None and s["id"] == sid and s["hidden"] == 1


def test_only_hidden_view_shows_only_hidden(make_session, persist_session):
    _persist(make_session, persist_session, sid="v1")
    hidden = _persist(make_session, persist_session, sid="h1")
    sessions_repo.set_hidden(hidden, True)

    assert {r["id"] for r in search.browse(only_hidden=True)} == {hidden}
    assert {r["id"] for r in search.search("auth", only_hidden=True)} == {hidden}


def test_unhide_restores_visibility(make_session, persist_session):
    sid = _persist(make_session, persist_session, sid="h1")
    sessions_repo.set_hidden(sid, True)
    assert sid not in {r["id"] for r in search.browse()}
    sessions_repo.set_hidden(sid, False)
    assert sid in {r["id"] for r in search.browse()}


def test_hidden_flag_survives_reingest(make_session, persist_session):
    sid = _persist(make_session, persist_session, sid="h1", asst="first answer")
    sessions_repo.set_hidden(sid, True)

    # A re-scan replaces the row when the transcript changes; hide must persist.
    persist_session(make_session(sid="h1", asst="a second, different answer"))

    assert search.get_session(sid)["hidden"] == 1
    assert sid not in {r["id"] for r in search.browse()}


def test_hidden_excluded_from_facets(make_session, persist_session):
    _persist(make_session, persist_session, sid="v1", repository="alpha")
    hidden = _persist(make_session, persist_session, sid="h1", repository="beta")
    sessions_repo.set_hidden(hidden, True)

    repos = {r["name"] for r in search.facets()["repositories"]}
    assert "alpha" in repos
    assert "beta" not in repos


def test_hidden_excluded_from_stats_and_usage(make_session, persist_session):
    _persist(make_session, persist_session, sid="v1")
    hidden = _persist(make_session, persist_session, sid="h1")

    before = stats_repo.overview()["sessions"]
    sessions_repo.set_hidden(hidden, True)
    after = stats_repo.overview()["sessions"]
    assert after == before - 1
    assert usage_repo.usage()["totals"]["sessions"] == after


def test_set_hidden_unknown_session_returns_false():
    assert sessions_repo.set_hidden("nope", True) is False


# ---------- disabling a source ----------


def test_disabled_source_hidden_but_still_counted(
    make_session, persist_session, monkeypatch
):
    sid = _persist(make_session, persist_session, sid="v1", source="vscode")
    assert sid in {r["id"] for r in search.browse()}

    monkeypatch.setenv("MARK_SOURCE_VSCODE_ENABLED", "0")

    # Hidden from listings and aggregates...
    assert sid not in {r["id"] for r in search.browse()}
    assert stats_repo.overview()["sessions"] == 0
    assert "vscode" not in {s["source"] for s in search.facets()["sources"]}
    # ...but kept for the Sources page so re-enabling can bring it back...
    assert stats_repo.source_counts().get("vscode") == 1
    # ...and still reachable directly.
    assert search.get_session(sid) is not None


def test_disabled_source_restored_on_reenable(
    make_session, persist_session, monkeypatch
):
    sid = _persist(make_session, persist_session, sid="v1", source="vscode")
    monkeypatch.setenv("MARK_SOURCE_VSCODE_ENABLED", "0")
    assert sid not in {r["id"] for r in search.browse()}
    monkeypatch.delenv("MARK_SOURCE_VSCODE_ENABLED")
    assert sid in {r["id"] for r in search.browse()}


# ---------- API surface ----------


def test_hide_unhide_api_roundtrip(client):
    sid = client.post(
        "/api/notes", json={"title": "N", "text": "hello searchable world"}
    ).json()["id"]

    hide = client.post(f"/api/sessions/{sid}/hide")
    assert hide.status_code == 200 and hide.json()["hidden"] is True

    res = client.get("/api/search", params={"q": "searchable"}).json()
    assert all(x["id"] != sid for x in res["results"])

    res_hidden = client.get(
        "/api/search", params={"q": "searchable", "hidden": 1}
    ).json()
    assert any(x["id"] == sid for x in res_hidden["results"])

    # The session itself stays reachable while hidden.
    assert client.get(f"/api/sessions/{sid}").json()["hidden"] == 1

    unhide = client.post(f"/api/sessions/{sid}/unhide")
    assert unhide.status_code == 200 and unhide.json()["hidden"] is False
    res2 = client.get("/api/search", params={"q": "searchable"}).json()
    assert any(x["id"] == sid for x in res2["results"])


def test_hide_missing_session_is_404(client):
    assert client.post("/api/sessions/nope/hide").status_code == 404
    assert client.post("/api/sessions/nope/unhide").status_code == 404


def test_collection_drops_hidden_member(client):
    sid = client.post("/api/notes", json={"title": "N", "text": "body text"}).json()[
        "id"
    ]
    cid = client.post("/api/collections", json={"name": "C"}).json()["id"]
    client.post(f"/api/collections/{cid}/members", json={"session_id": sid})
    assert client.get(f"/api/collections/{cid}").json()["count"] == 1

    client.post(f"/api/sessions/{sid}/hide")
    assert client.get(f"/api/collections/{cid}").json()["count"] == 0

    client.post(f"/api/sessions/{sid}/unhide")
    assert client.get(f"/api/collections/{cid}").json()["count"] == 1


# ---------- permanent delete (tombstones) ----------


def test_purge_removes_session_and_children(make_session, persist_session):
    sid = _persist(make_session, persist_session, sid="t1")
    assert sessions_repo.purge(sid) is True

    assert search.get_session(sid) is None
    assert sid not in {r["id"] for r in search.browse()}
    with db.cursor() as cur:
        for table in ("turns", "chunks", "tags", "search_index"):
            n = cur.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            assert n == 0, table
        assert (
            cur.execute(
                "SELECT 1 FROM tombstones WHERE session_id = ?", (sid,)
            ).fetchone()
            is not None
        )


def test_purge_unknown_session_returns_false():
    assert sessions_repo.purge("nope") is False


def test_tombstone_blocks_reingest(make_session, persist_session):
    sid = _persist(make_session, persist_session, sid="t1")
    sessions_repo.purge(sid)

    # A background re-scan re-persisting the same on-disk session must not undo
    # the deletion — write_session is the chokepoint that honors the tombstone.
    persist_session(make_session(sid="t1"))
    assert search.get_session(sid) is None
    assert sid not in {r["id"] for r in search.browse()}


def test_seed_tombstones_marks_deleted_as_unchanged(make_session, persist_session):
    sid = _persist(make_session, persist_session, sid="t1")
    with db.cursor() as cur:
        digest = cur.execute(
            "SELECT content_hash FROM sessions WHERE id = ?", (sid,)
        ).fetchone()[0]
    sessions_repo.purge(sid)

    # The deletion-time hash is what lets a re-scan skip it as unchanged rather
    # than re-parse it and report a phantom "added".
    existing: dict[str, str] = {}
    with db.cursor() as cur:
        ingest._seed_tombstones(cur, existing)
    assert existing.get(sid) == digest


def test_delete_api_permanently_removes(client):
    sid = client.post(
        "/api/notes", json={"title": "Doomed", "text": "delete me forever"}
    ).json()["id"]
    assert client.get(f"/api/sessions/{sid}").status_code == 200

    deleted = client.delete(f"/api/sessions/{sid}")
    assert deleted.status_code == 200 and deleted.json()["ok"] is True

    assert client.get(f"/api/sessions/{sid}").status_code == 404
    res = client.get("/api/search", params={"q": "forever"}).json()
    assert all(x["id"] != sid for x in res["results"])


def test_delete_missing_session_is_404(client):
    assert client.delete("/api/sessions/nope").status_code == 404
