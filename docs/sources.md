# Sources & syncing

A **source** is a place Mark reads AI chat history from. With no configuration at
all, Mark **auto-discovers** every supported source and keeps them in sync. This
page explains what's supported, how to override paths, and how syncing works.

## Supported sources

| Source key       | What it indexes                                              | Where it lives (auto-detected)                               |
|------------------|--------------------------------------------------------------|--------------------------------------------------------------|
| `vscode`         | VS Code inline/agent chats                                   | `…/Code/User/workspaceStorage` (Stable, Insiders, VSCodium)  |
| `copilot_cli`    | Copilot CLI / agent-store conversations + real token metrics | `~/.copilot/session-store.db` (+ `~/.copilot/session-state`) |
| `copilot_memory` | VS Code Copilot **memory-tool** notes (repo & session)       | `…/workspaceStorage/<id>/GitHub.copilot-chat/memory-tool`    |
| `cline`          | Cline-family agent task histories                            | `…/Code/User/globalStorage`                                  |
| `cursor`         | Cursor Composer / chat history                               | `…/Cursor/User/globalStorage/state.vscdb`                    |
| `claude_code`    | Claude Code CLI session transcripts + real token metrics     | `~/.claude/projects` (`$CLAUDE_CONFIG_DIR/projects` if set)  |

Plus **import** sources (one-off uploads rather than watched paths):

- **ChatGPT** exports (`conversations.json`) — imported as many sessions.
- **Grok** exports — a conversation saved by a validated Grok export tool (e.g.
  the *Enhanced Grok Export* userscript). See [The Grok family](#the-grok-family).
- **Your own notes and files** — see
  [Managing your archive](managing-your-archive.md).

### Copilot memory notes

When the VS Code Copilot agent uses its **memory tool**, it writes durable
markdown notes that the chat log never contains (it records only that the tool
ran). Mark captures them by scope:

| On disk (under a workspace's `memory-tool/memories/`)       | Captured as                                                     |
|-------------------------------------------------------------|----------------------------------------------------------------|
| `repo/<name>.md` — cross-session repository knowledge       | its own `copilot_memory` session, `Repo memory · <name>`       |
| `<name>.md` — user-scoped notes (rare)                      | its own `copilot_memory` session, `Memory · <name>`            |
| `<session-id>/<name>.md` — one conversation's working notes | an **attachment on the chat session** that produced it (VS Code) |

Repo/user notes are attributed to their workspace's repository (so they join
that repo's facet) and carry **no** token or dollar cost — memory is knowledge,
not spend, so they never skew the usage dashboards. Session notes ride along
with their conversation: they appear as attachments in its detail view and
re-sync whenever that chat is re-indexed. Disable the standalone repo/user
indexing like any source with `MARK_SOURCE_COPILOT_MEMORY_ENABLED=0`.

### The Cline family

The `cline` adapter auto-detects several Cline-derived extensions and labels each
with its own source name:

| Extension id                   | Labelled as |
|--------------------------------|-------------|
| `saoudrizwan.claude-dev`       | `cline`     |
| `zoocodeorganization.zoo-code` | `zoocode`   |
| `rooveterinaryinc.roo-cline`   | `roo`       |
| `kilocode.kilo-code`           | `kilocode`  |

Unknown forks are still indexed — they get a label derived from their extension
id. To name one explicitly, add an override (see below).

### The Grok family

Grok has no local store to watch and no official export, so Mark imports Grok
conversations from **export tools** — browser userscripts/extensions that save a
conversation to JSON. Different tools emit different JSON, so Mark accepts only a
**curated set of validated formats**, each matched by a strict signature and
normalised to a single `grok` session type:

| Export tool                 | Recognised by                                        |
|-----------------------------|------------------------------------------------------|
| Enhanced Grok Export (v2.x) | top-level `platform: "grok"` + a `conversation` list |

Drop the exported `.json` onto the upload area (or `POST /api/uploads`) and Mark
imports it. Per-message Grok **modes** (deepsearch/think/…) are preserved on each
turn. An export from an **unrecognised** tool is never parsed on a guess — it
falls back to a plain searchable document, so nothing is lost.

**Adding another export tool** is a small, test-backed change rather than a new
adapter: add a format handler (a strict `matches` signature plus a
`conversations` mapper) to `mark/sources/grok.py`, plus a sample fixture and a
test. Every handler normalises to the same `grok` session, so the rest of Mark is
unaffected.

## Auto-discovery vs. overriding

You only configure a source when you want to **change** something. Precedence is:

```
built-in default  <  ~/.mark/sources.toml  <  MARK_* environment variables
```

A key you don't mention keeps its built-in defaults — you override only what you
set.

### `sources.toml`

Copy [`sources.example.toml`](../sources.example.toml) to `~/.mark/sources.toml`
(or point `MARK_SOURCES_FILE` elsewhere) and edit. Common edits:

```toml
# Disable a source (keeps already-indexed sessions; just stops scanning).
[sources.cursor]
enabled = false

# Add extra roots to scan — e.g. a synced copy from another machine.
[sources.vscode]
roots = [
  "~/Library/Application Support/Code/User/workspaceStorage",
  "~/sync/other-machine/Code/User/workspaceStorage",
]

# Point the Copilot CLI adapter at non-default paths.
[sources.copilot_cli]
roots = ["~/.copilot/session-store.db"]
options = { state_dir = "~/.copilot/session-state" }

# Teach the Cline adapter a fork it doesn't know yet (ext-id = label).
[sources.cline]
options.extensions = { "some.new-cline-fork" = "myagent" }
```

> `roots` means different things per adapter: workspaceStorage dirs for VS Code,
> the store DB path for the Copilot CLI, globalStorage dirs for the Cline family,
> and the `state.vscdb` files for Cursor.

### Environment overrides

For one-off runs without editing a file:

| Variable                                  | Effect                                  |
|-------------------------------------------|-----------------------------------------|
| `MARK_SOURCE_<NAME>_ENABLED=0`            | Disable a source for one run            |
| `MARK_SOURCE_<NAME>_ROOTS=/a:/b`          | Override roots (`os.pathsep`-separated) |
| `MARK_SOURCES_FILE=/path/to/sources.toml` | Use a non-default config location       |

`<NAME>` is the upper-cased source key, e.g. `MARK_SOURCE_VSCODE_ENABLED=0` or
`MARK_SOURCE_CURSOR_ROOTS=…`.

## How syncing works

Mark keeps your archive current on its own:

- **On startup**, it runs one import pass.
- **While running**, it watches for changes — cheaply fingerprinting the on-disk
  sources every few seconds and running an **incremental** import only when
  something actually changed (a session ends, updates, or appears).
- **Manually**, click the **⟳** button to force a re-scan immediately.

| Variable               | Default | Purpose                                                        |
|------------------------|---------|----------------------------------------------------------------|
| `MARK_AUTO_SYNC`       | `1`     | `0` keeps the startup scan but disables polling and auto-retry |
| `MARK_SYNC_INTERVAL`   | `20`    | Seconds between change checks (minimum 5)                     |
| `MARK_SYNC_RETRY_BASE` | `5`     | Initial automatic retry delay in seconds                      |
| `MARK_SYNC_RETRY_MAX`  | `300`   | Maximum automatic retry delay in seconds                      |

Source databases are read **read-only**; for live stores (like the Copilot CLI
DB) Mark reads a consistent snapshot. Your original history is never modified.

## Read-only and non-destructive

- **Disabling** a source stops scanning but **never deletes** indexed sessions.
- To actually remove data, use the explicit delete/prune actions — see
  [Managing your archive](managing-your-archive.md#delete-a-session).

## Inspecting sources in the app

The sidebar status card shows the active embedding engine and last sync. The
`/api/sources` endpoint (used by the UI) reports each source's effective config:
whether it's enabled, its resolved roots, whether those paths exist, and how many
sessions are indexed from it.
