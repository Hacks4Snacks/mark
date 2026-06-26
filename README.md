<p align="center"><img src="mark/web/favicon.svg" width="72" alt="Mark logo" /></p>

# Mark — Multi-session AI Recall Keeper

> Your searchable, private archive of every AI coding chat.
>
> Published on PyPI as **`markive`** (the command is **`mark`**).

Mark turns the AI coding history already stored on your machine into a
**beautiful, searchable knowledge base**, so you never lose a useful
conversation again. It indexes several sources automatically:

- **VS Code chat** — the inline/agent chats under `workspaceStorage`.
- **Copilot CLI / agent store** — the richer agent conversations in
  `~/.copilot/session-store.db`.
- **Coding-agent extensions** — Cline, Zoo Code, Roo, Kilo, and other
  Cline-family task histories under `globalStorage` (auto-detected).

You can also drop in your own notes and files. Everything runs **100% locally**
— your conversations never leave your machine.

## Why it's better than Ctrl-F

- **Semantic search** — find conversations *by meaning*, not just exact words
  ("how I fixed the auth timeout" finds the session even if you wrote "token
  expiry bug").
- **Hybrid ranking** — keyword (FTS5/BM25) + vector similarity, fused together.
- **Auto topics & summaries** — every session gets a short summary and topic
  tags, generated locally (no LLM, no API keys).
- **Faceted browse** — filter by repository, topic, source, and recency.
- **Files, code & tools** — see which files a session touched, the code blocks,
  and which tools ran.

## Quick start

Pick whichever fits you — all three run **100% locally**:

```bash
# 1) Run without installing (needs uv: https://docs.astral.sh/uv/)
uvx --from . 'markive[semantic]'

# 2) Install as a command (pipx keeps it isolated)
pipx install '.[semantic]'      # then just run:  mark

# 3) One-shot dev launcher (creates a venv for you)
./run.sh
```

> Once published, you can also `pipx install 'markive[semantic]'` from PyPI.

Then open <http://127.0.0.1:8765>. The first launch indexes your history in the
background (watch the banner). Click **⟳** any time to pick up new sessions.
Data lives in `~/.mark/` (override with `MARK_DATA_DIR`).

> Plain pip: `pip install '.[semantic]'` then `mark` (or `python -m mark`). The
> `markive`/`markive-mcp` commands are aliases in case `mark` clashes on PATH.
> Without the `[semantic]` extra it still works, using a built-in vectorizer.

## Run in Docker

Your conversations stay on your machine — the container mounts them
**read-only** and only writes the derived index to a named volume.

```bash
docker compose up --build -d      # → http://127.0.0.1:8765
```

The compose file is preset for macOS + VS Code (stable). For Insiders, Linux, or
Windows, edit the three host paths under `volumes:` (see the storage-path notes
below). The server binds to `127.0.0.1` on the host only.

## Search modes

| Mode | What it does |
| --- | --- |
| **Hybrid** (default) | Best of both — keyword precision + semantic recall |
| **Semantic** | Pure "find by meaning" via embeddings |
| **Keyword** | Classic exact-term FTS5 search |

## Semantic engine

Mark tries, in order: [`fastembed`](https://github.com/qdrant/fastembed)
(ONNX transformer) → [`model2vec`](https://github.com/MinishLab/model2vec)
(static embeddings) → a built-in NumPy vectorizer that always works offline.
Install the optional upgrades for best quality:

```bash
pip install -r requirements-optional.txt
```

The status card in the sidebar shows which engine is active.

## Configuration

All optional, via environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARK_PORT` | `8765` | Server port |
| `MARK_HOST` | `127.0.0.1` | Bind address (localhost only) |
| `MARK_DATA_DIR` | `./data` | Where the SQLite DB and uploads live |
| `MARK_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model |
| `MARK_VSCODE_STORAGE` | auto-detected | Override VS Code `workspaceStorage` path(s) |
| `MARK_VSCODE_GLOBAL_STORAGE` | auto-detected | Override `globalStorage` path(s) (Cline, Zoo Code, …) |
| `MARK_COPILOT_STORE` | `~/.copilot/session-store.db` | Copilot CLI / agent session store |
| `MARK_SESSION_STATE` | `~/.copilot/session-state` | Per-session event logs (tokens, model, duration) |
| `MARK_MAX_EMBED_CHUNKS_PER_SESSION` | `40` | Cap on *embedded* chunks per session (keyword/FTS indexes all chunks; bounds the in-memory semantic vector set) |
| `MARK_PRICING_FILE` | built-in table | JSON of `{model: [in, out, cached]}` USD per 1M tokens |
| `MARK_RESUME_CMD` | `copilot --resume {id}` | Resume command shown in the UI |

## Usage, duration & cost

Every Copilot CLI session is enriched from its `events.jsonl` with **real**
metrics — model, wall-clock duration, input/output/cache token counts, premium
requests, and AIU. Mark turns those token counts into an **estimated USD cost**
using a public-price table (editable via `MARK_PRICING_FILE`); cache reads are
priced separately so long agent sessions aren't over-counted. VS Code sessions
(which don't log tokens) fall back to a text-based estimate, flagged as such.
Each session detail also shows its **session id** and a copyable
`copilot --resume <id>` command.

## Use it from your agent (MCP server)

Mark can expose your archive to any MCP-aware agent — Copilot CLI, Cline,
Claude Desktop — so an agent can **recall how you solved something before**.
Install the extra (adds the `mark-mcp` command) and register the stdio server:

```bash
pip install '.[mcp]'
```

```jsonc
// e.g. Claude Desktop / Copilot CLI MCP config
{
  "mcpServers": {
    "mark": { "command": "mark-mcp" }
  }
}
```

Tools exposed:

- **`search_history`** — find past conversations by meaning or keyword
  (filters: `mode`, `source`, `repo`).
- **`get_session`** — fetch a whole conversation as Markdown by id.
- **`list_recent`** — list your most recent conversations.

Everything runs locally over stdio — no network, no API keys.

## Ask your history (optional, local LLM)

If a local [Ollama](https://ollama.com) server is running, the **✦ Ask** view
lets you ask questions in natural language. Mark retrieves the most relevant
past conversations, has a **local** model synthesise a cited answer, and streams
it back token-by-token — so your archive stays on your machine, no API keys.

```bash
ollama pull llama3.2     # any installed model works; mark auto-picks one
ollama serve
```

Override the model with `MARK_OLLAMA_MODEL` or the endpoint with
`MARK_OLLAMA_URL`. When Ollama isn't reachable, the view simply shows setup
hints — every other feature works without it.

## Collections

**Collections** group related conversations so a long-running effort — *"the
auth refactor"*, *"learning Rust"*, *"everything about repo X"* — reads as one
place instead of scattered sessions. They're **auto-updating**: a collection
follows a saved search and its filters (query, repo, topic, source, date), so
newly indexed sessions flow in on their own. You stay in control — pin a session
that the rule missed, or remove one it shouldn't include, and that choice
**sticks across re-syncs**.

- **Save as collection** — run any search or pick filters, then click *▦ Save as
  collection* to turn the current view into an auto-updating group.
- **＋ Collection** — on any conversation, add it to (or remove it from) a
  collection by hand.
- **Overview** — each collection rolls up its sessions: total spend, time, files
  touched, topics, and date span.
- **Ask this collection** — the optional local *Ask* (see below) can be scoped to
  just one collection, so answers are drawn only from those conversations.

Collections live in the same local SQLite database — nothing leaves your machine.

## How it works

```
VS Code chatSessions/*.json  ─┐
                              ├─ingest─▶  SQLite (sessions, turns, files, tags, cost)
~/.copilot/session-store.db  ─┘   (+ events.jsonl metrics)
                                              │
                       ┌──────────────────────┼───────────────────────┐
                   FTS5 index            vector embeddings        local enrichment
                   (keyword)              (semantic)              (summaries, tags)
                       └──────────────── hybrid search (RRF) ─────────────┘
                                              │
                                    FastAPI + static UI
```
