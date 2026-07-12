from __future__ import annotations

import sqlite3

from mark import db
from mark.db import migrations


def test_user_version_advances_to_latest():
    with db.connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == migrations.CURRENT_VERSION
    assert migrations.CURRENT_VERSION >= 1


def test_init_db_is_idempotent():
    # Running twice must not raise (IF NOT EXISTS + no-op migrations).
    db.init_db()
    db.init_db()
    with db.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "sessions",
        "turns",
        "tags",
        "chunks",
        "embeddings",
        "collections",
    } <= tables


def test_tags_has_manual_column():
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tags)")}
    assert "manual" in cols


def test_attachment_provenance_columns_exist():
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"storage_kind", "sha256", "capture_version"} <= cols


def test_meta_round_trip():
    assert db.get_meta("missing") is None
    assert db.get_meta("missing", "fallback") == "fallback"
    db.set_meta("k", "v")
    assert db.get_meta("k") == "v"
    db.set_meta("k", "w")  # upsert
    assert db.get_meta("k") == "w"


def test_migrations_backfill_pre_column_db(tmp_path):
    """An old DB (user_version=0, no manual/thinking columns) has them added.

    Exercises the ALTER TABLE backfills directly, which the fresh-schema path
    never reaches (the canonical DDL already includes both columns).
    """
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
            "content_hash TEXT)"
        )
        con.execute(
            "CREATE TABLE tags (id INTEGER PRIMARY KEY, session_id TEXT, "
            "tag TEXT, score REAL DEFAULT 0)"
        )
        con.execute(
            "CREATE TABLE turns (id INTEGER PRIMARY KEY, session_id TEXT, "
            "turn_index INTEGER, user_message TEXT, assistant_response TEXT, "
            "tools TEXT, timestamp TEXT)"
        )
        con.execute("PRAGMA user_version = 0")
        con.commit()
        assert "manual" not in {r[1] for r in con.execute("PRAGMA table_info(tags)")}
        assert "thinking" not in {r[1] for r in con.execute("PRAGMA table_info(turns)")}
        assert "hidden" not in {
            r[1] for r in con.execute("PRAGMA table_info(sessions)")
        }

        migrations.run_migrations(con)
        con.commit()

        assert "manual" in {r[1] for r in con.execute("PRAGMA table_info(tags)")}
        assert "thinking" in {r[1] for r in con.execute("PRAGMA table_info(turns)")}
        assert "hidden" in {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tombstones'"
        ).fetchone()
        version = con.execute("PRAGMA user_version").fetchone()[0]
        assert version == migrations.CURRENT_VERSION
    finally:
        con.close()


def test_attachment_provenance_migration_quarantines_legacy_cli(tmp_path):
    import hashlib

    path = tmp_path / "legacy-attachments.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
        con.execute(
            "CREATE TABLE documents ("
            "id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id) "
            "ON DELETE CASCADE, kind TEXT, filename TEXT, stored_path TEXT, "
            "mime TEXT, size_bytes INTEGER, content TEXT)"
        )
        con.execute(
            "CREATE TABLE source_file_stat (path TEXT PRIMARY KEY, signature TEXT)"
        )
        con.executemany(
            "INSERT INTO sessions(id, source) VALUES (?, ?)",
            [("cli-session", "cli"), ("vscode-session", "vscode")],
        )
        con.executemany(
            "INSERT INTO documents(session_id, kind, filename, stored_path, "
            "mime, size_bytes, content) VALUES (?, 'attachment', ?, ?, ?, ?, ?)",
            [
                (
                    "cli-session",
                    "captured-secret.txt",
                    "/tmp/live-secret.txt",
                    "text/plain",
                    13,
                    "legacy secret",
                ),
                (
                    "vscode-session",
                    "memory.md",
                    "/tmp/memory.md",
                    "text/markdown",
                    11,
                    "trusted note",
                ),
            ],
        )
        con.executemany(
            "INSERT INTO source_file_stat(path, signature) VALUES (?, ?)",
            [
                ("srcfp:copilot_cli", "aggregate"),
                ("cli:cli-session", "session"),
                ("srcfp:vscode", "keep"),
            ],
        )
        provenance_index = migrations.MIGRATIONS.index(
            migrations._add_attachment_provenance
        )
        con.execute(f"PRAGMA user_version = {provenance_index}")
        con.commit()

        migrations.run_migrations(con)
        con.commit()

        assert (
            con.execute(
                "SELECT 1 FROM documents WHERE session_id = 'cli-session'"
            ).fetchone()
            is None
        )
        memory = con.execute(
            "SELECT storage_kind, sha256, capture_version, content "
            "FROM documents WHERE session_id = 'vscode-session'"
        ).fetchone()
        assert memory["storage_kind"] == "inline"
        assert memory["capture_version"] == 1
        assert memory["sha256"] == hashlib.sha256(b"trusted note").hexdigest()
        assert memory["content"] == "trusted note"
        assert con.execute(
            "SELECT size_bytes FROM documents WHERE session_id = 'vscode-session'"
        ).fetchone()[0] == len(b"trusted note")
        assert (
            con.execute(
                "SELECT stored_path FROM documents WHERE session_id = 'vscode-session'"
            ).fetchone()[0]
            is None
        )
        signatures = {
            row["path"] for row in con.execute("SELECT path FROM source_file_stat")
        }
        assert signatures == {"srcfp:vscode"}
    finally:
        con.close()
