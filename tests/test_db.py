"""Database schema bootstrap, migrations, and meta helpers."""

from __future__ import annotations

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
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"sessions", "turns", "tags", "chunks", "embeddings", "collections"} <= tables


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
