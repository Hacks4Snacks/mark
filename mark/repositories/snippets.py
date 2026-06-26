"""Queries backing the snippet & command library."""

from __future__ import annotations

from typing import Any

from .. import db

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


def languages() -> list[dict[str, Any]]:
    """Distinct code-block languages with counts, most common first."""
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT cb.language AS language, COUNT(*) AS count "
            "FROM code_blocks cb "
            "WHERE cb.language IS NOT NULL AND cb.language != '' "
            "GROUP BY cb.language ORDER BY count DESC, language"
        ).fetchall()
    return [{"language": r["language"], "count": r["count"]} for r in rows]


def snippets(
    q: str = "", language: str = "", commands: bool = False, limit: int = 80
) -> list[dict[str, Any]]:
    """Code blocks filtered by content/language, newest session first."""
    where = [
        "cb.content IS NOT NULL",
        "LENGTH(TRIM(cb.content)) > 1",
    ]
    params: list[Any] = []
    if commands:
        where.append("LOWER(cb.language) IN (%s)" % ",".join("?" * len(SHELL_LANGS)))
        params.extend(SHELL_LANGS)
    elif language:
        where.append("cb.language = ?")
        params.append(language)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("cb.content LIKE ? ESCAPE '\\'")
        params.append(f"%{esc}%")
    sql = (
        "SELECT cb.id, cb.session_id, cb.turn_index, cb.language, cb.content, "
        "  s.title AS session_title, s.source, s.repository, s.updated_at "
        "FROM code_blocks cb JOIN sessions s ON s.id = cb.session_id "
        "WHERE "
        + " AND ".join(where)
        + " ORDER BY s.updated_at DESC, cb.id DESC LIMIT ?"
    )
    params.append(max(1, min(limit, 300)))
    with db.cursor() as cur:
        rows = cur.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"],
            "session_id": r["session_id"],
            "session_title": r["session_title"],
            "source": r["source"],
            "repository": r["repository"],
            "language": r["language"],
            "content": r["content"],
            "turn_index": r["turn_index"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]
