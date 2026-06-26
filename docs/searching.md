# Searching & filtering

Mark's whole reason to exist is finding the conversation you half-remember. This
page explains the three search modes, the sidebar facets, sorting, and related
sessions.

## Search modes

The toggle next to the search bar picks how a query is matched:

| Mode | What it does | Reach for it when… |
| --- | --- | --- |
| **Hybrid** (default) | Fuses keyword precision with semantic recall | Almost always — it's the best general default |
| **Semantic** | Pure "find by meaning" via vector embeddings | You remember the *idea* but not the words |
| **Keyword** | Classic exact-term FTS5 / BM25 search | You know an exact identifier, error string, or command |

**Why hybrid wins.** Searching `how I fixed the auth timeout` will surface a
session even if you actually wrote *"token expiry bug"* — semantic recall finds
the meaning, while keyword precision keeps exact matches (function names, error
codes) at the top. Under the hood Mark runs both rankers and merges them with
**Reciprocal Rank Fusion (RRF)** at the chunk level, then picks the best-matching
chunk per session.

An empty query is not an error — it becomes a **browse** of everything, ordered
by your chosen sort and narrowed by whatever facets are active.

## The sidebar facets

Every filter is additive — combine as many as you like. The result count and an
"active filters" strip update live.

- **Source** — VS Code, Copilot CLI, Cline-family agents, Cursor, ChatGPT, and
  your own notes/uploads. See [Sources](sources.md).
- **Repositories** — the repos a session touched (auto-detected from workspace
  metadata).
- **Topics** — the locally generated topic tags (a tag cloud). You can also add
  or remove tags by hand — see [Managing your archive](managing-your-archive.md#topics--tags).
- **Date range** — a from/to window on each session's activity.
- **Show hidden only** — surface sessions you've hidden so you can review or
  restore them.

Use **Clear filters** to reset everything in one click.

## Sorting

The **Sort** dropdown controls ordering:

| Option | Behaviour |
| --- | --- |
| **Most recent** | Newest first when browsing. For an active query this keeps **relevance order**, so the best matches stay on top |
| **Oldest** | Oldest first; undated sessions sort last |
| **Longest** | By turn count — the meatiest conversations first |
| **Title A–Z** | Alphabetical |

## Reading a result

Click any result to open the **detail view**, which shows:

- The full conversation, turn by turn, with assistant *thinking* where captured.
- **Files touched**, **code blocks**, and **tools** that ran during the session.
- The **session id** and a copyable resume command (e.g. `copilot --resume <id>`),
  configurable via `MARK_RESUME_CMD`.
- A **reading-progress** bar as you scroll a long transcript.

### Related sessions

Each conversation links to a handful of **related sessions** — found by semantic
similarity to the one you're reading — so you can follow a train of thought across
separate chats without searching again.

## Save a search

Run any query or set of filters, then click **▦ Save as collection** in the list
header to turn that exact view into an **auto-updating** group. New sessions that
match flow in on their own. See [Collections](collections.md).

## Semantic engine

Semantic search needs vectors. Mark picks a backend automatically, in order:

1. [`fastembed`](https://github.com/qdrant/fastembed) — an ONNX transformer
   (best quality; installed by the `semantic` extra, no PyTorch needed).
2. [`model2vec`](https://github.com/MinishLab/model2vec) — fast static
   embeddings.
3. A **built-in NumPy hashing vectorizer** that always works offline.

The status card in the sidebar shows which engine is active. To upgrade quality:

```bash
pip install -r requirements-optional.txt
# or simply install the extra:
pip install 'markive[semantic]'
```

### Tuning embeddings

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARK_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model id |
| `MARK_EMBED_THREADS` | half your cores | CPU cap for the transformer backend (`0` = all cores, fastest) |
| `MARK_MAX_EMBED_CHUNKS_PER_SESSION` | `40` | Cap on embedded chunks per session |

> **Keyword search always indexes every chunk** — nothing is lost from FTS. Only
> *embeddings* are capped per session, because semantic search loads vectors into
> memory and one giant agent transcript could otherwise dominate the set. The
> earliest chunks per session win (user prompts come first).

See the full [configuration reference](configuration.md) for everything else.
