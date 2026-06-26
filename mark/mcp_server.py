"""Mark MCP server — expose your AI-chat archive to MCP clients.

Lets Copilot CLI, Cline, Claude Desktop, and other MCP-aware agents search and
retrieve your past coding conversations as tools, so an agent can recall how you
solved something before. Runs over stdio; everything stays local — no network,
no API keys.

Run:  ``mark-mcp``  (after ``pip install '.[mcp]'``)

Register with an MCP client, e.g. Claude Desktop / Copilot CLI config::

    {
      "mcpServers": {
        "mark": { "command": "mark-mcp" }
      }
    }
"""

from __future__ import annotations

import html
import re

from mcp.server.fastmcp import FastMCP

from . import db, exporting, search

mcp = FastMCP("mark")

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Strip the snippet's <mark> markup back to plain text for an agent."""
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _format_hit(s: dict) -> str:
    line = f"- [{s['id']}] {s.get('title') or 'Untitled'}"
    facts = []
    if s.get("source"):
        facts.append(s["source"])
    if s.get("repository"):
        facts.append(s["repository"])
    when = s.get("updated_at") or s.get("created_at")
    if when:
        facts.append(str(when)[:10])
    if s.get("score") is not None:
        facts.append(f"score {s['score']}")
    out = line + ("  · " + " · ".join(facts) if facts else "")
    snip = _clean(s.get("snippet") or "")
    if snip:
        out += "\n  " + snip
    return out


@mcp.tool()
def search_history(
    query: str,
    mode: str = "hybrid",
    limit: int = 8,
    source: str | None = None,
    repo: str | None = None,
) -> str:
    """Search your past AI coding conversations by meaning or keyword.

    Args:
        query: What to look for (natural language or keywords).
        mode: "hybrid" (default), "semantic", or "keyword".
        limit: Max conversations to return (1-25).
        source: Optionally restrict to a source (vscode, cli, cline, zoocode, chatgpt).
        repo: Optionally restrict to a repository name.

    Returns matching conversations with their id, title, source, repository,
    date, relevance score, and best-matching snippet. Pass a returned id to
    `get_session` to read the whole conversation.
    """
    limit = max(1, min(int(limit), 25))
    results = search.search(
        query,
        mode=mode if mode in ("hybrid", "semantic", "keyword") else "hybrid",
        source=source,
        repo=repo,
        limit=limit,
    )
    if not results:
        return f"No conversations found for: {query!r}"
    head = f"{len(results)} conversation(s) matching {query!r}:\n\n"
    return head + "\n\n".join(_format_hit(s) for s in results)


@mcp.tool()
def get_session(session_id: str) -> str:
    """Retrieve a full past conversation as Markdown, given its id.

    Use the id returned by `search_history` or `list_recent`. Returns the whole
    conversation (every turn, with tool calls noted) so you can reuse a prior
    solution, command, or explanation.
    """
    session = search.get_session(session_id)
    if not session:
        return f"No conversation found with id: {session_id}"
    return exporting.session_to_markdown(session)


@mcp.tool()
def list_recent(
    limit: int = 10, source: str | None = None, repo: str | None = None
) -> str:
    """List your most recent conversations, optionally filtered by source/repo.

    Returns id, title, source, repository, and date for each. Pass an id to
    `get_session` to read the full conversation.
    """
    limit = max(1, min(int(limit), 50))
    results = search.browse(source=source, repo=repo, sort="recent", limit=limit)
    if not results:
        return "No conversations indexed yet."
    return "\n".join(_format_hit(s) for s in results)


def main() -> None:
    """Console entry point: initialise the DB, then serve over stdio."""
    db.init_db()
    mcp.run()


if __name__ == "__main__":
    main()
