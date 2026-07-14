from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL DEFAULT 'copilot',
    source_adapter TEXT,
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
    indexed_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- User-hidden sessions stay indexed but are filtered from listings and
    -- aggregates until unhidden; never auto-deleted so re-scans can't fight it.
    hidden        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_index          INTEGER NOT NULL,
    user_message        TEXT,
    assistant_response  TEXT,
    thinking            TEXT,
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
    content      TEXT,
    storage_kind TEXT,
    sha256       TEXT,
    capture_version INTEGER
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
    manual      INTEGER NOT NULL DEFAULT 0,
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
    fingerprint TEXT,
    vector      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS collections (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    icon         TEXT,
    color        TEXT,
    rule         TEXT,                          -- JSON search rule; NULL = manual-only
    pinned       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS collection_members (
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    state         TEXT NOT NULL DEFAULT 'include',  -- 'include' | 'exclude'
    added_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (collection_id, session_id)
);

-- Permanently deleted sessions leave a tombstone so a background re-scan can't
-- silently re-import them. Only the id (plus the hash/source at deletion time)
-- is kept; everything else is reclaimed.
CREATE TABLE IF NOT EXISTS tombstones (
    session_id   TEXT PRIMARY KEY,
    source       TEXT,
    content_hash TEXT,
    deleted_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Cheap per-file change signatures so an incremental re-scan can skip files
-- whose contents are unchanged without re-reading and re-hashing them. ``path``
-- is a real source-file path or a synthetic key (e.g. ``cli:<session_id>``);
-- ``signature`` is an opaque stat-derived string compared verbatim.
CREATE TABLE IF NOT EXISTS source_file_stat (
    path       TEXT PRIMARY KEY,
    signature  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_files_session ON session_files(session_id);
CREATE INDEX IF NOT EXISTS idx_tags_session ON tags(session_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag_session ON tags(tag, session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repository);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_collection_members_session ON collection_members(session_id);

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
