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

| On disk (under a workspace's `memory-tool/memories/`)       | Captured as                                                      |
|-------------------------------------------------------------|------------------------------------------------------------------|
| `repo/<name>.md` — cross-session repository knowledge       | its own `copilot_memory` session with the Markdown attached      |
| `<name>.md` — user-scoped notes (rare)                      | its own `copilot_memory` session with the Markdown attached      |
| `<session-id>/<name>.md` — one conversation's working notes | an **attachment on the chat session** that produced it (VS Code) |

Repo/user notes are attributed to their workspace's repository (so they join
that repo's facet), preserve the observed Markdown as an attachment, and carry
**no** token or dollar cost — memory is knowledge, not spend, so they never skew
the usage dashboards. Session notes ride along with their conversation: they
appear as attachments in its detail view and re-sync whenever that chat is
re-indexed. Disable the standalone repo/user indexing like any source with
`MARK_SOURCE_COPILOT_MEMORY_ENABLED=0`.

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

```text
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

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARK_AUTO_SYNC` | `1` | `0` keeps the startup scan but disables polling and auto-retry |
| `MARK_SYNC_INTERVAL` | `20` | Seconds between change checks (minimum 5) |
| `MARK_SYNC_RETRY_BASE` | `5` | Initial automatic retry delay in seconds |
| `MARK_SYNC_RETRY_MAX` | `300` | Maximum automatic retry delay in seconds |

Source databases are read **read-only**; for live stores (like the Copilot CLI
DB) Mark reads a consistent snapshot. Your original history is never modified.

## Read-only and non-destructive

- **Disabling** a source stops scanning but **never deletes** indexed sessions.
- To actually remove data, use the explicit delete/prune actions — see
  [Managing your archive](managing-your-archive.md#delete-a-session).

## Inspecting sources in the app

Open **Sources** in the top bar, choose **Sources & index health** in the command
palette, or follow `#/sources`. The view combines the ingestion coordinator,
search-index coverage, and per-adapter diagnostics. Opening or refreshing it does
not import content, start model downloads, or perform inference.

### Source states and history

Each adapter shows its resolved root paths, access state, indexed-session count,
latest outcome, last completed scan, and last check. States distinguish:

- **Scanned:** a successful scan was recorded for the current paths/options.
- **Detected / not yet scanned:** roots are accessible, but no successful scan of
  this configuration has been recorded yet.
- **Missing roots / Partial roots:** none or only some of the configured roots
  are currently accessible at their expected locations.
- **Disabled:** scanning is disabled; already indexed sessions are retained.
- **Error:** configuration, path access, an adapter attempt, or its post-scan
  fingerprint check failed. The error and suggested next action remain visible.
- **Import only:** the source is a user-supplied export, not a watched store.
  Recognized import success/failure is retained too; retry it by supplying an
  export with **Add**, not by running a watched-source scan.

Paths are those visible to the **running Mark process**. In Docker, compare them
with the container-side mount paths, not just their host locations. Counts include
hidden sessions and disabled-source sessions and use stable adapter ownership
where available, even for custom Cline-family labels.

History is stored in the existing local metadata table and survives restarting
Mark. An unchanged source fingerprint updates the last check, not the last
successful scan time. Configuration changes are identified so earlier results are
not presented as proof that new paths were scanned. The last recorded error is
kept in an expandable section after recovery; it is separate from the current
error state. Before a source has been checked, timestamps are **Not recorded**—no
historical success is inferred from existing rows. The first pass after this
upgrade may revisit adapters to establish that history.

Diagnostics reflect adapter outcomes and root access, not a completeness audit:
an adapter may skip unsupported or malformed individual records. A Scanned state
does not guarantee every upstream conversation was captured. Normal query filters,
hidden sessions, and unsupported export formats can also explain missing results.

### Search-index coverage

The view distinguishes the configured preferred model from the backend/model
identity persisted with the active or building index. It also shows:

- Keyword-indexed chunks versus stored chunks.
- Compatible vectors versus **eligible** chunks under the configured per-session
  sampling cap, plus how many eligible chunks still need vectors.
- Chunks excluded intentionally by that cap, total stored vector rows, and the
  semantic generation.
- Empty-archive, pending-verification, error, built-in-fallback, and active
  semantic-index states.

Coverage is based on one SQLite read snapshot and reuses the writer's eligibility
policy. A vector counts only when its fingerprint, model, dimensions, and byte
length match the selected identity. Unknown identity is shown as **Unknown**, not
0% complete. A fully populated stored index is not marked active until it is
verified and compatible with this process's already loaded backend. An index
published by a different process/model may require verification or repair here.

**100% of eligible chunks is not 100% of all transcript chunks**, and neither is a
relevance/quality score. Keyword indexing is not subject to the semantic sampling
cap. Coverage spans the entire conversation archive, including hidden and disabled
sources; curated solution copies are separate.

**Built-in fallback** explicitly means the lightweight hashing vectorizer is in
use, not a transformer. Installing the optional semantic dependency and restarting
Mark may improve recall. The health view does not diagnose every earlier model-load
fallback reason, and retrying does not install packages or switch the cached backend.

### Retry and refresh

- **Refresh diagnostics** reads current source/index state only. Detailed coverage
  refreshes about every ten seconds while the health view is open, using the app's
  existing heartbeat; normal status polling does not run the coverage queries.
- **Re-scan / retry** queues an incremental pass over enabled watched sources.
- **Retry semantic indexing** uses the same queue with semantic-repair intent.
  It is not a separate worker or a destructive full rebuild.

Both retry actions show whether work was accepted or already covered, plus
queued/running/completed states, errors, worker availability, retry attempts, and
the next automatic retry when scheduled. Automatic retry remains governed by
`MARK_AUTO_SYNC` and the existing backoff settings. If diagnostics cannot be read,
the view marks displayed values as potentially stale instead of implying success.

A keyword coverage gap may require investigating and rebuilding unchanged source
content; an ordinary incremental retry is not guaranteed to repair it. The health
view does not automatically trigger a full rebuild, change source settings, or
delete data.

### Health API

- `GET /api/sources` retains its existing list shape and adds `health`,
  `root_status`, `history`, `configuration_changed`, `error`, and `action` fields.
- `GET /api/health` returns `checked_at`, `coordinator`, `sources`, and `index`.
  Index details include `coverage`, `configured_model`, and `per_session_cap`.
- `GET /api/status` remains the cheap coordinator/semantic-status heartbeat and
  adds `stopping` so retry controls can reflect shutdown.
- `POST /api/reindex?repair_semantic=true` requests semantic verification/repair
  through the existing coordinator. The `admission` response remains authoritative.

Only bounded outcome metadata, timestamps, and counts are retained for diagnostics;
source option contents are represented by a fingerprint rather than copied into
history. No database schema migration or reindex is required for this feature.
