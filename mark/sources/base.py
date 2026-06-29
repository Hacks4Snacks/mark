from __future__ import annotations

import re
import shutil
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .. import config

ProgressCb = Callable[[str], None]

FENCE_RE = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)
URL_RE = re.compile(r"https?://[^\s)>\]]+")


def snapshot_sqlite(src: Path, dest: Path) -> Path:
    """Copy a possibly live or locked SQLite store to ``dest`` for safe reading.

    Prefers SQLite's online backup from a read-only handle; falls back to a raw
    copy of the database and its ``-wal`` sidecar when the source can't be opened
    read-only — a WAL database on a read-only bind mount fails that way. Callers
    open ``dest`` (a private copy they own) and remove it with
    :func:`cleanup_snapshot` when done.
    """
    config.ensure_dirs()
    cleanup_snapshot(dest)
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(dest)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error:
        # The online backup must open the source read-only; a WAL store on a
        # read-only mount can't, so fall back to a filesystem copy (SQLite
        # replays the copied -wal when the private copy is opened read-write).
        for suffix in ("", "-wal"):
            part = Path(f"{src}{suffix}")
            if part.exists():
                shutil.copy2(part, Path(f"{dest}{suffix}"))
    return dest


def cleanup_snapshot(dest: Path) -> None:
    """Delete a snapshot DB created by :func:`snapshot_sqlite` and its sidecars."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{dest}{suffix}").unlink(missing_ok=True)


class WatchedSource(ABC):
    """A live, local store that mark discovers and auto-syncs.

    Implementations own discovery, parsing and their own cheap change signature.
    They must not write to the database directly other than through
    :func:`mark.persist.write_session`, so the persistence/search/UI layers
    stay source-agnostic.

    Discovery is driven by a :class:`mark.config.SourceConfig` (the effective
    enable flag, root paths and options after defaults/file/env are merged), so
    a source never reads its paths from ``config`` directly.
    """

    #: Stable adapter id (e.g. ``"cline"``). Distinct from the per-session
    #: ``source`` string — one adapter may emit several (``cline``/``zoocode``).
    key: str = ""

    #: The ``source`` strings this adapter can write, for display/counting in the
    #: ``/api/sources`` endpoint. One adapter may emit several (e.g. the Cline
    #: family's ``cline``/``zoocode``/``roo``/``kilocode``); adapters with dynamic
    #: names list the known ones.
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
        """Import new/changed sessions; return ``{added, updated, skipped, }``."""


class ImportSource(ABC):
    """A user-supplied export file imported on demand (e.g. a ChatGPT export).

    Unlike :class:`WatchedSource`, there is no live local store to watch — the
    user hands mark an export and each conversation becomes a session via
    :func:`mark.persist.write_session`. Implementations work on the raw bytes
    so they slot into the existing upload action without a temp file.
    """

    #: Stable adapter id and the per-session ``source`` string it writes.
    key: str = ""
    #: Human-friendly name for toasts / the Sources panel.
    label: str = ""

    @abstractmethod
    def detect(self, filename: str, data: bytes) -> bool:
        """True if ``data`` looks like this source's export format."""

    @abstractmethod
    def parse_export(self, data: bytes) -> Iterable[dict[str, Any]]:
        """Yield canonical session dicts parsed from the export bytes."""


def epoch_ms_to_iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def uri_to_path(obj: Any) -> str | None:
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
                return unquote(val)
        if "uri" in obj:
            return uri_to_path(obj["uri"])
    return None


def friendly_repo(path: str | None) -> str | None:
    if not path:
        return None
    p = path.rstrip("/")
    # Strip a trailing file component if this looks like a file path.
    name = Path(p).name
    return name or None


def repo_from_cwd(repository: str | None, cwd: str | None) -> str | None:
    if repository:
        return repository.rstrip("/").split("/")[-1] or None
    if not cwd or "/.paperclip/" in cwd:
        return None
    cwd = cwd.rstrip("/")
    if cwd == str(Path.home()):
        return None
    parts = [p for p in cwd.split("/") if p]
    return parts[-1] if parts else None


def estimate_tokens(text: str | None) -> int:
    return max(0, len(text) // 4) if text else 0


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def ts_diff_seconds(a: str | None, b: str | None) -> float | None:
    da, db_ = parse_iso(a), parse_iso(b)
    if da and db_:
        return max(0.0, (db_ - da).total_seconds())
    return None


def turns_duration(turns: list[dict[str, Any]]) -> float | None:
    stamps = [t.get("timestamp") for t in turns if t.get("timestamp")]
    return ts_diff_seconds(stamps[0], stamps[-1]) if len(stamps) >= 2 else None


def compute_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    input_includes_cache: bool = True,
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


def estimate_metrics(turns: list[dict[str, Any]]) -> dict[str, Any]:
    inp = sum(estimate_tokens(t.get("user_message")) for t in turns)
    outp = sum(estimate_tokens(t.get("assistant_response")) for t in turns)
    return {
        "duration_seconds": turns_duration(turns),
        "model": None,
        "input_tokens": inp,
        "output_tokens": outp,
        "premium_requests": None,
        "aiu": None,
        "est_cost_usd": compute_cost(None, inp, outp),
        "tokens_estimated": 1,
    }


def derive_title(turns: list[dict[str, Any]]) -> str:
    for t in turns:
        msg = (t["user_message"] or "").strip()
        if msg:
            for line in msg.splitlines():
                first_line = line.strip().lstrip("#>*-• ").strip()
                if first_line:
                    return (
                        (first_line[:90] + "...")
                        if len(first_line) > 90
                        else first_line
                    )
    return "Untitled session"
