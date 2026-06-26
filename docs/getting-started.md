# Getting started

Mark turns the AI coding history already on your machine into a searchable
knowledge base. This guide gets you from zero to a running archive.

## Requirements

- **Python 3.10 or newer.**
- A modern browser (the UI is served locally).
- *Optional:* [`uv`](https://docs.astral.sh/uv/) or
  [`pipx`](https://pipx.pypa.io/) for isolated installs.
- *Optional:* [Ollama](https://ollama.com) if you want the natural-language
  **Ask** feature.

Everything runs locally. No account, API key, or network connection is required.

## Install & run

Pick whichever fits your workflow — all three run **100% locally**.

```bash
# 1) Run without installing (needs uv)
uvx 'markive[semantic]'

# 2) Install as an isolated command (recommended)
pipx install 'markive[semantic]'   # then just run:  mark

# 3) Plain pip
pip install 'markive[semantic]'    # then:  mark   (or  python -m mark)
```

The `[semantic]` extra adds transformer-quality "find by meaning" search. It is
optional — without it Mark still works using a built-in offline vectorizer. See
[Searching](searching.md#semantic-engine) for the difference.

> **Command names.** The primary command is `mark`. `markive` and `markive-mcp`
> are aliases that match the distribution name, in case `mark` collides with
> something already on your `PATH`.

### Running from a checkout

```bash
./run.sh           # creates a throwaway venv and launches
```

### Running in Docker

Prefer a container? Mark ships a Docker Compose setup that mounts your chat
stores **read-only** and keeps the index in a named volume:

```bash
docker compose up --build -d      # http://127.0.0.1:8765
```

No `MARK_*` path variables are needed — the mounts land on the paths Mark
auto-detects inside the container. See [Running in Docker](docker.md) for
per-OS customisation and tuning.

## First launch

1. Start Mark, then open <http://127.0.0.1:8765>.
2. The first launch **indexes your history in the background** — watch the banner
   at the top. Large histories take a little while to embed; search works on what
   has been indexed so far and fills in as it goes.
3. Mark keeps itself current: it **auto-syncs** new and changed sessions while
   running. Click the **⟳** (re-scan) button any time to pick up changes
   immediately.

## Where your data lives

| Path | Contents |
| --- | --- |
| `~/.mark/mark.db` | The SQLite index (sessions, turns, files, tags, cost, embeddings) |
| `~/.mark/uploads/` | Files you add through the UI |
| `~/.mark/sources.toml` | *Optional* source overrides (see [Sources](sources.md)) |

Override the base directory with `MARK_DATA_DIR`. Your original chat stores are
**never modified** — Mark only reads them, and for live databases it reads a
consistent read-only snapshot.

## The UI at a glance

- **Search bar** (top) with a **Hybrid / Semantic / Keyword** mode toggle.
- **Sidebar** with stat cards and facets: source, sort, date range, repositories,
  topics, plus a "show hidden only" toggle.
- **Top-bar actions:**
  - **Collections** — auto-updating groups of conversations.
  - **Library** — every extracted code block and shell command.
  - **Usage** — spend, duration and token analytics.
  - **Ask** — natural-language Q&A (needs a local LLM).
  - **Add** — drop in a note or file.
  - **⟳** — re-scan now.
  - **theme** — toggle light/dark.

Each result opens a **detail view** showing the full conversation, the files it
touched, code blocks, tools that ran, a session id, and a copyable resume
command. Deep links work: `#/session/<id>`, `#/collection/<id>`, `#/library`,
`#/usage`, `#/ask`, `#/collections`.

## Next steps

- [Configure which sources are indexed](sources.md)
- [Learn the search modes and filters](searching.md)
- [Group long-running efforts into collections](collections.md)
- [Expose your archive to an AI agent](mcp.md)
