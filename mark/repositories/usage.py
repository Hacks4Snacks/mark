from __future__ import annotations

from typing import Any

from .. import db, visibility


def usage() -> dict[str, Any]:
    """Totals plus per-day/model/repo/source breakdowns of usage and cost."""
    vclause, vparams = visibility.sql_where()
    with db.cursor() as cur:
        t = cur.execute(
            "SELECT COUNT(*) sessions, COALESCE(SUM(est_cost_usd),0) cost, "
            "COALESCE(SUM(premium_requests),0) premium, COALESCE(SUM(input_tokens),0) input_tokens, "
            "COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(duration_seconds),0) duration, "
            f"COALESCE(SUM(aiu),0) aiu FROM sessions WHERE {vclause}",
            vparams,
        ).fetchone()
        by_day = cur.execute(
            "SELECT substr(COALESCE(updated_at, created_at),1,10) day, COUNT(*) sessions, "
            "COALESCE(SUM(est_cost_usd),0) cost, COALESCE(SUM(premium_requests),0) premium "
            "FROM sessions WHERE COALESCE(updated_at, created_at) IS NOT NULL"
            f" AND {vclause} GROUP BY day ORDER BY day",
            vparams,
        ).fetchall()
        by_model = cur.execute(
            "SELECT COALESCE(NULLIF(model,''),'(unknown)') model, COUNT(*) sessions, "
            "COALESCE(SUM(est_cost_usd),0) cost, COALESCE(SUM(premium_requests),0) premium "
            f"FROM sessions WHERE {vclause}"
            " GROUP BY model ORDER BY cost DESC, sessions DESC LIMIT 12",
            vparams,
        ).fetchall()
        by_repo = cur.execute(
            "SELECT COALESCE(NULLIF(repository,''),'(none)') repository, COUNT(*) sessions, "
            f"COALESCE(SUM(est_cost_usd),0) cost FROM sessions WHERE {vclause}"
            " GROUP BY repository ORDER BY cost DESC, sessions DESC LIMIT 12",
            vparams,
        ).fetchall()
        by_source = cur.execute(
            "SELECT source, COUNT(*) sessions, COALESCE(SUM(est_cost_usd),0) cost, "
            f"COALESCE(SUM(premium_requests),0) premium FROM sessions WHERE {vclause}"
            " GROUP BY source ORDER BY cost DESC",
            vparams,
        ).fetchall()
    return {
        "totals": {
            "sessions": t["sessions"],
            "cost": round(t["cost"], 2),
            "premium": int(t["premium"]),
            "input_tokens": int(t["input_tokens"]),
            "output_tokens": int(t["output_tokens"]),
            "duration": t["duration"] or 0,
            "aiu": round(t["aiu"], 2),
        },
        "by_day": [
            {
                "day": r["day"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 4),
                "premium": int(r["premium"]),
            }
            for r in by_day
        ],
        "by_model": [
            {
                "model": r["model"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 2),
                "premium": int(r["premium"]),
            }
            for r in by_model
        ],
        "by_repo": [
            {
                "repository": r["repository"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 2),
            }
            for r in by_repo
        ],
        "by_source": [
            {
                "source": r["source"],
                "sessions": r["sessions"],
                "cost": round(r["cost"], 2),
                "premium": int(r["premium"]),
            }
            for r in by_source
        ],
    }
