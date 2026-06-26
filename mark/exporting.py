"""Plain-text / Markdown rendering of a session dict (from ``search.get_session``).

Shared by the web export endpoint and the MCP server so a conversation is
rendered identically wherever it is pulled out of the archive.
"""

from __future__ import annotations

import json
import re
from typing import Any


def session_to_markdown(s: dict[str, Any]) -> str:
    """Render a session dict as clean Markdown (title, meta, then turns)."""
    out: list[str] = [f"# {s.get('title') or 'Untitled conversation'}\n"]
    if s.get("summary"):
        out.append(f"> {s['summary']}\n")
    meta: list[str] = []
    if s.get("source"):
        meta.append(f"- **Source:** {s['source']}")
    if s.get("repository"):
        meta.append(f"- **Repository:** {s['repository']}")
    if s.get("model"):
        meta.append(f"- **Model:** {s['model']}")
    when = s.get("updated_at") or s.get("created_at")
    if when:
        meta.append(f"- **Date:** {when}")
    if s.get("turn_count"):
        meta.append(f"- **Turns:** {s['turn_count']}")
    if s.get("est_cost_usd") is not None:
        meta.append(f"- **Est. cost:** ~${s['est_cost_usd']:.2f}")
    if meta:
        out.append("\n".join(meta) + "\n")
    out.append("---\n")

    turns = s.get("turns") or []
    if turns:
        for t in turns:
            out.append(f"## Turn {int(t.get('turn_index', 0)) + 1}\n")
            if t.get("user_message"):
                out.append("**You:**\n")
                out.append(t["user_message"].strip() + "\n")
            if t.get("assistant_response"):
                tools = t.get("tools")
                if isinstance(tools, str):
                    try:
                        tools = json.loads(tools or "[]")
                    except (TypeError, json.JSONDecodeError):
                        tools = []
                out.append("**Assistant:**\n")
                if tools:
                    out.append("_tools: " + ", ".join(str(x) for x in tools) + "_\n")
                out.append(t["assistant_response"].strip() + "\n")
            out.append("---\n")
    elif (s.get("document") or {}).get("content"):
        out.append(s["document"]["content"].strip() + "\n")

    return "\n".join(out).rstrip() + "\n"


def slug(text: str, fallback: str) -> str:
    """Filesystem-safe slug from a title, with a fallback when empty."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or fallback
