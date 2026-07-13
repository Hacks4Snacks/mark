# Configuration reference

Every setting is **optional**. With nothing configured, Mark auto-discovers your
sources, indexes them, and serves the UI on `127.0.0.1:8765`. Configure only what
you want to change.

Configuration comes from two places:

- **Environment variables** (`MARK_*`) — listed below.
- **`sources.toml`** — which chat stores to scan; see [Sources & syncing](sources.md).

Precedence for source settings is: built-in default < `sources.toml` < env vars.

## Server & data

| Variable           | Default                 | Purpose                                                  |
|--------------------|-------------------------|----------------------------------------------------------|
| `MARK_HOST`        | `127.0.0.1`             | Bind address. Keep on localhost — this is a personal app |
| `MARK_PORT`        | `8765`                  | Server port                                              |
| `MARK_DATA_DIR`    | `~/.mark`               | Base directory for the DB and uploads                    |
| `MARK_DB_PATH`     | `$DATA_DIR/mark.db`     | SQLite index location                                    |
| `MARK_UPLOADS_DIR` | `$DATA_DIR/uploads`     | Where uploaded files are stored                          |
| `MARK_RESUME_CMD`  | `copilot --resume {id}` | Resume command shown in the UI (`{id}` is substituted)   |

## Syncing

| Variable               | Default | Purpose                                                        |
|------------------------|---------|----------------------------------------------------------------|
| `MARK_AUTO_SYNC`       | `1`     | `0` keeps the startup scan but disables polling and auto-retry |
| `MARK_SYNC_INTERVAL`   | `20`    | Seconds between source-change checks (minimum 5)               |
| `MARK_SYNC_RETRY_BASE` | `5`     | Initial automatic retry delay in seconds                       |
| `MARK_SYNC_RETRY_MAX`  | `300`   | Maximum automatic retry delay in seconds                       |

## Sources

| Variable                     | Default                  | Purpose                                 |
|------------------------------|--------------------------|-----------------------------------------|
| `MARK_SOURCES_FILE`          | `$DATA_DIR/sources.toml` | Path to the source-config TOML          |
| `MARK_SOURCE_<NAME>_ENABLED` | per-source               | `0` disables a source for one run       |
| `MARK_SOURCE_<NAME>_ROOTS`   | per-source               | Override roots (`os.pathsep`-separated) |

`<NAME>` is the upper-cased source key, e.g. `VSCODE`, `COPILOT_CLI`, `CLINE`,
`CURSOR`. See [Sources & syncing](sources.md) for details.

## Search & embeddings

| Variable                            | Default                  | Purpose                                                               |
|-------------------------------------|--------------------------|-----------------------------------------------------------------------|
| `MARK_EMBED_MODEL`                  | `BAAI/bge-small-en-v1.5` | fastembed model id (when the `semantic` extra is installed)           |
| `MARK_EMBED_THREADS`                | a quarter, max 4         | CPU cap for the transformer backend; `0` uses all cores (fastest)     |
| `MARK_HASH_DIM`                     | `1024`                   | Dimension of the built-in offline hashing vectorizer fallback         |
| `MARK_MAX_CHUNK_CHARS`              | `2000`                   | Window size for splitting long turns into search chunks               |
| `MARK_MAX_EMBED_CHUNKS_PER_SESSION` | `40`                     | Cap on *embedded* chunks per session (keyword/FTS indexes all chunks) |

## Cost & usage

| Variable            | Default        | Purpose                                                      |
|---------------------|----------------|--------------------------------------------------------------|
| `MARK_PRICING_FILE` | built-in table | JSON of `{model: [input, output, cached]}` USD per 1M tokens |

See [Usage & cost analytics](usage-and-cost.md#customising-prices) for the file
format.

## Ask (local LLM)

Ask is **disabled by default** while it's being refined. Set `MARK_ENABLE_ASK=1`
to turn it on; until then the **✦ Ask** UI stays hidden and its API routes are
not mounted.

| Variable                          | Default                         | Purpose                                                                       |
|-----------------------------------|---------------------------------|-------------------------------------------------------------------------------|
| `MARK_ENABLE_ASK`                 | `0`                             | Master switch for the whole Ask feature (`1` enables it; off by default)      |
| `MARK_OLLAMA_URL`                 | `http://localhost:11434`        | Ollama endpoint                                                               |
| `MARK_OLLAMA_MODEL`               | *(auto-pick)*                   | Force a specific installed Ollama model                                       |
| `MARK_ASK_NUM_CTX_CAP`            | `16384`                         | Ceiling on the context window requested (clamped to the model's own length)   |
| `MARK_ASK_DEFAULT_NUM_CTX`        | `8192`                          | Fallback window when the model doesn't report a context length                |
| `MARK_ASK_RESERVE_OUTPUT_TOKENS`  | `1024`                          | Tokens held back within the window for the answer                             |
| `MARK_ASK_MAX_CANDIDATE_PASSAGES` | `80`                            | Passages retrieved (and reranked) before packing into the budget              |
| `MARK_ASK_PER_SESSION_PASSAGES`   | `2`                             | Max passages drawn from any one session (favours breadth across sessions)     |
| `MARK_ASK_NEIGHBOR_TURNS`         | `1`                             | Surrounding turns included on each side of a matched passage                  |
| `MARK_ASK_MAX_TURN_CHARS`         | `4000`                          | Cap on characters from the matched passage itself                             |
| `MARK_ASK_NEIGHBOR_CHARS`         | `800`                           | Cap on characters from each surrounding neighbour turn (drives breadth)       |
| `MARK_ASK_RERANK`                 | `1`                             | Cross-encoder reranking of passages (needs `semantic` extra; `0` disables)    |
| `MARK_RERANK_MODEL`               | `Xenova/ms-marco-MiniLM-L-6-v2` | fastembed cross-encoder used for reranking                                    |

The number of **sources** an answer cites is not a fixed setting: Ask packs as
many distinct sessions as fit the model's context window, so a larger
`MARK_ASK_NUM_CTX_CAP` (or a roomier model) yields more sources.

See [Ask your history](ask.md).

## Uploads & attachments

| Variable                    | Default   | Purpose                                                           |
|-----------------------------|-----------|-------------------------------------------------------------------|
| `MARK_MAX_UPLOAD_BYTES`     | `25 MiB`  | Largest file accepted via **Add**                                 |
| `MARK_MAX_ATTACHMENT_BYTES` | `512 KiB` | Largest agent-created file snapshotted for later viewing/download |

See [Managing your archive](managing-your-archive.md).
