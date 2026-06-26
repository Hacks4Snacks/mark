"""SQLite storage layer for mindex.

This package is split into:

* :mod:`mindex.db.connection` — opening connections, the ``cursor`` context
  manager, and ``meta`` key/value helpers.
* :mod:`mindex.db.schema` — the canonical ``CREATE TABLE`` DDL.
* :mod:`mindex.db.migrations` — forward-only migrations keyed off
  ``PRAGMA user_version``.

The public surface (``connect``, ``cursor``, ``init_db``, ``get_meta``,
``set_meta``) is re-exported here so callers keep using ``from . import db`` /
``db.cursor()`` unchanged.
"""

from __future__ import annotations

from .connection import connect, cursor, get_meta, set_meta
from .migrations import run_migrations
from .schema import SCHEMA

__all__ = [
    "connect",
    "cursor",
    "get_meta",
    "set_meta",
    "init_db",
    "SCHEMA",
]


def init_db() -> None:
    """Create the schema if missing, then apply any pending migrations."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        run_migrations(conn)
        conn.commit()
