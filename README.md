<p align="center"><img src="mark/web/favicon.svg" width="72" alt="Mark logo" /></p>

# Mark: Multi-session AI Recall Keeper

> Your searchable, private archive of every AI coding chat.
>
> Run it with the **`mark`** command (the package is named **`markive`**).

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue.svg" />
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg" /></a>
  <img alt="Runs locally" src="https://img.shields.io/badge/data-100%25%20local-success.svg" />
</p>

Mark turns the AI coding history already stored on your machine into a
**beautiful, searchable knowledge base**, so you never lose a useful
conversation again. It indexes several sources automatically:

- **VS Code chat:** the inline/agent chats under `workspaceStorage`.
- **Copilot CLI / agent store:** the richer agent conversations in
  `~/.copilot/session-store.db`.
- **Coding-agent extensions:** Cline, Zoo Code, Roo, Kilo, and other
  Cline-family task histories under `globalStorage` (auto-detected).

You can also drop in your own notes and files. Everything runs **100% locally**,
so your conversations never leave your machine.

## Contents

- [Why it's better than Ctrl-F](#why-its-better-than-ctrl-f)
- [Quick start](#quick-start)
- [Requirements](#requirements)
- [Documentation](#documentation)
- [Run in Docker](#run-in-docker)
- [Search modes](#search-modes)
- [Semantic engine](#semantic-engine)
- [Configuration](#configuration)
- [Usage, duration & cost](#usage-duration--cost)
- [Use it from your agent (MCP server)](#use-it-from-your-agent-mcp-server)
- [Ask your history](#ask-your-history-optional-local-llm)
- [Collections](#collections)
- [Snippet & command library](#snippet--command-library)
- [Manage your archive](#manage-your-archive)
- [Privacy](#privacy)
- [Contributing](#contributing)
- [License](#license)

## Why it's better than Ctrl-F

- **Semantic search:** find conversations *by meaning*, not just exact words
  ("how I fixed the auth timeout" finds the session even if you wrote "token
  expiry bug").
- **Hybrid ranking:** keyword (FTS5/BM25) + vector similarity, fused together.
- **Auto topics & summaries:** every session gets a short summary and topic
  tags, generated locally (no LLM, no API keys).
- **Faceted browse:** filter by repository, topic, source, and recency.
- **Files, code & tools:** see which files a session touched, the code blocks,
  and which tools ran.

## Quick start

Pick whichever fits you. All three run **100% locally**:

```bash
# 1) Run without installing (needs uv: https://docs.astral.sh/uv/)
uvx --from . 'markive[semantic]'

# 2) Install as a command (pipx keeps it isolated)
pipx install '.[semantic]'      # then just run:  mark

# 3) One-shot dev launcher (creates a venv for you)
./run.sh
```

Then open <http://127.0.0.1:8765>. The first launch indexes your history in the
background (watch the banner). Click **⟳** any time to pick up new sessions.
Data lives in `~/.mark/` (override with `MARK_DATA_DIR`).

> Plain pip: `pip install '.[semantic]'` then `mark` (or `python -m mark`). The
> `markive`/`markive-mcp` commands are aliases in case `mark` clashes on PATH.
> Without the `[semantic]` extra it still works, using a built-in vectorizer.

## Requirements

- **Python 3.10+**
- One or more supported chat sources on your machine (VS Code chat, Copilot CLI,
  or a Cline-family extension); Mark auto-discovers them
- Optional: the `[semantic]` extra for transformer-quality search, the `[mcp]`
  extra for the agent server, and a local [Ollama](https://ollama.com) server for
  natural-language *Ask*

## Documentation

Full guides live in [`docs/`](docs/README.md):

| Guide                                                  | Covers                                                |
|--------------------------------------------------------|-------------------------------------------------------|
| [Getting started](docs/getting-started.md)             | Install, first launch, the UI, where data lives       |
| [Searching & filtering](docs/searching.md)             | Search modes, facets, sorting, related sessions       |
| [Collections](docs/collections.md)                     | Auto-updating groups, pin/exclude, ask-a-collection   |
| [Usage & cost](docs/usage-and-cost.md)                 | Dashboards, real vs estimated metrics, custom pricing |
| [Ask your history](docs/ask.md)                        | Local RAG with Ollama                                 |
| [Snippet & command library](docs/library.md)           | Browse code and commands across sessions              |
| [Sources & syncing](docs/sources.md)                   | Supported sources, overrides, sync behaviour          |
| [Managing your archive](docs/managing-your-archive.md) | Notes, uploads, tags, hide, delete, export            |
| [MCP server](docs/mcp.md)                              | Expose your archive to agents                         |
| [Running in Docker](docs/docker.md)                    | Containerised setup                                   |
| [Configuration reference](docs/configuration.md)       | Every `MARK_*` variable                               |
| [FAQ & troubleshooting](docs/faq.md)                   | Privacy, performance, common fixes                    |

## Run in Docker

Your conversations stay on your machine: the container mounts them
**read-only** and only writes the derived index to a named volume.

```bash
docker compose up --build -d      # http://127.0.0.1:8765
```

The mounts land on the paths mark auto-detects inside the container, so **no
`MARK_*` path variables are needed**, giving the same "no config = discover
everything" behavior as a local install. The compose file is preset for macOS +
VS Code (stable); for Insiders, Linux, or Windows edit only the **host** (left)
side of each mount under `volumes:`. The server binds to `127.0.0.1` on the host
only.

Need finer control (disable a source, add a Cline-family label, or point at
extra roots)? Bind-mount a `sources.toml` (same format as a local
`~/.mark/sources.toml`; see [`sources.example.toml`](sources.example.toml)). The
compose file ships a commented mount line for it; use the **in-container** mount
targets for any `roots` you set.

## Search modes

| Mode                 | What it does                                      |
|----------------------|---------------------------------------------------|
| **Hybrid** (default) | Best of both: keyword precision + semantic recall |
| **Semantic**         | Pure "find by meaning" via embeddings             |
| **Keyword**          | Classic exact-term FTS5 search                    |

## Semantic engine

Mark tries, in order: [`fastembed`](https://github.com/qdrant/fastembed)
(ONNX transformer), then [`model2vec`](https://github.com/MinishLab/model2vec)
(static embeddings), then a built-in NumPy vectorizer that always works offline.
Install the optional upgrades for best quality:

```bash
pip install -r requirements-optional.txt
```

The status card in the sidebar shows which engine is active.

## Configuration

All optional, via environment variables:

| Variable                            | Default                  | Purpose                                                                                                         |
|-------------------------------------|--------------------------|-----------------------------------------------------------------------------------------------------------------|
| `MARK_PORT`                         | `8765`                   | Server port                                                                                                     |
| `MARK_HOST`                         | `127.0.0.1`              | Bind address (localhost only)                                                                                   |
| `MARK_DATA_DIR`                     | `~/.mark`                | Where the SQLite DB and uploads live                                                                            |
| `MARK_EMBED_MODEL`                  | `BAAI/bge-small-en-v1.5` | fastembed model                                                                                                 |
| `MARK_MAX_EMBED_CHUNKS_PER_SESSION` | `40`                     | Cap on *embedded* chunks per session (keyword/FTS indexes all chunks; bounds the in-memory semantic vector set) |
| `MARK_PRICING_FILE`                 | built-in table           | JSON of `{model: [in, out, cached]}` USD per 1M tokens                                                          |
| `MARK_RESUME_CMD`                   | `copilot --resume {id}`  | Resume command shown in the UI                                                                                  |

**Sources** (which chat stores to scan) are configured separately. By default
mark auto-discovers them; to override paths, enable/disable a source, or add
label overrides, use `~/.mark/sources.toml` (see
[`sources.example.toml`](sources.example.toml)) or the
`MARK_SOURCE_<NAME>_ENABLED` / `MARK_SOURCE_<NAME>_ROOTS` environment variables.

See the [full configuration reference](docs/configuration.md) for every variable.

## Usage, duration & cost

Every Copilot CLI session is enriched from its `events.jsonl` with **real**
metrics: model, wall-clock duration, input/output/cache token counts, premium
requests, and AIU. Mark turns those token counts into an **estimated USD cost**
using a public-price table (editable via `MARK_PRICING_FILE`); cache reads are
priced separately so long agent sessions aren't over-counted. VS Code sessions
(which don't log tokens) fall back to a text-based estimate, flagged as such.
Each session detail also shows its **session id** and a copyable
`copilot --resume <id>` command.

## Use it from your agent (MCP server)

Mark can expose your archive to any MCP-aware agent (Copilot CLI, Cline,
Claude Desktop) so an agent can **recall how you solved something before**.
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

- **`search_history`:** find past conversations by meaning or keyword
  (filters: `mode`, `source`, `repo`).
- **`get_session`:** fetch a whole conversation as Markdown by id.
- **`list_recent`:** list your most recent conversations.

Everything runs locally over stdio: no network, no API keys.

## Ask your history (optional, local LLM)

If a local [Ollama](https://ollama.com) server is running, the **✦ Ask** view
lets you ask questions in natural language. Mark retrieves the most relevant
past conversations, has a **local** model synthesise a cited answer, and streams
it back token-by-token, so your archive stays on your machine, no API keys.

```bash
ollama pull llama3.2     # any installed model works; mark auto-picks one
ollama serve
```

Override the model with `MARK_OLLAMA_MODEL` or the endpoint with
`MARK_OLLAMA_URL`. When Ollama isn't reachable, the view simply shows setup
hints; every other feature works without it.

## Collections

**Collections** group related conversations so a long-running effort (*"the
auth refactor"*, *"learning Rust"*, *"everything about repo X"*) reads as one
place instead of scattered sessions. They're **auto-updating**: a collection
follows a saved search and its filters (query, repo, topic, source, date), so
newly indexed sessions flow in on their own. You stay in control: pin a session
that the rule missed, or remove one it shouldn't include, and that choice
**sticks across re-syncs**.

- **Save as collection:** run any search or pick filters, then click *▦ Save as
  collection* to turn the current view into an auto-updating group.
- **＋ Collection:** on any conversation, add it to (or remove it from) a
  collection by hand.
- **Overview:** each collection rolls up its sessions: total spend, time, files
  touched, topics, and date span.
- **Ask this collection:** the optional local *Ask* (see below) can be scoped to
  just one collection, so answers are drawn only from those conversations.

Collections live in the same local SQLite database; nothing leaves your machine.

## Snippet & command library

The **Library** view pulls every code block and shell command out of your
history into one browsable list, so you can find *that one command* or reusable
snippet without remembering which session it lived in. Filter by language or
free text, or flip on **commands only** to see just the shell commands you've
run. See [Snippet & command library](docs/library.md).

## Manage your archive

Your archive is yours to curate. Add your own **notes** and **upload files** to
keep alongside indexed sessions, attach **tags**, **hide** noise from the default
view, or **delete** sessions for good. Agent-created files are snapshotted so you
can view and download them later, and any conversation can be **exported to
Markdown**. See [Managing your archive](docs/managing-your-archive.md).

## Privacy

Mark is built to keep your history yours:

- **100% local:** no telemetry, no accounts, no API keys. Search, embeddings,
  summaries, and topic tags are all generated on-device.
- **Read-only sources:** your original chat stores are never modified; Mark only
  writes its own index under `~/.mark/`.
- **Localhost only:** the server binds to `127.0.0.1`. The sole optional network
  calls are to a local Ollama server *you* run for *Ask*.

To wipe everything, stop Mark and delete the data directory (`rm -rf ~/.mark`);
your source conversations are untouched.

## Contributing

```bash
git clone https://github.com/Hacks4Snacks/markive
cd markive
./run.sh                         # creates a venv and launches

# lint & test
python -m ruff check mark tests
python -m pytest -q
```

Issues and pull requests are welcome at
<https://github.com/graymark/markive>.

## License

[MIT](LICENSE) © 2026 Mark Dalton Gray.
