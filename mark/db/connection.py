from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Literal
from uuid import uuid4

from .. import config


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """A connection scoped to one unit of work: commit on success, roll back on
    error, and always close.

    Use this for multi-statement writes that need the raw connection (e.g.
    ``executemany``/``executescript`` or incremental per-batch commits); prefer
    :func:`cursor` for single-cursor work. Unlike a bare ``with connect()``,
    this guarantees the handle is closed rather than leaning on GC.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def temporary_id_table(
    conn: sqlite3.Connection,
    ids: Iterable[str | int] | None,
    *,
    id_type: Literal["TEXT", "INTEGER"] = "TEXT",
) -> Iterator[str | None]:
    """Materialize an optional ID scope without consuming bind variables.

    SQLite's supported bind-variable ceiling varies by build. A temporary table
    keeps large collection scopes portable while preserving one global SQL
    ranking operation. The generated table name is internal and connection-local.
    """
    if ids is None:
        yield None
        return

    table = f"_mark_ids_{uuid4().hex}"
    conn.execute(f"CREATE TEMP TABLE {table}(id {id_type} PRIMARY KEY) WITHOUT ROWID")
    try:
        conn.executemany(
            f"INSERT OR IGNORE INTO {table}(id) VALUES (?)",
            ((value,) for value in ids),
        )
        yield table
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


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
