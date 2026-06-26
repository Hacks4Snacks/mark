"""SQLite storage layer for mark.

This package is split into:

* :mod:`mark.db.connection` — opening connections, the ``cursor`` context
  manager, and ``meta`` key/value helpers.
* :mod:`mark.db.schema` — the canonical ``CREATE TABLE`` DDL.
* :mod:`mark.db.migrations` — forward-only migrations keyed off
  ``PRAGMA user_version``.

The public surface (``connect``, ``cursor``, ``init_db``, ``get_meta``,
``set_meta``) is re-exported here so callers keep using ``from . import db`` /
``db.cursor()`` unchanged.
"""

from __future__ import annotations

from .connection import connect, cursor, get_meta, set_meta, transaction
from .migrations import run_migrations
from .schema import SCHEMA

__all__ = [
    "SCHEMA",
    "connect",
    "cursor",
    "get_meta",
    "init_db",
    "set_meta",
    "transaction",
]


def init_db() -> None:
    """Create the schema if missing, then apply any pending migrations."""
    with transaction() as conn:
        conn.executescript(SCHEMA)
        run_migrations(conn)
