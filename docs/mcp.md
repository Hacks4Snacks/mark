# MCP server

Mark can expose your archive to any **MCP-aware agent** — Copilot CLI, Cline,
Claude Desktop, and others — so an agent can **recall how you solved something
before** instead of starting from scratch. It speaks the
[Model Context Protocol](https://modelcontextprotocol.io) over stdio, entirely
locally: no network, no API keys.

## Install

The MCP server ships as an optional extra that adds the `mark-mcp` command:

```bash
pip install 'markive[mcp]'
```

(`markive-mcp` is an alias in case `mark-mcp` collides on your `PATH`.)

## Register the server

Add Mark as a stdio MCP server in your agent's config. For Claude Desktop or
Copilot CLI:

```jsonc
{
  "mcpServers": {
    "mark": { "command": "mark-mcp" }
  }
}
```

That's it — the agent launches `mark-mcp`, which reads the same local
`~/.mark/mark.db` your UI uses. (Set `MARK_DATA_DIR` in the server's environment
if your data lives elsewhere.)

## Tools exposed

| Tool | Purpose | Key arguments |
| --- | --- | --- |
| `search_history` | Find past conversations by meaning or keyword | `query`, `mode` (`hybrid`/`semantic`/`keyword`), `limit` (1–25), `source`, `repo` |
| `get_session` | Fetch a whole conversation as Markdown by id | `session_id` |
| `list_recent` | List your most recent conversations | `limit` (1–50), `source`, `repo` |

### `search_history`

Returns matching conversations with their id, title, source, repository, date,
relevance score, and best-matching snippet. The agent passes a returned id to
`get_session` to read the full conversation. Filters mirror the UI: restrict by
`source` (`vscode`, `cli`, `cline`, `zoocode`, `cursor`, `chatgpt`, …) or `repo`.

### `get_session`

Returns the entire conversation rendered as Markdown — every turn, with tool calls
noted — so the agent can reuse a prior solution, command, or explanation verbatim.

### `list_recent`

A lightweight catch-up: id, title, source, repository, and date for your latest
sessions, optionally filtered by source/repo.

## Typical agent flow

1. The agent calls `search_history` with a natural-language description of the
   problem.
2. It picks the most relevant hit and calls `get_session` to read the full
   transcript.
3. It applies what you did last time — same fix, same command — without you
   re-explaining.

## Privacy

Everything runs locally over stdio. The agent only sees what it explicitly
queries, and nothing ever leaves your machine.

## Quick smoke test

The repo ships a tiny smoke-test script you can adapt to confirm the server
starts and answers:

```bash
python scripts/mcp_smoke.py
```
