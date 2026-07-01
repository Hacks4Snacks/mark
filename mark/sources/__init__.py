from __future__ import annotations

from .base import ImportSource, ProgressCb, WatchedSource
from .chatgpt import ChatGptSource
from .claude_code import ClaudeCodeSource
from .cline import ClineSource
from .copilot_cli import CopilotCliSource
from .cursor import CursorSource
from .grok import GrokSource
from .vscode import VSCodeSource

WATCHED_SOURCES: list[WatchedSource] = [
    VSCodeSource(),
    CopilotCliSource(),
    ClineSource(),
    CursorSource(),
    ClaudeCodeSource(),
]

#: On-demand importers for user-supplied export files (cloud assistants).
IMPORT_SOURCES: list[ImportSource] = [
    ChatGptSource(),
    GrokSource(),
]

__all__ = [
    "IMPORT_SOURCES",
    "WATCHED_SOURCES",
    "ChatGptSource",
    "ClaudeCodeSource",
    "ClineSource",
    "CopilotCliSource",
    "CursorSource",
    "GrokSource",
    "ImportSource",
    "ProgressCb",
    "VSCodeSource",
    "WatchedSource",
]
