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
    "IMPORT_SOURCES",
    "WATCHED_SOURCES",
    "ChatGptSource",
    "ClineSource",
    "CopilotCliSource",
    "CursorSource",
    "ImportSource",
    "ProgressCb",
    "VSCodeSource",
    "WatchedSource",
]
