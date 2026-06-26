"""Queries backing the sidebar stats and the per-source overview counts."""

from __future__ import annotations

from typing import Any

from .. import db


def source_counts() -> dict[str, int]:
    """Number of sessions per ``source`` string (includes automation)."""
    with db.cursor() as cur:
        return {
            r["source"]: r["n"]
            for r in cur.execute(
                "SELECT source, COUNT(*) n FROM sessions GROUP BY source"
            ).fetchall()
        }


def overview() -> dict[str, Any]:
    """Headline counts and aggregates for the sidebar stat cards."""
    with db.cursor() as cur:
        sources = {
            r["source"]: r["n"]
            for r in cur.execute(
                "SELECT source, COUNT(*) n FROM sessions GROUP BY source"
            ).fetchall()
        }
        visible = cur.execute(
            "SELECT COUNT(*) FROM sessions WHERE source != 'automation'"
        ).fetchone()[0]
        turns = cur.execute(
            "SELECT COUNT(*) FROM turns t JOIN sessions s ON s.id = t.session_id "
            "WHERE s.source != 'automation'"
        ).fetchone()[0]
        files = cur.execute(
            "SELECT COUNT(DISTINCT f.file_path) FROM session_files f "
            "JOIN sessions s ON s.id = f.session_id WHERE s.source != 'automation'"
        ).fetchone()[0]
        tags = cur.execute(
            "SELECT COUNT(DISTINCT t.tag) FROM tags t "
            "JOIN sessions s ON s.id = t.session_id WHERE s.source != 'automation'"
        ).fetchone()[0]
        agg = cur.execute(
            "SELECT COALESCE(SUM(est_cost_usd),0) c, COALESCE(SUM(premium_requests),0) p, "
            "COALESCE(SUM(duration_seconds),0) d FROM sessions WHERE source != 'automation'"
        ).fetchone()
        rng = cur.execute(
            "SELECT MIN(COALESCE(created_at, updated_at)) mn, "
            "MAX(COALESCE(updated_at, created_at)) mx FROM sessions"
        ).fetchone()
    return {
        "sessions": visible,
        "automation": sources.get("automation", 0),
        "by_source": sources,
        "turns": turns,
        "files": files,
        "tags": tags,
        "total_cost_usd": round(agg["c"], 2),
        "premium_requests": int(agg["p"]),
        "total_duration_seconds": agg["d"],
        "date_min": rng["mn"],
        "date_max": rng["mx"],
        "embed_model": db.get_meta("embed_model"),
        "last_ingest": db.get_meta("last_ingest"),
    }
