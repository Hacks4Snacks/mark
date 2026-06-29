from __future__ import annotations

import functools
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("mark")

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
# Web assets ship inside the package so the app works when pip-installed.
WEB_DIR = APP_DIR / "web"

# Data lives in a stable per-user directory by default so it works no matter
# where the app is launched from (pipx/uvx/Docker). Override with MARK_DATA_DIR.
DATA_DIR = Path(os.environ.get("MARK_DATA_DIR", Path.home() / ".mark")).expanduser()
DB_PATH = Path(os.environ.get("MARK_DB_PATH", DATA_DIR / "mark.db")).expanduser()
UPLOADS_DIR = Path(
    os.environ.get("MARK_UPLOADS_DIR", DATA_DIR / "uploads")
).expanduser()

HOST = os.environ.get("MARK_HOST", "127.0.0.1")
PORT = int(os.environ.get("MARK_PORT", "8765"))

# Opt-in RAG: if a local Ollama server is reachable, mark can synthesise
# answers from your past conversations. Everything stays local — no API keys.
OLLAMA_URL = os.environ.get("MARK_OLLAMA_URL", "http://localhost:11434").rstrip("/")
# Empty = auto-pick a reasonable installed model (prefers a small general one).
OLLAMA_MODEL = os.environ.get("MARK_OLLAMA_MODEL", "").strip()

# The Copilot CLI / agent session store (a live SQLite DB). mark reads a
# consistent snapshot of it read-only. Override the path via
# ``[sources.copilot_cli] roots`` in sources.toml or ``MARK_SOURCE_COPILOT_CLI_ROOTS``.
COPILOT_STORE_PATH = Path.home() / ".copilot" / "session-store.db"

# Per-session event logs (token usage, model, duration) written by the CLI.
# Override via ``[sources.copilot_cli] options.state_dir`` in sources.toml.
SESSION_STATE_DIR = Path.home() / ".copilot" / "session-state"

# Command shown in the UI for resuming a Copilot CLI session.
RESUME_COMMAND = os.environ.get("MARK_RESUME_CMD", "copilot --resume {id}")

# When on, mark imports new/updated sessions automatically: once on startup,
# then continuously in the background whenever a session changes or ends. It
# does this by cheaply fingerprinting the on-disk sources every few seconds and
# running an incremental import only when something actually changed.
AUTO_SYNC = os.environ.get("MARK_AUTO_SYNC", "1") not in ("0", "", "false", "False")
# Seconds between source-change checks. Lower = faster pickup, slightly more I/O.
SYNC_INTERVAL = max(5, int(os.environ.get("MARK_SYNC_INTERVAL", "20")))

# Public list prices in USD per 1M tokens: (input, output, cached_input).
# Matched by substring against the model name; override the whole table with a
# JSON file via MARK_PRICING_FILE. These are estimates.
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    "claude-opus": (15.0, 75.0, 1.50),
    "claude-sonnet": (3.0, 15.0, 0.30),
    "claude-haiku": (0.80, 4.0, 0.08),
    # Bare aliases so versioned names (e.g. Cursor's "claude-4.5-opus-high-
    # thinking", "claude-4-sonnet") still price to the right tier.
    "opus": (15.0, 75.0, 1.50),
    "sonnet": (3.0, 15.0, 0.30),
    "haiku": (0.80, 4.0, 0.08),
    "gpt-5-mini": (0.25, 2.0, 0.025),
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4o": (2.50, 10.0, 1.25),
    "gpt-4.1": (2.0, 8.0, 0.50),
    "o3": (2.0, 8.0, 0.50),
    "o1": (15.0, 60.0, 7.50),
    "gemini": (1.25, 5.0, 0.31),
    "grok": (3.0, 15.0, 0.75),
    # Cursor Composer and local/self-hosted models are not billed per token, so
    # they price to zero rather than silently inheriting the sonnet _default.
    "composer": (0.0, 0.0, 0.0),
    "gpt-oss": (0.0, 0.0, 0.0),
    "llama": (0.0, 0.0, 0.0),
    "_default": (3.0, 15.0, 0.30),
}


@functools.lru_cache(maxsize=8)
def _load_pricing_file(
    path: str, _mtime: float
) -> dict[str, tuple[float, float, float]]:
    """Parse a custom pricing JSON, cached by (path, mtime).

    Falls back to the built-in table (with a warning) if the file is unreadable
    or malformed, so a typo never silently yields wrong-but-plausible costs.
    """
    import json

    try:
        raw = json.loads(Path(path).read_text())
        return {k: tuple(v) for k, v in raw.items()}  # type: ignore[misc]
    except (OSError, ValueError) as exc:
        _log.warning(
            "MARK_PRICING_FILE %s ignored (%s); using built-in prices", path, exc
        )
        return MODEL_PRICING


def _load_pricing() -> dict[str, tuple[float, float, float]]:
    raw_path = os.environ.get("MARK_PRICING_FILE")
    if not raw_path:
        return MODEL_PRICING
    path = Path(raw_path).expanduser()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _log.warning("MARK_PRICING_FILE %s not found; using built-in prices", path)
        return MODEL_PRICING
    return _load_pricing_file(str(path), mtime)


def price_for(model: str | None) -> tuple[float, float, float]:
    """(input, output, cached_input) USD per 1M tokens for a model name."""
    table = _load_pricing()
    if model:
        key = model.lower()
        matched_name: str | None = None
        matched_price: tuple[float, float, float] | None = None
        for name, price in table.items():
            if name != "_default" and name in key:
                matched_name, matched_price = name, price
                break
        # Version-agnostic "mini" handling: a name like ``gpt-5.4-mini`` does not
        # contain the literal ``gpt-5-mini`` key (the ``.4-`` breaks it), so it
        # would otherwise match the full ``gpt-5`` tier. When the model is a mini,
        # prefer its family's ``-mini`` price if one is defined.
        if matched_name and "mini" in key and "mini" not in matched_name:
            mini = table.get(f"{matched_name}-mini")
            if mini is not None:
                return mini
        if matched_price is not None:
            return matched_price
    return table.get("_default", (3.0, 15.0, 0.30))


# Preferred transformer model when fastembed is installed.
EMBED_MODEL = os.environ.get("MARK_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
# Dimension used by the always-available built-in hashing vectorizer fallback.
HASH_EMBED_DIM = int(os.environ.get("MARK_HASH_DIM", "1024"))


# Cap CPU used by the transformer embedding backend so a first-time index of a
# large history doesn't peg every core during ingest. fastembed/ONNX otherwise
# spread inference across all logical CPUs; default to half, leaving headroom for
# the user's foreground work. Set MARK_EMBED_THREADS=0 to use all cores (fastest).
def _cgroup_cpu_limit() -> float | None:
    """Effective CPU count from this container's cgroup quota, if any.

    ``os.cpu_count()`` reports the host's logical CPUs and ignores a container CPU
    limit, so in a constrained container the embedding backend would size its
    thread pool to the host and oversubscribe. Reads the cgroup v2 (then v1)
    quota; returns ``None`` when unconstrained or unreadable (e.g. macOS/Windows).
    """
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if raw and raw[0] != "max":
            quota = int(raw[0])
            period = int(raw[1]) if len(raw) > 1 else 100000
            if quota > 0 and period > 0:
                return quota / period
    except (OSError, ValueError):
        pass
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def _default_embed_threads() -> int:
    host = os.cpu_count() or 2
    limit = _cgroup_cpu_limit()
    effective = min(host, limit) if limit else host
    return max(1, int(effective) // 2)


EMBED_THREADS = max(
    0, int(os.environ.get("MARK_EMBED_THREADS", str(_default_embed_threads())))
)

# Max characters per search chunk (the window size used to split long turns).
MAX_CHUNK_CHARS = int(os.environ.get("MARK_MAX_CHUNK_CHARS", "2000"))
# Keyword (FTS) search indexes every chunk so nothing is lost from search. Only
# *embeddings* are capped per session, because semantic search loads all vectors
# into memory and one huge agent transcript would otherwise dominate it. The
# earliest chunks per session win (user prompts are emitted first).
MAX_EMBED_CHUNKS_PER_SESSION = int(
    os.environ.get("MARK_MAX_EMBED_CHUNKS_PER_SESSION", "40")
)
# Cap the size of an agent-created file we snapshot as a viewable attachment.
# Larger files are recorded (path + size) but their content is not stored.
MAX_ATTACHMENT_BYTES = int(os.environ.get("MARK_MAX_ATTACHMENT_BYTES", str(512 * 1024)))

MAX_UPLOAD_BYTES = int(os.environ.get("MARK_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
# Extensions we will extract text from directly (plus .pdf if pypdf installed).
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".cs",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".env",
    ".csv",
    ".html",
    ".css",
    ".xml",
    ".kt",
    ".swift",
    ".scala",
    ".tf",
    ".bicep",
}


def _editor_config_base() -> Path:
    """Per-platform base directory holding VS Code-family editor profiles."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    # linux and friends
    return Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))


def _candidate_storage_roots(subdir: str = "workspaceStorage") -> list[Path]:
    """Known VS Code (stable/Insiders) ``User/<subdir>`` locations per platform."""
    base = _editor_config_base()
    variants = ["Code", "Code - Insiders", "VSCodium"]
    return [base / v / "User" / subdir for v in variants]


def vscode_storage_roots() -> list[Path]:
    """Discover the workspaceStorage directories to scan for chat sessions.

    Returns whichever known platform locations actually exist. Override via
    ``[sources.vscode] roots`` in sources.toml or ``MARK_SOURCE_VSCODE_ROOTS``.
    """
    return [r for r in _candidate_storage_roots() if r.exists()]


def vscode_global_storage_roots() -> list[Path]:
    """Discover ``User/globalStorage`` directories (Cline, Zoo Code, etc.).

    Override via ``[sources.cline] roots`` in sources.toml or
    ``MARK_SOURCE_CLINE_ROOTS``.
    """
    return [r for r in _candidate_storage_roots("globalStorage") if r.exists()]


def _cursor_user_dirs() -> list[Path]:
    """Cursor (stable + Nightly) ``User`` directories per platform."""
    base = _editor_config_base()
    return [base / v / "User" for v in ("Cursor", "Cursor Nightly")]


def cursor_global_db_paths() -> list[Path]:
    """Discover Cursor ``globalStorage/state.vscdb`` files (composer chat history).

    Override via ``[sources.cursor] roots`` in sources.toml or
    ``MARK_SOURCE_CURSOR_ROOTS``.
    """
    dbs = [u / "globalStorage" / "state.vscdb" for u in _cursor_user_dirs()]
    return [p for p in dbs if p.exists()]


def cursor_workspace_storage_roots() -> list[Path]:
    """Discover Cursor ``workspaceStorage`` dirs (map a composer to its repo).

    Override via ``[sources.cursor] options.workspace_roots`` in sources.toml.
    """
    return [
        u / "workspaceStorage"
        for u in _cursor_user_dirs()
        if (u / "workspaceStorage").exists()
    ]


def claude_projects_roots() -> list[Path]:
    """Discover Claude Code transcript roots (``~/.claude/projects``).

    Claude Code writes one JSONL transcript per session under
    ``<base>/projects/<encoded-cwd>/<session-id>.jsonl``. The ``<base>`` is
    ``~/.claude`` unless ``CLAUDE_CONFIG_DIR`` is set (``os.pathsep``-separated for
    several). The returned paths may not exist yet — discovery in the adapter
    guards with ``Path.exists``. Override directly via ``[sources.claude_code]
    roots`` in sources.toml or ``MARK_SOURCE_CLAUDE_CODE_ROOTS``.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if base:
        return [
            Path(p).expanduser() / "projects"
            for p in base.split(os.pathsep)
            if p.strip()
        ]
    return [Path.home() / ".claude" / "projects"]


# Friendly source labels for known Cline-family coding-agent extensions.
CLINE_FAMILY_SOURCES: dict[str, str] = {
    "saoudrizwan.claude-dev": "cline",
    "zoocodeorganization.zoo-code": "zoocode",
    "rooveterinaryinc.roo-cline": "roo",
    "kilocode.kilo-code": "kilocode",
}


@dataclass
class SourceConfig:
    """Effective configuration for one source adapter.

    Resolved with precedence: built-in adapter default < ``sources.toml`` < env.
    ``roots`` meaning is adapter-specific (workspaceStorage dirs for VS Code, the
    store db path for the Copilot CLI, globalStorage dirs for the Cline family).
    Disabling a source stops it being scanned or imported but never deletes
    already-indexed rows.
    """

    key: str
    enabled: bool = True
    roots: list[Path] = field(default_factory=list)
    label: str | None = None
    options: dict[str, Any] = field(default_factory=dict)


def _sources_file_path() -> Path:
    return Path(
        os.environ.get("MARK_SOURCES_FILE", DATA_DIR / "sources.toml")
    ).expanduser()


@functools.lru_cache(maxsize=8)
def _load_sources_file(path: str, _mtime: float) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        return {}
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    srcs = data.get("sources")
    return srcs if isinstance(srcs, dict) else {}


def load_sources_file() -> dict[str, Any]:
    """Parse the optional ``[sources.<key>]`` TOML overrides; {} when absent/bad.

    Cached by (path, mtime) so the background sync loop never re-reads and
    re-parses the file on every fingerprint tick.
    """
    path = _sources_file_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    return _load_sources_file(str(path), mtime)


def _env_flag(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() not in ("0", "", "false", "no", "off")


def resolve_source_config(default: SourceConfig) -> SourceConfig:
    """Merge a built-in default with the TOML file and env overrides.

    Precedence (low  high): adapter default, ``sources.toml``,
    ``MARK_SOURCE_<KEY>_ENABLED`` / ``MARK_SOURCE_<KEY>_ROOTS``.
    """
    enabled = default.enabled
    roots = list(default.roots)
    label = default.label
    options = dict(default.options)

    filecfg = load_sources_file().get(default.key)
    if isinstance(filecfg, dict):
        if "enabled" in filecfg:
            enabled = bool(filecfg["enabled"])
        if filecfg.get("roots"):
            roots = [Path(str(p)).expanduser() for p in filecfg["roots"]]
        if filecfg.get("label"):
            label = str(filecfg["label"])
        if isinstance(filecfg.get("options"), dict):
            options.update(filecfg["options"])

    key_up = default.key.upper()
    enabled = _env_flag(os.environ.get(f"MARK_SOURCE_{key_up}_ENABLED"), enabled)
    env_roots = os.environ.get(f"MARK_SOURCE_{key_up}_ROOTS")
    if env_roots:
        roots = [Path(p).expanduser() for p in env_roots.split(os.pathsep) if p.strip()]

    return SourceConfig(default.key, enabled, roots, label, options)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
