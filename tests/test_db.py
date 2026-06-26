"""Database schema bootstrap, migrations, and meta helpers."""

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

        migrations.run_migrations(con)
        con.commit()

        assert "manual" in {r[1] for r in con.execute("PRAGMA table_info(tags)")}
        assert "thinking" in {r[1] for r in con.execute("PRAGMA table_info(turns)")}
        version = con.execute("PRAGMA user_version").fetchone()[0]
        assert version == migrations.CURRENT_VERSION
    finally:
        con.close()
