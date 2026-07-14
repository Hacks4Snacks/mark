from __future__ import annotations

import hashlib
import sqlite3

import pytest


class _LimitedCursor:
    def __init__(self, cursor, connection, maximum: int):
        self._cursor = cursor
        self._connection = connection
        self._maximum = maximum

    @property
    def connection(self):
        return self._connection

    def execute(self, sql, parameters=()):
        if len(parameters) > self._maximum:
            raise sqlite3.OperationalError("too many SQL variables")
        self._cursor.execute(sql, parameters)
        return self

    def executemany(self, sql, parameters):
        self._cursor.executemany(sql, parameters)
        return self

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _LimitedConnection:
    def __init__(self, connection, maximum: int):
        self._connection = connection
        self._maximum = maximum

    def cursor(self):
        return _LimitedCursor(self._connection.cursor(), self, self._maximum)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.fixture
def limit_sql_variables(monkeypatch):
    """Enforce a portable per-statement SQLite bind-variable ceiling."""
    from mark import db
    from mark.db import connection

    real_connect = connection.connect

    def apply(maximum: int = 999) -> None:
        def limited_connect():
            return _LimitedConnection(real_connect(), maximum)

        monkeypatch.setattr(connection, "connect", limited_connect)
        monkeypatch.setattr(db, "connect", limited_connect)

    return apply


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point config at a fresh temp DB, force the builtin embedder, init schema."""
    from mark import config, db, embeddings, ingest, search

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
    ingest.mark_semantic_unverified()
    yield


@pytest.fixture
def client(monkeypatch):
    """A FastAPI TestClient whose lifespan does NOT import real local sources."""
    from mark import background
    from mark.app import create_app

    monkeypatch.setattr(background, "start", lambda **kwargs: None)
    monkeypatch.setattr(background, "stop", lambda: None)
    monkeypatch.setattr(background, "mark_http_ready", lambda: None)
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
        from mark import persist

        persist.write_session(session)

    return _persist
