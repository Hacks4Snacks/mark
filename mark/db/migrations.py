from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable

from .. import config

Migration = Callable[[sqlite3.Connection], None]


def _add_tags_manual_column(conn: sqlite3.Connection) -> None:
    """Add ``tags.manual`` to databases created before manual topics existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tags)")}
    if "manual" not in cols:
        conn.execute("ALTER TABLE tags ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")


def _add_turns_thinking_column(conn: sqlite3.Connection) -> None:
    """Add ``turns.thinking`` to retain model reasoning for auditable records."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
    if "thinking" not in cols:
        conn.execute("ALTER TABLE turns ADD COLUMN thinking TEXT")


def _add_sessions_hidden_column(conn: sqlite3.Connection) -> None:
    """Add ``sessions.hidden`` so existing archives gain the hide/unhide flag."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "hidden" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
        )


def _add_tombstones_table(conn: sqlite3.Connection) -> None:
    """Add the ``tombstones`` table so permanently deleted sessions stay deleted."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tombstones ("
        "session_id TEXT PRIMARY KEY, source TEXT, content_hash TEXT, "
        "deleted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
    )


def _add_source_file_stat_table(conn: sqlite3.Connection) -> None:
    """Add ``source_file_stat`` so incremental re-scans can skip unchanged files."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_file_stat ("
        "path TEXT PRIMARY KEY, signature TEXT NOT NULL)"
    )


def _add_attachment_provenance(conn: sqlite3.Connection) -> None:
    """Trust only classified attachment rows and recapture legacy CLI files."""
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "documents" not in tables:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    for name, sql_type in (
        ("storage_kind", "TEXT"),
        ("sha256", "TEXT"),
        ("capture_version", "INTEGER"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {sql_type}")

    # VS Code session-memory attachments were sourced from Mark's own known
    # memory directory and stored inline; preserve them with explicit trust.
    trusted_inline = conn.execute(
        "SELECT d.id, d.content FROM documents d JOIN sessions s ON s.id = d.session_id "
        "WHERE d.kind = 'attachment' AND s.source = 'vscode' "
        "AND d.content IS NOT NULL"
    ).fetchall()
    for row in trusted_inline:
        raw = row["content"].encode("utf-8")
        if len(raw) <= config.MAX_ATTACHMENT_BYTES:
            conn.execute(
                "UPDATE documents SET stored_path = NULL, size_bytes = ?, "
                "storage_kind = 'inline', sha256 = ?, capture_version = 1 "
                "WHERE id = ?",
                (len(raw), hashlib.sha256(raw).hexdigest(), row["id"]),
            )
        else:
            conn.execute(
                "UPDATE documents SET stored_path = NULL, size_bytes = ?, "
                "content = NULL, storage_kind = 'metadata', sha256 = NULL, "
                "capture_version = 1 WHERE id = ?",
                (len(raw), row["id"]),
            )
    # Pre-fix CLI rows may have originated from denied/request-only tool paths.
    # Delete them rather than trying to infer provenance after the fact, and
    # invalidate both aggregate + per-session signatures so the next scan safely
    # recaptures from authoritative successful events.
    conn.execute(
        "DELETE FROM documents WHERE kind = 'attachment' AND session_id IN "
        "(SELECT id FROM sessions WHERE source = 'cli')"
    )
    if "source_file_stat" in tables:
        conn.execute(
            "DELETE FROM source_file_stat WHERE path = 'srcfp:copilot_cli' "
            "OR path LIKE 'cli:%'"
        )


def _classify_owned_uploads(conn: sqlite3.Connection) -> None:
    """Mark uploaded originals as Mark-owned blobs for lifecycle cleanup."""
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "documents" not in tables:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if "storage_kind" not in cols:
        return
    conn.execute(
        "UPDATE documents SET storage_kind = 'upload', capture_version = 1 "
        "WHERE kind = 'file' AND stored_path IS NOT NULL "
        "AND session_id IN (SELECT id FROM sessions WHERE source = 'upload')"
    )


def _add_embedding_generation(conn: sqlite3.Connection) -> None:
    """Initialize semantic-index generation metadata for legacy databases."""
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "meta" not in tables:
        return
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('embed_generation', '0')"
    )


def _add_embedding_fingerprint_column(conn: sqlite3.Connection) -> None:
    """Add per-row semantic identity; legacy rows remain unclassified."""
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "embeddings" not in tables:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(embeddings)")}
    if "fingerprint" not in cols:
        conn.execute("ALTER TABLE embeddings ADD COLUMN fingerprint TEXT")


def _add_tag_scope_index(conn: sqlite3.Connection) -> None:
    """Support intersection filtering by tag without scanning the tag table."""
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "tags" in tables:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tags_tag_session ON tags(tag, session_id)"
        )


def _add_source_adapter_column(conn: sqlite3.Connection) -> None:
    """Persist stable watched-adapter ownership separately from display source."""
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "sessions" not in tables:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "source_adapter" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN source_adapter TEXT")

    fixed = {
        "vscode": "vscode",
        "cli": "copilot_cli",
        "copilot_memory": "copilot_memory",
        "cursor": "cursor",
        "claude-code": "claude_code",
        "cline": "cline",
        "zoocode": "cline",
        "roo": "cline",
        "kilocode": "cline",
    }
    for source, adapter in fixed.items():
        conn.execute(
            "UPDATE sessions SET source_adapter = ? "
            "WHERE source_adapter IS NULL AND source = ?",
            (adapter, source),
        )

    # Cline-family display names are configurable and unknown forks derive
    # their label from the extension id. Their task path is the durable legacy
    # signal that they were owned by the Cline watched adapter.
    if "source_path" in cols:
        for row in conn.execute(
            "SELECT id, source, source_path FROM sessions "
            "WHERE source_adapter IS NULL AND source_path IS NOT NULL"
        ).fetchall():
            parts = row["source_path"].replace("\\", "/").rstrip("/").split("/")
            if (
                len(parts) >= 2
                and parts[-2] == "tasks"
                and row["id"] == f"{row['source']}-{parts[-1]}"
            ):
                conn.execute(
                    "UPDATE sessions SET source_adapter = 'cline' WHERE id = ?",
                    (row["id"],),
                )


# Ordered list of migrations. Append new ones; never reorder or delete.
# The 1-based index of a migration is its schema version.
MIGRATIONS: list[Migration] = [
    _add_tags_manual_column,
    _add_turns_thinking_column,
    _add_sessions_hidden_column,
    _add_tombstones_table,
    _add_source_file_stat_table,
    _add_attachment_provenance,
    _classify_owned_uploads,
    _add_embedding_generation,
    _add_embedding_fingerprint_column,
    _add_tag_scope_index,
    _add_source_adapter_column,
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
