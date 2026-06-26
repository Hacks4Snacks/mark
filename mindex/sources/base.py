"""Shared foundation for source adapters.

A *source* turns some on-disk store of AI conversations into the canonical
**session dict** consumed by :func:`mindex.persist.write_session`. This module
holds the adapter base class plus the generic helpers (URI/path handling, token
and cost estimation, timestamp math) that more than one adapter needs.

The session-dict contract
-------------------------
Required keys: ``id``, ``source``, ``title``, ``turns`` (list), ``created_at``,
``updated_at``, ``content_hash``. Everything else is optional/nullable:
``workspace_id``, ``repository``, ``repo_path``, ``requester``, ``responder``,
``source_path``, ``metrics`` (dict), ``extra_files`` (list of
``(path, tool, turn_index)``), ``attachments`` (list of dicts). A plain chat
fills the required core and leaves the coding-oriented fields empty; token/cost
fall back to :func:`_estimate_metrics`.

Each ``turn`` dict has: ``turn_index``, ``user_message``,
``assistant_response``, ``tools`` (list), ``timestamp``, ``files`` (list),
``urls`` (list), ``code_blocks`` (list of ``{language, content}``).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from .. import config

ProgressCb = Callable[[str], None]

_FENCE_RE = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)
_URL_RE = re.compile(r"https?://[^\s)>\]]+")


class WatchedSource(ABC):
    """A live, local store that mindex discovers and auto-syncs.

    Implementations own discovery, parsing and their own cheap change signature.
    They must not write to the database directly other than through
    :func:`mindex.persist.write_session`, so the persistence/search/UI layers
    stay source-agnostic.

    Discovery is driven by a :class:`mindex.config.SourceConfig` (the effective
    enable flag, root paths and options after defaults/file/env are merged), so
    a source never reads its paths from ``config`` directly.
    """

    #: Stable adapter id (e.g. ``"vscode"``). Distinct from the per-session
    #: ``source`` string — one adapter may emit several (``cli``/``automation``).
    key: str = ""

    #: The ``source`` strings this adapter can write, for display/counting in the
    #: ``/api/sources`` endpoint. Adapters with dynamic names list the known ones.
    row_sources: tuple[str, ...] = ()

    def default_config(self) -> config.SourceConfig:
        """Built-in defaults (discovered roots, label, options) before overrides."""
        return config.SourceConfig(key=self.key)

    @abstractmethod
    def fingerprint(self, cfg: config.SourceConfig) -> str:
        """A cheap, stat-only signature that changes when the source changes."""

    @abstractmethod
    def ingest(
        self,
        cur,
        existing: dict[str, str],
        cfg: config.SourceConfig,
        *,
        rebuild: bool,
        progress: ProgressCb | None = None,
    ) -> dict[str, int]:
        """Import new/changed sessions; return ``{added, updated, skipped, …}``."""


# --- low-level helpers -------------------------------------------------------


def _epoch_ms_to_iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _uri_to_path(obj: Any) -> str | None:
    """Best-effort conversion of the many VS Code URI shapes to a readable path."""
    if obj is None:
        return None
    if isinstance(obj, str):
        s = obj
        if s.startswith("file://"):
            return unquote(s[7:])
        return unquote(s) if "://" not in s else s
    if isinstance(obj, dict):
        for key in ("fsPath", "path", "external"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                if obj.get("scheme") in (None, "file") or val.startswith("/"):
                    return unquote(val)
                return unquote(val)
        if "uri" in obj:
            return _uri_to_path(obj["uri"])
    return None


def _friendly_repo(path: str | None) -> str | None:
    if not path:
        return None
    p = path.rstrip("/")
    # Strip a trailing file component if this looks like a file path.
    name = Path(p).name
    return name or None


def _repo_from_cwd(repository: str | None, cwd: str | None) -> str | None:
    if repository:
        return repository.rstrip("/").split("/")[-1] or None
    if not cwd or "/.paperclip/" in cwd:
        return None
    cwd = cwd.rstrip("/")
    if cwd == str(Path.home()):
        return None
    parts = [p for p in cwd.split("/") if p]
    if "microsoft" in parts:
        i = parts.index("microsoft")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else None


# --- metrics: duration, tokens, cost -----------------------------------------


def _estimate_tokens(text: str | None) -> int:
    return max(0, len(text) // 4) if text else 0


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _ts_diff_seconds(a: str | None, b: str | None) -> float | None:
    da, db_ = _parse_iso(a), _parse_iso(b)
    if da and db_:
        return max(0.0, (db_ - da).total_seconds())
    return None


def _turns_duration(turns: list[dict[str, Any]]) -> float | None:
    stamps = [t.get("timestamp") for t in turns if t.get("timestamp")]
    return _ts_diff_seconds(stamps[0], stamps[-1]) if len(stamps) >= 2 else None


def _compute_cost(
    model,
    input_tokens,
    output_tokens,
    cache_read=0,
    cache_write=0,
    input_includes_cache=True,
) -> float:
    pin, pout, pcache = config.price_for(model)
    # The Copilot CLI reports inputTokens INCLUSIVE of cached tokens; Cline-family
    # reports them exclusive. Price fresh input, cache reads, and cache writes
    # separately either way to avoid over-charging.
    fresh_input = (
        max(0, input_tokens - cache_read - cache_write)
        if input_includes_cache
        else input_tokens
    )
    cost = (
        fresh_input * pin
        + cache_read * pcache
        + cache_write * pin * 1.25
        + output_tokens * pout
    ) / 1_000_000
    return round(cost, 4)


def _estimate_metrics(turns: list[dict[str, Any]]) -> dict[str, Any]:
    inp = sum(_estimate_tokens(t.get("user_message")) for t in turns)
    outp = sum(_estimate_tokens(t.get("assistant_response")) for t in turns)
    return {
        "duration_seconds": _turns_duration(turns),
        "model": None,
        "input_tokens": inp,
        "output_tokens": outp,
        "premium_requests": None,
        "aiu": None,
        "est_cost_usd": _compute_cost(None, inp, outp),
        "tokens_estimated": 1,
    }


def _derive_title(turns: list[dict[str, Any]]) -> str:
    for t in turns:
        msg = (t["user_message"] or "").strip()
        if msg:
            for line in msg.splitlines():
                first_line = line.strip().lstrip("#>*-• ").strip()
                if first_line:
                    return (
                        (first_line[:90] + "…") if len(first_line) > 90 else first_line
                    )
    return "Untitled session"
