"""Configuration and path discovery for mindex.

All settings can be overridden via environment variables prefixed with ``MINDEX_``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Project locations -------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
# Web assets ship inside the package so the app works when pip-installed.
WEB_DIR = APP_DIR / "web"

# Data lives in a stable per-user directory by default so it works no matter
# where the app is launched from (pipx/uvx/Docker). Override with MINDEX_DATA_DIR.
DATA_DIR = Path(os.environ.get("MINDEX_DATA_DIR", Path.home() / ".mindex")).expanduser()
DB_PATH = Path(os.environ.get("MINDEX_DB_PATH", DATA_DIR / "mindex.db")).expanduser()
UPLOADS_DIR = Path(
    os.environ.get("MINDEX_UPLOADS_DIR", DATA_DIR / "uploads")
).expanduser()

# --- Server ------------------------------------------------------------------

HOST = os.environ.get("MINDEX_HOST", "127.0.0.1")
PORT = int(os.environ.get("MINDEX_PORT", "8765"))

# --- Sources -----------------------------------------------------------------

# The Copilot CLI / agent session store (a live SQLite DB). mindex reads a
# consistent snapshot of it read-only.
COPILOT_STORE_PATH = Path(
    os.environ.get(
        "MINDEX_COPILOT_STORE", Path.home() / ".copilot" / "session-store.db"
    )
).expanduser()

# Per-session event logs (token usage, model, duration) written by the CLI.
SESSION_STATE_DIR = Path(
    os.environ.get("MINDEX_SESSION_STATE", Path.home() / ".copilot" / "session-state")
).expanduser()

# All sessions are imported and classified; background automation runs (e.g.
# "Paperclip Wake Payload" heartbeats) are simply tagged source='automation' and
# hidden behind a UI toggle. Semantic embedding of those runs is skipped by
# default to keep indexing fast — set to 1 to embed them too.
EMBED_AUTOMATION = os.environ.get("MINDEX_EMBED_AUTOMATION", "0") not in (
    "0",
    "",
    "false",
    "False",
)

# Command shown in the UI for resuming a Copilot CLI session.
RESUME_COMMAND = os.environ.get("MINDEX_RESUME_CMD", "copilot --resume {id}")

# --- Auto-sync ---------------------------------------------------------------

# When on, mindex imports new/updated sessions automatically: once on startup,
# then continuously in the background whenever a session changes or ends. It
# does this by cheaply fingerprinting the on-disk sources every few seconds and
# running an incremental import only when something actually changed.
AUTO_SYNC = os.environ.get("MINDEX_AUTO_SYNC", "1") not in ("0", "", "false", "False")
# Seconds between source-change checks. Lower = faster pickup, slightly more I/O.
SYNC_INTERVAL = max(5, int(os.environ.get("MINDEX_SYNC_INTERVAL", "20")))

# --- Cost estimation ---------------------------------------------------------

# Public list prices in USD per 1M tokens: (input, output, cached_input).
# Matched by substring against the model name; override the whole table with a
# JSON file via MINDEX_PRICING_FILE. These are estimates — edit to taste.
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    "claude-opus": (15.0, 75.0, 1.50),
    "claude-sonnet": (3.0, 15.0, 0.30),
    "claude-haiku": (0.80, 4.0, 0.08),
    "gpt-5-mini": (0.25, 2.0, 0.025),
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4o": (2.50, 10.0, 1.25),
    "gpt-4.1": (2.0, 8.0, 0.50),
    "o3": (2.0, 8.0, 0.50),
    "o1": (15.0, 60.0, 7.50),
    "gemini": (1.25, 5.0, 0.31),
    "_default": (3.0, 15.0, 0.30),
}


def _load_pricing() -> dict[str, tuple[float, float, float]]:
    path = os.environ.get("MINDEX_PRICING_FILE")
    if not path:
        return MODEL_PRICING
    try:
        import json

        raw = json.loads(Path(path).expanduser().read_text())
        return {k: tuple(v) for k, v in raw.items()}  # type: ignore[misc]
    except Exception:
        return MODEL_PRICING


def price_for(model: str | None) -> tuple[float, float, float]:
    """(input, output, cached_input) USD per 1M tokens for a model name."""
    table = _load_pricing()
    if model:
        key = model.lower()
        for name, price in table.items():
            if name != "_default" and name in key:
                return price
    return table.get("_default", (3.0, 15.0, 0.30))


# --- Embeddings --------------------------------------------------------------

# Preferred transformer model when fastembed is installed.
EMBED_MODEL = os.environ.get("MINDEX_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
# Dimension used by the always-available built-in hashing vectorizer fallback.
HASH_EMBED_DIM = int(os.environ.get("MINDEX_HASH_DIM", "1024"))

# Max characters embedded per chunk (keeps memory bounded on huge turns).
MAX_CHUNK_CHARS = int(os.environ.get("MINDEX_MAX_CHUNK_CHARS", "2000"))
# Cap chunks indexed per session so a single huge agent transcript can't bloat
# the index. The full text is still stored; only later chunks skip search/vectors.
MAX_CHUNKS_PER_SESSION = int(os.environ.get("MINDEX_MAX_CHUNKS_PER_SESSION", "40"))
# Cap stored assistant text per agent turn (tool outputs/file dumps are noisy).
MAX_AGENT_TURN_CHARS = int(os.environ.get("MINDEX_MAX_AGENT_TURN_CHARS", "4000"))
# Cap the size of an agent-created file we snapshot as a viewable attachment.
# Larger files are recorded (path + size) but their content is not stored.
MAX_ATTACHMENT_BYTES = int(
    os.environ.get("MINDEX_MAX_ATTACHMENT_BYTES", str(512 * 1024))
)

# --- Uploads -----------------------------------------------------------------

MAX_UPLOAD_BYTES = int(os.environ.get("MINDEX_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
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


def _candidate_storage_roots(subdir: str = "workspaceStorage") -> list[Path]:
    """Known VS Code (stable/Insiders) ``User/<subdir>`` locations per platform."""
    home = Path.home()
    roots: list[Path] = []
    variants = ["Code", "Code - Insiders", "VSCodium"]

    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    else:  # linux and friends
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))

    for v in variants:
        roots.append(base / v / "User" / subdir)
    return roots


def vscode_storage_roots() -> list[Path]:
    """Resolve workspaceStorage directories to scan for chat sessions.

    Honors ``MINDEX_VSCODE_STORAGE`` (os.pathsep-separated) when set, otherwise
    returns whichever known platform locations actually exist.
    """
    override = os.environ.get("MINDEX_VSCODE_STORAGE")
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p.strip()]
    return [r for r in _candidate_storage_roots() if r.exists()]


def vscode_global_storage_roots() -> list[Path]:
    """Resolve ``User/globalStorage`` directories (Cline, Zoo Code, etc.)."""
    override = os.environ.get("MINDEX_VSCODE_GLOBAL_STORAGE")
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p.strip()]
    return [r for r in _candidate_storage_roots("globalStorage") if r.exists()]


# Friendly source labels for known Cline-family coding-agent extensions.
CLINE_FAMILY_SOURCES: dict[str, str] = {
    "saoudrizwan.claude-dev": "cline",
    "zoocodeorganization.zoo-code": "zoocode",
    "rooveterinaryinc.roo-cline": "roo",
    "kilocode.kilo-code": "kilocode",
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
