from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .. import config, db, visibility

# Shell-ish languages treated as runnable "commands" in the library.
SHELL_LANGS = (
    "bash",
    "sh",
    "shell",
    "shellscript",
    "zsh",
    "console",
    "shell-session",
    "sh-session",
    "shellsession",
    "powershell",
    "ps1",
)


def _snippet_where(
    *,
    q: str = "",
    language: str = "",
    commands: bool = False,
    repo: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list[Any]]:
    """One scope for snippets, their totals, and the unfiltered facet counts."""
    where = [
        "cb.content IS NOT NULL",
        "LENGTH(TRIM(cb.content)) > 1",
    ]
    visible, visible_params = visibility.sql_where("s")
    params: list[Any] = list(visible_params)
    where.append(visible)
    if commands:
        placeholders = ",".join("?" * len(SHELL_LANGS))
        where.append(f"LOWER(cb.language) IN ({placeholders})")
        params.extend(SHELL_LANGS)
    elif language:
        where.append("cb.language = ?")
        params.append(language)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("cb.content LIKE ? ESCAPE '\\'")
        params.append(f"%{esc}%")
    if repo:
        where.append("s.repository = ?")
        params.append(repo)
    # Match conversation search: inclusive UTC calendar dates on the session's
    # updated timestamp, falling back to creation when it is absent.
    timestamp = "julianday(COALESCE(s.updated_at, s.created_at))"
    if date_from:
        where.append(f"{timestamp} >= julianday(?)")
        params.append(date_from)
    if date_to:
        # SQL day arithmetic also handles 9999-12-31 without Python overflow.
        where.append(f"{timestamp} < julianday(?) + 1")
        params.append(date_to)
    return " AND ".join(where), params


def languages() -> list[dict[str, Any]]:
    """Visible, browsable code-block languages with global snippet counts."""
    where, params = _snippet_where()
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT cb.language AS language, COUNT(*) AS count "
            "FROM code_blocks cb JOIN sessions s ON s.id = cb.session_id "
            "WHERE cb.language IS NOT NULL AND cb.language != '' "
            f"AND {where} GROUP BY cb.language ORDER BY count DESC, language",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def repositories() -> list[dict[str, Any]]:
    """Projects containing visible, browsable snippets, with global counts."""
    where, params = _snippet_where()
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT s.repository, COUNT(*) AS count "
            "FROM code_blocks cb JOIN sessions s ON s.id = cb.session_id "
            "WHERE s.repository IS NOT NULL AND s.repository != '' "
            f"AND {where} GROUP BY s.repository "
            "ORDER BY s.repository COLLATE NOCASE, s.repository",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_snippets(
    *,
    q: str = "",
    language: str = "",
    commands: bool = False,
    repo: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    offset: int = 0,
    limit: int = 80,
) -> dict[str, Any]:
    """A bounded page and its exact total from the same SQLite read snapshot.

    Ordering is stable for an unchanged archive. Concurrent ingestion/curation
    may move entries between requests; this is not a cross-request snapshot.
    """
    where, params = _snippet_where(
        q=q, language=language, commands=commands,
        repo=repo, date_from=date_from, date_to=date_to,
    )
    limit = max(1, min(limit, 300))  # Preserve the existing list API's clamp.
    offset = max(0, min(offset, 2**63 - 1))
    scope = "FROM code_blocks cb JOIN sessions s ON s.id = cb.session_id WHERE " + where
    sql = (
        "SELECT cb.id, cb.session_id, cb.turn_index, cb.language, cb.content, "
        "  s.title AS session_title, s.source, s.repository, s.updated_at, s.created_at "
        + scope
        + " ORDER BY julianday(COALESCE(s.updated_at, s.created_at)) DESC, cb.id DESC "
        "LIMIT ? OFFSET ?"
    )
    with db.transaction() as conn:
        conn.execute("BEGIN")
        total = conn.execute("SELECT COUNT(*) " + scope, params).fetchone()[0]
        rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    return {
        "snippets": [dict(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
    }


def snippets(
    q: str = "", language: str = "", commands: bool = False, limit: int = 80
) -> list[dict[str, Any]]:
    """Compatibility list interface; use list_snippets for paging and totals."""
    return list_snippets(q=q, language=language, commands=commands, limit=limit)["snippets"]


# Curated copies are user-owned, not ingestion-owned code_blocks. Never retain
# transient code-block IDs as provenance or join them with a cascading FK.
_SOLUTION_FIELDS = ("title", "notes", "tags", "favorite", "status", "prerequisites")
_SOLUTION_COLUMNS = (
    "id",
    *_SOLUTION_FIELDS,
    "language",
    "source_kind",
    "source_session_id",
    "source_turn_index",
    "source_title",
    "source",
    "repository",
    "source_timestamp",
    "source_sha256",
    "revision",
    "created_at",
    "updated_at",
)


def _solution_source(cur: sqlite3.Cursor, reference: dict[str, Any]) -> dict[str, Any]:
    """Read one source under the caller's transaction, bounding raw content I/O."""
    kind = reference["kind"]
    if kind == "answer":
        table, body = "turns", "x.assistant_response"
        selector, key = "x.turn_index", reference["turn_index"]
        language, timestamp = "NULL", "x.timestamp"
    else:
        table, body = "code_blocks", "x.content"
        selector, key = "x.id", reference["snippet_id"]
        language, timestamp = "x.language", "s.updated_at"
    row = cur.execute(
        f"SELECT s.id AS source_session_id, s.title AS source_title, s.source, "
        f"s.repository, x.turn_index AS source_turn_index, {language} AS language, "
        f"{timestamp} AS source_timestamp, "
        f"CASE WHEN length(CAST({body} AS BLOB)) <= ? THEN {body} END AS content, "
        f"length(CAST({body} AS BLOB)) AS source_bytes "
        f"FROM {table} x JOIN sessions s ON s.id = x.session_id "
        f"WHERE x.session_id = ? AND {selector} = ?",
        (config.MAX_SOLUTION_CONTENT_CHARS * 4, reference["session_id"], key),
    ).fetchone()
    if row is None:
        raise LookupError(
            "Source no longer available. Refresh the conversation or Library."
        )
    data = dict(row)
    content = data["content"]
    if (content is None and data["source_bytes"]) or (
        content is not None and len(content) > config.MAX_SOLUTION_CONTENT_CHARS
    ):
        raise OverflowError(
            f"Source exceeds {config.MAX_SOLUTION_CONTENT_CHARS:,} characters. "
            "Save a smaller snippet instead; no partial copy was saved."
        )
    if not content or not content.strip():
        raise LookupError("This source has no answer or snippet to save.")
    data.pop("source_bytes")
    data["source_title"] = data["source_title"] or "Untitled conversation"
    data["source_kind"] = kind
    # Includes provenance as well as content: identical snippets from different
    # conversations remain distinct, while double-submit is idempotent.
    identity: list[Any] = [
        kind,
        data["source_session_id"],
        data["source_turn_index"],
        data["language"],
        content,
    ]
    data["source_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return data


def solution_preview(reference: dict[str, Any]) -> dict[str, Any]:
    with db.cursor() as cur:
        return _solution_source(cur, reference)


def _source_for_save(
    cur: sqlite3.Cursor, reference: dict[str, Any], source_sha256: str
) -> dict[str, Any]:
    try:
        return _solution_source(cur, reference)
    except LookupError:
        if reference["kind"] != "snippet":
            raise
        # A sync can replace every code-block ID while the save dialog is open.
        # Rebind only to identical content AND session/turn/language provenance,
        # not whichever snippet happens to occupy the old position afterward.
        candidates = cur.connection.execute(
            "SELECT id FROM code_blocks WHERE session_id = ? "
            "AND length(CAST(content AS BLOB)) <= ? ORDER BY id",
            (reference["session_id"], config.MAX_SOLUTION_CONTENT_CHARS * 4),
        )
        try:
            for row in candidates:
                try:
                    candidate = _solution_source(cur, {**reference, "snippet_id": row["id"]})
                except (LookupError, OverflowError):
                    continue
                if candidate["source_sha256"] == source_sha256:
                    return candidate
        finally:
            candidates.close()
        raise


def _solution_select(*, with_content: bool) -> tuple[str, list[str]]:
    visible, params = visibility.sql_where("s")
    columns = ", ".join(f"c.{field}" for field in _SOLUTION_COLUMNS)
    body = (
        "c.content" if with_content else "substr(c.content, 1, 240) AS content_preview"
    )
    return (
        f"SELECT {columns}, {body}, length(c.content) AS content_chars, "
        f"CASE WHEN s.id IS NOT NULL AND NOT ({visible}) THEN 1 ELSE 0 END AS source_hidden, "
        "CASE WHEN s.id IS NULL THEN 'missing' "
        "WHEN c.source_turn_index IS NOT NULL AND t.id IS NULL THEN 'missing' "
        "WHEN c.source_kind = 'answer' THEN "
        "  CASE WHEN t.assistant_response = c.content THEN 'available' ELSE 'changed' END "
        "WHEN EXISTS (SELECT 1 FROM code_blocks cb "
        "  WHERE cb.session_id = c.source_session_id "
        "  AND cb.turn_index IS c.source_turn_index AND cb.content = c.content "
        "  AND cb.language IS c.language) THEN 'available' ELSE 'changed' "
        "END AS source_status "
        "FROM curated_solutions c LEFT JOIN sessions s ON s.id = c.source_session_id "
        "LEFT JOIN turns t ON t.session_id = c.source_session_id "
        "AND t.turn_index = c.source_turn_index",
        params,
    )


def _solution_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["tags"] = json.loads(data["tags"])
    data["favorite"] = bool(data["favorite"])
    data["source_hidden"] = bool(data["source_hidden"])
    return data


def get_solution(solution_id: str) -> dict[str, Any] | None:
    sql, params = _solution_select(with_content=True)
    with db.cursor() as cur:
        row = cur.execute(sql + " WHERE c.id = ?", [*params, solution_id]).fetchone()
    return _solution_row(row) if row else None


def list_solutions(
    *,
    q: str = "",
    tag: str = "",
    status: str = "",
    favorite: bool = False,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    where = ["1 = 1"]
    params: list[Any] = []
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(
            "(c.title || char(10) || c.notes || char(10) || c.prerequisites || "
            "char(10) || c.tags || char(10) || c.content) LIKE ? ESCAPE '\\'"
        )
        params.append(f"%{escaped}%")
    if tag.strip():
        where.append("EXISTS (SELECT 1 FROM json_each(c.tags) WHERE value = ?)")
        params.append(" ".join(tag.lower().split()))
    if status:
        where.append("c.status = ?")
        params.append(status)
    if favorite:
        where.append("c.favorite = 1")
    predicate = " WHERE " + " AND ".join(where)
    sql, visibility_params = _solution_select(with_content=False)
    with db.transaction() as conn:
        conn.execute("BEGIN")
        total = conn.execute(
            "SELECT COUNT(*) FROM curated_solutions c" + predicate, params
        ).fetchone()[0]
        rows = conn.execute(
            sql + predicate + " ORDER BY c.favorite DESC, c.updated_at DESC, c.id "
            "LIMIT ? OFFSET ?",
            [*visibility_params, *params, limit, offset],
        ).fetchall()
    return {
        "solutions": [_solution_row(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total,
    }


def _solution_values(fields: dict[str, Any]) -> list[Any]:
    return [
        json.dumps(fields[key], ensure_ascii=False) if key == "tags" else fields[key]
        for key in _SOLUTION_FIELDS
    ]


def create_solution(
    reference: dict[str, Any], source_sha256: str, fields: dict[str, Any]
) -> tuple[str, bool]:
    with db.transaction() as conn:
        # Capture and insert atomically with respect to source re-ingestion.
        conn.execute("BEGIN IMMEDIATE")
        source = _source_for_save(conn.cursor(), reference, source_sha256)
        if source["source_sha256"] != source_sha256:
            raise ValueError(
                "Source changed since preview. Close and reopen Save solution."
            )
        existing = conn.execute(
            "SELECT id FROM curated_solutions WHERE source_sha256 = ?", (source_sha256,)
        ).fetchone()
        if existing:
            return existing["id"], False
        solution_id = uuid4().hex
        source_keys = tuple(source)
        columns = ("id", *_SOLUTION_FIELDS, *source_keys)
        conn.execute(
            f"INSERT INTO curated_solutions ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [
                solution_id,
                *_solution_values(fields),
                *(source[key] for key in source_keys),
            ],
        )
    return solution_id, True


def update_solution(solution_id: str, revision: int, fields: dict[str, Any]) -> bool:
    with db.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT revision FROM curated_solutions WHERE id = ?", (solution_id,)
        ).fetchone()
        if not current:
            return False
        if current["revision"] != revision:
            raise ValueError(
                "Solution changed in another window. Reopen it before editing."
            )
        assignments = ", ".join(f"{key} = ?" for key in _SOLUTION_FIELDS)
        conn.execute(
            f"UPDATE curated_solutions SET {assignments}, revision = revision + 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            [*_solution_values(fields), solution_id],
        )
    return True


def delete_solution(solution_id: str, revision: int) -> bool:
    with db.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT revision FROM curated_solutions WHERE id = ?", (solution_id,)
        ).fetchone()
        if not current:
            return False
        if current["revision"] != revision:
            raise ValueError(
                "Solution changed in another window. Reopen it before deleting."
            )
        conn.execute("DELETE FROM curated_solutions WHERE id = ?", (solution_id,))
    return True
