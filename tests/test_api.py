from __future__ import annotations


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


def test_missing_session_is_404(client):
    assert client.get("/api/sessions/nope").status_code == 404


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
