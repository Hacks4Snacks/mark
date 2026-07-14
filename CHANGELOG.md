# Changelog

All notable changes to **Mark** (distributed as [`markive`](https://pypi.org/p/markive)) are
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
From v0.2.0 onward, entries are generated automatically by
[release-please](https://github.com/googleapis/release-please) from Conventional
Commits.

## [0.4.0](https://github.com/Hacks4Snacks/mark/compare/v0.3.0...v0.4.0) (2026-07-14)


### Features

* Harden Mark across ingestion, storage, search, large-archive performance, and packaging ([#18](https://github.com/Hacks4Snacks/mark/issues/18)) ([c8b1a0e](https://github.com/Hacks4Snacks/mark/commit/c8b1a0ef1958d37a404d5cfb4337aefc39f5445e))

## [0.3.0](https://github.com/Hacks4Snacks/mark/compare/v0.2.0...v0.3.0) (2026-07-02)


### Features

* grok source adapter support ([#15](https://github.com/Hacks4Snacks/mark/issues/15)) ([2c6d835](https://github.com/Hacks4Snacks/mark/commit/2c6d835609caa46a2ea8b1ecf4e354e4457f4565))

## [0.2.0](https://github.com/Hacks4Snacks/mark/compare/v0.1.0...v0.2.0) (2026-06-29)


### Features

* feature flag ask ([#14](https://github.com/Hacks4Snacks/mark/issues/14)) ([92e9c18](https://github.com/Hacks4Snacks/mark/commit/92e9c183cea2d8fcba059e1f25b68a0672003350))
* Improve Ask Context ([#12](https://github.com/Hacks4Snacks/mark/issues/12)) ([fc72e14](https://github.com/Hacks4Snacks/mark/commit/fc72e143c5a050af52f9189ed35ec33a55d2b465))
* Support Claude Code ([#7](https://github.com/Hacks4Snacks/mark/issues/7)) ([5b1f51a](https://github.com/Hacks4Snacks/mark/commit/5b1f51ad1c468db13129b941b3b3ca95c0c0781b))


### Bug Fixes

* Address additional UI scrolling bug and icon update ([#11](https://github.com/Hacks4Snacks/mark/issues/11)) ([31289a2](https://github.com/Hacks4Snacks/mark/commit/31289a2ff97cc1f383e55175a6b0c5b266952c6b))
* Reduce truncation ([#13](https://github.com/Hacks4Snacks/mark/issues/13)) ([01b6f9b](https://github.com/Hacks4Snacks/mark/commit/01b6f9b0ad55ccccd80200cdcbbc733cb9cecea6))

## [0.1.0](https://github.com/Hacks4Snacks/mark/releases/tag/v0.1.0) (2026-06-26)

**First public release.** 🎉

Mark turns the AI coding history already sitting on your machine into a
beautiful, searchable knowledge base, so you never lose a useful conversation
again. It auto-discovers your existing chat stores, indexes them locally, and
gives you semantic search, analytics, and recall across every session — with
**zero data leaving your machine**.

### Added

#### Sources & ingestion

- **Auto-discovered sources:** VS Code chat (`workspaceStorage`), Copilot CLI /
  agent store (`~/.copilot/session-store.db`), and Cline-family coding-agent
  extensions (Cline, Roo, Kilo, Zoo Code, and relatives under `globalStorage`).
- **Read-only ingestion:** original chat stores are never modified; live
  databases are read from a consistent snapshot.
- **Background indexing & auto-sync:** the first launch indexes in the
  background and search fills in as it goes; new and changed sessions are picked
  up automatically, or on demand with the **⟳** re-scan button.
- **Source overrides** via `~/.mark/sources.toml` or
  `MARK_SOURCE_<NAME>_ENABLED` / `MARK_SOURCE_<NAME>_ROOTS` environment
  variables (see [`sources.example.toml`](sources.example.toml)).

#### Search

- **Three search modes:** Hybrid (default), Semantic, and Keyword.
- **Hybrid ranking** that fuses keyword precision (SQLite FTS5 / BM25) with
  vector similarity.
- **Pluggable semantic engine** that degrades gracefully:
  [`fastembed`](https://github.com/qdrant/fastembed) (ONNX transformer, via the
  `semantic` extra) → [`model2vec`](https://github.com/MinishLab/model2vec)
  (static embeddings) → a built-in NumPy vectorizer that always works offline.
  The active engine is shown in the sidebar.
- **Faceted browse & filtering** by repository, topic, source, recency, and sort
  order, plus a "show hidden only" toggle.
- **Auto summaries & topic tags** generated locally for every session — no LLM,
  no API keys.

#### Insight & analytics

- **Usage, duration & cost dashboards.** Copilot CLI sessions are enriched from
  `events.jsonl` with *real* metrics (model, wall-clock duration,
  input/output/cache tokens, premium requests, AIU). Token counts are turned
  into an estimated USD cost using a public-price table; cache reads are priced
  separately so long agent sessions aren't over-counted.
- **Configurable pricing** via `MARK_PRICING_FILE`. VS Code sessions (no token
  logs) fall back to a text-based estimate, clearly flagged as estimated.
- **Conversation detail view** surfacing the full transcript, the files a session
  touched, extracted code blocks, the tools that ran, the session id, and a
  copyable resume command (`MARK_RESUME_CMD`, default `copilot --resume {id}`).

#### Organize & recall

- **Collections:** auto-updating groups that follow a saved search and its
  filters (query, repo, topic, source, date). Pin a session the rule missed or
  exclude one it shouldn't include — your choices stick across re-syncs. Each
  collection rolls up total spend, time, files, topics, and date span.
- **Snippet & command library:** every code block and shell command pulled out of
  your history into one browsable list, filterable by language, free text, or
  "commands only".
- **Manage your archive:** add notes and upload files (PDF text extraction via the
  `pdf` extra), attach tags, hide noise, or delete sessions for good.
  Agent-created files are snapshotted for later viewing and download, and any
  conversation can be exported to Markdown.

#### Ask your history (optional, local LLM)

- **✦ Ask** view: natural-language Q&A over your archive using a **local**
  [Ollama](https://ollama.com) model. Mark retrieves the most relevant sessions,
  synthesises a cited answer, and streams it back token-by-token. Ask can be
  scoped to a single collection. Configurable via `MARK_OLLAMA_MODEL` /
  `MARK_OLLAMA_URL`; every other feature works without it.

#### Agent integration (MCP)

- **MCP server** (`mark-mcp`, via the `mcp` extra) exposing your archive to any
  MCP-aware agent (Copilot CLI, Cline, Claude Desktop) over local stdio — no
  network, no API keys. Tools: `search_history`, `get_session`, `list_recent`.

#### Web UI

- Local single-page UI served at <http://127.0.0.1:8765> with a search bar +
  mode toggle, sidebar stat cards and facets, light/dark theme, and top-bar
  actions for Collections, Library, Usage, Ask, and Add.
- **Deep links:** `#/session/<id>`, `#/collection/<id>`, `#/collections`,
  `#/library`, `#/usage`, `#/ask`.

#### Privacy

- **100% local:** no telemetry, no accounts, no API keys. Search, embeddings,
  summaries, and topic tags are all generated on-device.
- **Localhost only:** the server binds to `127.0.0.1`. The sole optional network
  calls are to a local Ollama server you run for *Ask*.
- Wipe everything by deleting the data directory (`rm -rf ~/.mark`); source
  conversations are untouched.

#### Install & run

- **Multiple install paths**, all fully local: `uvx 'markive[semantic]'`,
  `pipx install 'markive[semantic]'`, plain `pip`, the `./run.sh` dev launcher,
  and a `docker compose` setup that mounts chat stores read-only and keeps the
  index in a named volume.
- **Packaging:** published to PyPI as `markive`; commands `mark` and `mark-mcp`
  (with `markive` / `markive-mcp` aliases). Optional extras: `semantic`, `pdf`,
  `mcp`, `all`, `dev`. Requires **Python 3.10+**.
- **Configuration** via `MARK_*` environment variables (`MARK_PORT`,
  `MARK_HOST`, `MARK_DATA_DIR`, `MARK_EMBED_MODEL`,
  `MARK_MAX_EMBED_CHUNKS_PER_SESSION`, and more — see
  [`docs/configuration.md`](docs/configuration.md)).
- **Documentation:** full guides under [`docs/`](docs/README.md) and a
  `seed_demo_data.py` script for a throwaway demo archive.
