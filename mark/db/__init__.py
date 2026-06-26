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
