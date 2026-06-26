"""Data-access layer: named query functions over the SQLite database.

Routers and services call these instead of embedding SQL inline, so the API
layer stays thin and the queries live in one place per domain.
"""

from __future__ import annotations

from . import sessions, snippets, stats, usage

__all__ = ["sessions", "snippets", "stats", "usage"]
