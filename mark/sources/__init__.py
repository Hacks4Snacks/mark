"""Source-adapter registry.

Each adapter discovers and parses one on-disk store of AI conversations and
produces the canonical session dict consumed by :func:`mark.persist.write_session`.
``mark.ingest`` loops over :data:`WATCHED_SOURCES` for both change detection
and importing, so adding a source is one new module plus one line here.
"""

from __future__ import annotations

from .base import ImportSource, ProgressCb, WatchedSource
from .chatgpt import ChatGptSource
from .cline import ClineSource
from .copilot_cli import CopilotCliSource
from .cursor import CursorSource
from .vscode import VSCodeSource

WATCHED_SOURCES: list[WatchedSource] = [
    VSCodeSource(),
    CopilotCliSource(),
    ClineSource(),
    CursorSource(),
]

#: On-demand importers for user-supplied export files (cloud assistants).
IMPORT_SOURCES: list[ImportSource] = [
    ChatGptSource(),
]

__all__ = [
    "WATCHED_SOURCES",
    "IMPORT_SOURCES",
    "WatchedSource",
    "ImportSource",
    "ProgressCb",
    "VSCodeSource",
    "CopilotCliSource",
    "ClineSource",
    "CursorSource",
    "ChatGptSource",
]
