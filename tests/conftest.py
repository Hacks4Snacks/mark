from __future__ import annotations

import hashlib

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point config at a fresh temp DB, force the builtin embedder, init schema."""
    from mark import config, db, embeddings, search

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mark.db")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    # Deterministic, offline embeddings.
    monkeypatch.setattr(embeddings, "_embedder", embeddings._HashEmbed())
    # Don't let Ask's optional cross-encoder reranker download a model mid-test;
    # tests exercise the deterministic fallback (and rerank wiring) explicitly.
    monkeypatch.setattr(embeddings, "_reranker", None)
    monkeypatch.setattr(embeddings, "_reranker_ready", True)
    # The vector matrix cache is keyed by "model:rowcount"; reset it so one
    # test's vectors can never leak into another test that happens to match.
    monkeypatch.setattr(
        search,
        "_vec_cache",
        {"key": None, "ids": None, "sessions": None, "matrix": None},
    )
    db.init_db()
    yield


@pytest.fixture
def client(monkeypatch):
    """A FastAPI TestClient whose lifespan does NOT import real local sources."""
    from mark import background
    from mark.app import create_app

    monkeypatch.setattr(background, "start", lambda: None)
    monkeypatch.setattr(background, "stop", lambda: None)
    # The Ask feature ships disabled by default; enable it so endpoint tests can
    # exercise its routes. Tests that need it off build their own app.
    from mark import config

    monkeypatch.setattr(config, "ENABLE_ASK", True)

    from fastapi.testclient import TestClient

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def make_session():
    """Factory for canonical session dicts (the persist.write_session contract)."""

    def _make(
        sid="s1",
        title="Test session",
        user="how do I fix the auth token timeout",
        asst="Use a refresh token and handle the 401 response.",
        source="vscode",
        repository="kbank",
        code_blocks=None,
    ):
        turns = [
            {
                "turn_index": 0,
                "user_message": user,
                "assistant_response": asst,
                "tools": [],
                "timestamp": "2026-01-01T00:00:00+00:00",
                "files": [],
                "urls": [],
                "code_blocks": code_blocks or [],
            }
        ]
        raw = f"{sid}{title}{user}{asst}".encode()
        return {
            "id": sid,
            "source": source,
            "title": title,
            "workspace_id": None,
            "repository": repository,
            "repo_path": None,
            "requester": None,
            "responder": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-02T00:00:00+00:00",
            "source_path": None,
            "content_hash": hashlib.sha256(raw).hexdigest(),
            "turns": turns,
            "metrics": {},
        }

    return _make


@pytest.fixture
def persist_session():
    """Persist a canonical session dict into the test database."""

    def _persist(session):
        from mark import db, persist

        with db.connect() as conn:
            persist.write_session(conn.cursor(), session)
            conn.commit()

    return _persist
