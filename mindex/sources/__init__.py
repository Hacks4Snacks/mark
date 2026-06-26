"""Source-adapter registry.

Each adapter discovers and parses one on-disk store of AI conversations and
produces the canonical session dict consumed by :func:`mindex.persist.write_session`.
``mindex.ingest`` loops over :data:`WATCHED_SOURCES` for both change detection
and importing, so adding a source is one new module plus one line here.
"""

from __future__ import annotations

from .base import ProgressCb, WatchedSource
from .cline import ClineSource
from .copilot_cli import CopilotCliSource
from .vscode import VSCodeSource

WATCHED_SOURCES: list[WatchedSource] = [
    VSCodeSource(),
    CopilotCliSource(),
    ClineSource(),
]

__all__ = [
    "WATCHED_SOURCES",
    "WatchedSource",
    "ProgressCb",
    "VSCodeSource",
    "CopilotCliSource",
    "ClineSource",
]
