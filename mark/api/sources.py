from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .. import background, config, db, ingest
from ..repositories import stats as stats_repo
from ..schemas import SourceInfo, StatusResponse

router = APIRouter()


def _status_payload() -> dict[str, Any]:
    model = db.get_meta("embed_model") or ""
    st = background.status_snapshot()
    st["embed_model"] = model
    st["semantic"] = bool(model) and not model.startswith("builtin")
    st["auto_sync"] = config.AUTO_SYNC
    st["sync_interval"] = config.SYNC_INTERVAL
    st["last_ingest"] = db.get_meta("last_ingest")
    st["resume_cmd"] = config.RESUME_COMMAND
    return st


@router.get("/api/status", response_model=StatusResponse)
def api_status() -> dict[str, Any]:
    return _status_payload()


@router.get("/api/sources", response_model=list[SourceInfo])
def api_sources() -> list[dict[str, Any]]:
    """Effective per-source config (defaults < sources.toml < env) for the UI.

    ``indexed`` counts existing sessions for the adapter even when it is disabled,
    since disabling keeps already-indexed rows.
    """
    by_source = stats_repo.source_counts()
    out: list[dict[str, Any]] = []
    for s in ingest.WATCHED_SOURCES:
        cfg = config.resolve_source_config(s.default_config())
        out.append(
            {
                "key": cfg.key,
                "label": cfg.label or cfg.key,
                "kind": "watched",
                "enabled": cfg.enabled,
                "roots": [str(r) for r in cfg.roots],
                "exists": any(Path(r).exists() for r in cfg.roots),
                "indexed": sum(by_source.get(n, 0) for n in s.row_sources),
            }
        )
    for imp in ingest.IMPORT_SOURCES:
        out.append(
            {
                "key": imp.key,
                "label": imp.label or imp.key,
                "kind": "import",
                "enabled": True,
                "roots": [],
                "exists": True,
                "indexed": by_source.get(imp.key, 0),
            }
        )
    return out


@router.post("/api/reindex", response_model=StatusResponse)
def api_reindex(rebuild: bool = False) -> dict[str, Any]:
    started = background.start_reindex(rebuild=rebuild)
    st = _status_payload()
    st["started"] = started
    return st
