from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .. import background, config, db, ingest, persist
from ..repositories import stats as stats_repo
from ..schemas import HealthResponse, ReindexStatusResponse, SourceInfo, StatusResponse

router = APIRouter()


def _status_payload(semantic: dict[str, Any] | None = None) -> dict[str, Any]:
    if semantic is None:
        semantic = ingest.semantic_status()
    model = semantic["model"]
    st = background.status_snapshot()
    st["embed_model"] = model
    st["semantic_active"] = semantic["active"]
    st["semantic"] = semantic["active"] and not model.startswith("builtin")
    st["semantic_pending"] = semantic["pending"]
    st["semantic_generation"] = semantic["generation"]
    st["semantic_fingerprint"] = semantic["fingerprint"]
    st["semantic_target_fingerprint"] = semantic["target_fingerprint"]
    st["semantic_error"] = semantic["error"]
    st["auto_sync"] = config.AUTO_SYNC
    st["sync_interval"] = config.SYNC_INTERVAL
    st["last_ingest"] = db.get_meta("last_ingest")
    st["resume_cmd"] = config.RESUME_COMMAND
    st["ask_enabled"] = config.ENABLE_ASK
    return st


@router.get("/api/status", response_model=StatusResponse)
def api_status() -> dict[str, Any]:
    return _status_payload()


def _root_status(path: Path) -> dict[str, Any]:
    """Probe only root metadata/access, never enumerate or read source content."""
    try:
        is_directory = path.is_dir()
        path.stat()
        mode = os.R_OK | (os.X_OK if is_directory else 0)
        if not os.access(path, mode):
            return {
                "path": str(path),
                "state": "unreadable",
                "error": "Read access denied",
            }
        return {"path": str(path), "state": "present", "error": None}
    except (FileNotFoundError, NotADirectoryError):
        return {"path": str(path), "state": "missing", "error": None}
    except PermissionError as exc:
        return {"path": str(path), "state": "unreadable", "error": str(exc)[:2000]}
    except OSError as exc:
        return {"path": str(path), "state": "error", "error": str(exc)[:2000]}


def _source_health(
    cfg: config.SourceConfig,
    roots: list[dict[str, Any]],
    history: dict[str, Any] | None,
) -> tuple[str, bool, str | None, str]:
    changed = bool(
        history
        and history.get("configuration") is not None
        and history.get("configuration") != persist.source_config_fingerprint(cfg)
    )
    current_error = None if changed else (history or {}).get("current_error")
    present = sum(root["state"] == "present" for root in roots)
    path_error = next((root["error"] for root in roots if root.get("error")), None)
    if not cfg.enabled:
        return (
            "disabled",
            changed,
            None,
            "Re-enable this adapter in source configuration to resume scanning. Indexed conversations are retained.",
        )
    if path_error:
        return (
            "error",
            changed,
            path_error,
            "Check read permissions and container mounts for the reported root, then retry the scan.",
        )
    if not present:
        return (
            "missing",
            changed,
            current_error,
            "No usable root was detected. Check source installation, configured roots, and container mount paths.",
        )
    if current_error:
        return (
            "error",
            changed,
            current_error,
            "The last adapter attempt failed. Check the error, source files and permissions, then retry.",
        )
    if present < len(roots):
        return (
            "degraded",
            changed,
            None,
            "Some configured roots are missing. Check each root; available roots can still contribute sessions.",
        )
    if changed:
        return (
            "detected",
            True,
            None,
            "Paths or options changed since the recorded scan. Retry to check the current configuration.",
        )
    if (
        history
        and history.get("status") in ("ok", "unchanged")
        and history.get("last_success_at")
    ):
        return (
            "healthy",
            False,
            None,
            "Last scan completed. Counts include hidden sessions; use search filters if expected results are not visible.",
        )
    return (
        "detected",
        False,
        None,
        "Roots are accessible; a successful scan has not been recorded for this configuration yet.",
    )


@router.get("/api/sources", response_model=list[SourceInfo])
def api_sources() -> list[dict[str, Any]]:
    """Effective per-source config (defaults < sources.toml < env) for the UI.

    ``indexed`` counts existing sessions for the adapter even when it is disabled,
    since disabling keeps already-indexed rows.
    """
    by_source = stats_repo.source_counts()
    by_adapter = stats_repo.source_adapter_counts()
    legacy_by_source = stats_repo.legacy_source_counts()
    with db.cursor() as cur:
        histories = persist.source_health_records(cur)
    out: list[dict[str, Any]] = []
    for s in ingest.WATCHED_SOURCES:
        history = histories.get(s.key)
        indexed = by_adapter.get(s.key, 0) + sum(
            legacy_by_source.get(n, 0) for n in s.row_sources
        )
        try:
            cfg = config.resolve_source_config(s.default_config())
            roots = [_root_status(Path(root)) for root in cfg.roots]
            health, changed, error, action = _source_health(cfg, roots, history)
        except Exception as exc:
            out.append(
                {
                    "key": s.key,
                    "label": s.key,
                    "kind": "watched",
                    "enabled": True,
                    "roots": [],
                    "exists": False,
                    "indexed": indexed,
                    "health": "error",
                    "history": history,
                    "error": str(exc)[:2000],
                    "action": "Source configuration or discovery failed. Check its roots/options and environment overrides; other adapters are unaffected.",
                }
            )
            continue
        out.append(
            {
                "key": cfg.key,
                "label": cfg.label or cfg.key,
                "kind": "watched",
                "enabled": cfg.enabled,
                "roots": [str(r) for r in cfg.roots],
                "exists": any(root["state"] == "present" for root in roots),
                "indexed": indexed,
                "root_status": roots,
                "health": health,
                "history": history,
                "configuration_changed": changed,
                "error": error,
                "action": action,
            }
        )
    for imp in ingest.IMPORT_SOURCES:
        history = histories.get(imp.key)
        error = (history or {}).get("current_error")
        out.append(
            {
                "key": imp.key,
                "label": imp.label or imp.key,
                "kind": "import",
                "enabled": True,
                "roots": [],
                "exists": True,
                "indexed": by_source.get(imp.key, 0),
                "health": "error" if error else "import",
                "history": history,
                "error": error,
                "action": "Import a new export using Add. Imported sources are not watched and cannot be refreshed by a re-scan.",
            }
        )
    return out


@router.get("/api/health", response_model=HealthResponse)
def api_health() -> dict[str, Any]:
    """Detailed on-demand diagnostics; no scans, inference or repair on GET."""
    index = ingest.semantic_status(include_coverage=True)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "coordinator": _status_payload(index),
        "sources": api_sources(),
        "index": index,
    }


@router.post("/api/reindex", response_model=ReindexStatusResponse)
def api_reindex(rebuild: bool = False, repair_semantic: bool = False) -> dict[str, Any]:
    admission = background.request_reindex(
        rebuild=rebuild, repair_semantic=repair_semantic
    )
    st = _status_payload()
    st["started"] = admission == "accepted"
    st["admission"] = admission
    return st
