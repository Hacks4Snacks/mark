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

| Variable             | Default | Purpose                                          |
|----------------------|---------|--------------------------------------------------|
| `MARK_AUTO_SYNC`     | `1`     | `0` disables background syncing (manual ⟳ only)  |
| `MARK_SYNC_INTERVAL` | `20`    | Seconds between source-change checks (minimum 5) |

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
| `MARK_EMBED_THREADS`                | half your cores          | CPU cap for the transformer backend; `0` uses all cores (fastest)     |
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

| Variable            | Default                  | Purpose                                 |
|---------------------|--------------------------|-----------------------------------------|
| `MARK_OLLAMA_URL`   | `http://localhost:11434` | Ollama endpoint                         |
| `MARK_OLLAMA_MODEL` | *(auto-pick)*            | Force a specific installed Ollama model |

See [Ask your history](ask.md).

## Uploads & attachments

| Variable                    | Default   | Purpose                                                           |
|-----------------------------|-----------|-------------------------------------------------------------------|
| `MARK_MAX_UPLOAD_BYTES`     | `25 MiB`  | Largest file accepted via **Add**                                 |
| `MARK_MAX_ATTACHMENT_BYTES` | `512 KiB` | Largest agent-created file snapshotted for later viewing/download |

See [Managing your archive](managing-your-archive.md).
