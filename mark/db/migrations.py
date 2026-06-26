"""Versioned, forward-only schema migrations keyed off ``PRAGMA user_version``.

The canonical schema in :mod:`mark.db.schema` is created with ``IF NOT
EXISTS`` on every startup, so migrations here only handle *evolving* an existing
database (adding columns, backfilling, reshaping). Each migration is a callable
that takes a connection and must be idempotent; the list index + 1 is its
version number. ``run_migrations`` applies every migration whose version is
greater than the database's current ``user_version`` and then records the new
version, so a fresh database fast-forwards to the latest version with no work.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

Migration = Callable[[sqlite3.Connection], None]


def _add_tags_manual_column(conn: sqlite3.Connection) -> None:
    """Add ``tags.manual`` to databases created before manual topics existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tags)")}
    if "manual" not in cols:
        conn.execute("ALTER TABLE tags ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")


# Ordered list of migrations. Append new ones; never reorder or delete.
# The 1-based index of a migration is its schema version.
MIGRATIONS: list[Migration] = [
    _add_tags_manual_column,
]

CURRENT_VERSION = len(MIGRATIONS)


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations and advance ``user_version`` to the latest."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= CURRENT_VERSION:
        return
    for i in range(version, CURRENT_VERSION):
        MIGRATIONS[i](conn)
    # PRAGMA user_version doesn't accept bound parameters.
    conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
