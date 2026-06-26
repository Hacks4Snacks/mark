"""SQLite storage layer for mindex.

A single local database holds Copilot sessions, their turns, uploaded
documents, extracted metadata, full-text (FTS5) and vector embeddings.

Searchable text lives in ``chunks`` — one row per turn or document segment —
which is mirrored into the ``search_index`` FTS5 table and the ``embeddings``
table. This keeps keyword and semantic search over a single unit.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL DEFAULT 'copilot',
    title         TEXT,
    summary       TEXT,
    workspace_id  TEXT,
    repository    TEXT,
    repo_path     TEXT,
    requester     TEXT,
    responder     TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    turn_count    INTEGER DEFAULT 0,
    duration_seconds  REAL,
    model         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    premium_requests INTEGER,
    aiu           REAL,
    est_cost_usd  REAL,
    tokens_estimated INTEGER DEFAULT 0,
    source_path   TEXT,
    content_hash  TEXT,
    indexed_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS turns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_index          INTEGER NOT NULL,
    user_message        TEXT,
    assistant_response  TEXT,
    tools               TEXT,
    timestamp           TEXT,
    UNIQUE(session_id, turn_index)
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL DEFAULT 'note',
    filename     TEXT,
    stored_path  TEXT,
    mime         TEXT,
    size_bytes   INTEGER,
    content      TEXT
);

CREATE TABLE IF NOT EXISTS session_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL,
    tool_name   TEXT,
    turn_index  INTEGER,
    UNIQUE(session_id, file_path)
);

CREATE TABLE IF NOT EXISTS session_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ref_type    TEXT NOT NULL,
    ref_value   TEXT NOT NULL,
    turn_index  INTEGER,
    UNIQUE(session_id, ref_type, ref_value)
);

CREATE TABLE IF NOT EXISTS code_blocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_index  INTEGER,
    language    TEXT,
    content     TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    score       REAL DEFAULT 0,
    UNIQUE(session_id, tag)
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,          -- 'turn' | 'document'
    turn_index  INTEGER,
    heading     TEXT,
    content     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id    INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    session_id  TEXT NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_files_session ON session_files(session_id);
CREATE INDEX IF NOT EXISTS idx_tags_session ON tags(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repository);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);

CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    content,
    title,
    tags,
    chunk_id    UNINDEXED,
    session_id  UNINDEXED,
    source_type UNINDEXED,
    turn_index  UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


def get_meta(key: str, default: str | None = None) -> str | None:
    with cursor() as cur:
        row = cur.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_meta(key: str, value: str) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
