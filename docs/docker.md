# Running in Docker

Mark ships a Docker Compose setup that runs the whole app in a container while
keeping your conversations on your machine. Your chat stores are mounted
**read-only**; only the derived index is written, to a named volume.

## Quick start

```bash
docker compose up --build -d      # http://127.0.0.1:8765
```

The server binds to `127.0.0.1` on the **host only** — this is a personal app, not
something to expose on a network.

To pick up new sessions, open the UI and click **⟳**, or `docker compose restart`.

## How the mounts work

The Compose file mounts your local conversation stores **read-only** onto the
exact in-container paths Mark auto-detects. That means **no `MARK_*` path
variables are needed** — you get the same "no config = discover everything"
behaviour as a local install.

- The index (`mark.db`) lives in the named volume **`mark-data`** (`/app/data`
  inside the container), so it survives rebuilds.
- The mounts are `…:ro` — Mark can read your history but never write to it.

## Customising per OS / editor

The shipped file is preset for **macOS + VS Code (Stable)**. Only edit the **host
(left)** side of each mount; keep the **container (right)** side as
`/home/mark/...` — that's where Mark looks.

| Setup            | Change the host path to                                |
|------------------|--------------------------------------------------------|
| VS Code Insiders | `Code - Insiders` instead of `Code`                    |
| Linux            | `${HOME}/.config/Code/User/...`                        |
| Windows          | `%APPDATA%/Code/User/...` (use WSL paths under Docker) |

Cursor mounts are included and optional — remove them if you don't use Cursor.

## Optional tuning

Uncomment the `environment:` block in `docker-compose.yml` to tune indexing:

```yaml
environment:
  # Cap on embedded chunks per session (keyword/FTS still indexes all chunks).
  MARK_MAX_EMBED_CHUNKS_PER_SESSION: "40"
  # Embedding CPU cap while indexing. Default = half your cores; "0" = all.
  MARK_EMBED_THREADS: "4"
```

See the full [configuration reference](configuration.md) for every variable.

## Advanced source config

Need to disable a source, add a Cline-family label, or point at extra roots?
Bind-mount a `sources.toml` (same format as a local `~/.mark/sources.toml`; see
[`sources.example.toml`](../sources.example.toml)). The Compose file ships a
commented mount line for it:

```yaml
- "./sources.toml:/app/data/sources.toml:ro"
```

Use the **in-container** mount targets for any `roots` you set. More detail in
[Sources & syncing](sources.md).
