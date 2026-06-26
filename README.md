<p align="center"><img src="mindex/web/favicon.svg" width="72" alt="mindex logo" /></p>

# mindex — your searchable AI chat archive

mindex turns the AI coding history already stored on your machine into a
**beautiful, searchable knowledge base**, so you never lose a useful
conversation again. It indexes several sources automatically:

- **VS Code chat** — the inline/agent chats under `workspaceStorage`.
- **Copilot CLI / agent store** — the richer agent conversations in
  `~/.copilot/session-store.db`.
- **Coding-agent extensions** — Cline, Zoo Code, Roo, Kilo, and other
  Cline-family task histories under `globalStorage` (auto-detected).

You can also drop in your own notes and files. Everything runs **100% locally**
— your conversations never leave your machine.

Background automation runs (e.g. "Paperclip Wake Payload" heartbeats) are
detected and tagged as the `automation` source, **hidden by default** behind a
sidebar toggle so they don't bury real conversations.

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
uvx --from . 'mindex[semantic]'

# 2) Install as a command (pipx keeps it isolated)
pipx install '.[semantic]'      # then just run:  mindex

# 3) One-shot dev launcher (creates a venv for you)
./run.sh
```

Then open <http://127.0.0.1:8765>. The first launch indexes your history in the
background (watch the banner). Click **⟳** any time to pick up new sessions.
Data lives in `~/.mindex/` (override with `MINDEX_DATA_DIR`).

> Plain pip: `pip install '.[semantic]'` then `mindex` (or `python -m mindex`).
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

mindex tries, in order: [`fastembed`](https://github.com/qdrant/fastembed)
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
| `MINDEX_PORT` | `8765` | Server port |
| `MINDEX_HOST` | `127.0.0.1` | Bind address (localhost only) |
| `MINDEX_DATA_DIR` | `./data` | Where the SQLite DB and uploads live |
| `MINDEX_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model |
| `MINDEX_VSCODE_STORAGE` | auto-detected | Override VS Code `workspaceStorage` path(s) |
| `MINDEX_VSCODE_GLOBAL_STORAGE` | auto-detected | Override `globalStorage` path(s) (Cline, Zoo Code, …) |
| `MINDEX_COPILOT_STORE` | `~/.copilot/session-store.db` | Copilot CLI / agent session store |
| `MINDEX_SESSION_STATE` | `~/.copilot/session-state` | Per-session event logs (tokens, model, duration) |
| `MINDEX_EMBED_AUTOMATION` | `0` | Set `1` to also embed automation runs for semantic search |
| `MINDEX_MAX_CHUNKS_PER_SESSION` | `40` | Cap on indexed chunks per session (bounds huge agent tasks) |
| `MINDEX_PRICING_FILE` | built-in table | JSON of `{model: [in, out, cached]}` USD per 1M tokens |
| `MINDEX_RESUME_CMD` | `copilot --resume {id}` | Resume command shown in the UI |

## Usage, duration & cost

Every Copilot CLI session is enriched from its `events.jsonl` with **real**
metrics — model, wall-clock duration, input/output/cache token counts, premium
requests, and AIU. mindex turns those token counts into an **estimated USD cost**
using a public-price table (editable via `MINDEX_PRICING_FILE`); cache reads are
priced separately so long agent sessions aren't over-counted. VS Code sessions
(which don't log tokens) fall back to a text-based estimate, flagged as such.
Each session detail also shows its **session id** and a copyable
`copilot --resume <id>` command.

## Automation runs

**Everything is imported** — nothing is thrown away. Background automation runs
(Paperclip wake payloads, session-insight tasks, `Continue.`/`OK` system turns)
are classified as the `automation` source and **hidden by default** behind a
sidebar toggle, so they don't bury real conversations. Flip *Include automation
runs* (or click the Automation source chip) to browse them.

## How it works

```
VS Code chatSessions/*.json  ─┐
                              ├─ingest─▶  SQLite (sessions, turns, files, tags, cost)
~/.copilot/session-store.db  ─┘   (+ events.jsonl metrics; automation tagged + hidden)
                                              │
                       ┌──────────────────────┼───────────────────────┐
                   FTS5 index            vector embeddings        local enrichment
                   (keyword)              (semantic)              (summaries, tags)
                       └──────────────── hybrid search (RRF) ─────────────┘
                                              │
                                    FastAPI + static UI
```
