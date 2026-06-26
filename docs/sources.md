# Sources & syncing

A **source** is a place Mark reads AI chat history from. With no configuration at
all, Mark **auto-discovers** every supported source and keeps them in sync. This
page explains what's supported, how to override paths, and how syncing works.

## Supported sources

| Source key | What it indexes | Where it lives (auto-detected) |
| --- | --- | --- |
| `vscode` | VS Code inline/agent chats | `…/Code/User/workspaceStorage` (Stable, Insiders, VSCodium) |
| `copilot_cli` | Copilot CLI / agent-store conversations + real token metrics | `~/.copilot/session-store.db` (+ `~/.copilot/session-state`) |
| `cline` | Cline-family agent task histories | `…/Code/User/globalStorage` |
| `cursor` | Cursor Composer / chat history | `…/Cursor/User/globalStorage/state.vscdb` |

Plus **import** sources (one-off uploads rather than watched paths):

- **ChatGPT** exports (`conversations.json`) — imported as many sessions.
- **Your own notes and files** — see
  [Managing your archive](managing-your-archive.md).

### The Cline family

The `cline` adapter auto-detects several Cline-derived extensions and labels each
with its own source name:

| Extension id | Labelled as |
| --- | --- |
| `saoudrizwan.claude-dev` | `cline` |
| `zoocodeorganization.zoo-code` | `zoocode` |
| `rooveterinaryinc.roo-cline` | `roo` |
| `kilocode.kilo-code` | `kilocode` |

Unknown forks are still indexed — they get a label derived from their extension
id. To name one explicitly, add an override (see below).

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

| Variable | Effect |
| --- | --- |
| `MARK_SOURCE_<NAME>_ENABLED=0` | Disable a source for one run |
| `MARK_SOURCE_<NAME>_ROOTS=/a:/b` | Override roots (`os.pathsep`-separated) |
| `MARK_SOURCES_FILE=/path/to/sources.toml` | Use a non-default config location |

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
| `MARK_AUTO_SYNC` | `1` | Set `0` to disable background syncing (manual re-scan only) |
| `MARK_SYNC_INTERVAL` | `20` | Seconds between change checks (minimum 5) |

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
