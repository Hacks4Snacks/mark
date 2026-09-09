# Searching & filtering

Mark's whole reason to exist is finding the conversation you half-remember. This
page explains the three search modes, the sidebar facets, sorting, and related
sessions.

## Search modes

The toggle next to the search bar picks how a query is matched:

| Mode                 | What it does                                 | Reach for it when…                                     |
|----------------------|----------------------------------------------|--------------------------------------------------------|
| **Hybrid** (default) | Fuses keyword precision with semantic recall | Almost always — it's the best general default          |
| **Semantic**         | Pure "find by meaning" via vector embeddings | You remember the *idea* but not the words              |
| **Keyword**          | Classic exact-term FTS5 / BM25 search        | You know an exact identifier, error string, or command |

**Why hybrid wins.** Searching `how I fixed the auth timeout` will surface a
session even if you actually wrote *"token expiry bug"* — semantic recall finds
the meaning, while keyword precision keeps exact matches (function names, error
codes) at the top. Under the hood Mark runs both rankers and merges them with
**Reciprocal Rank Fusion (RRF)** at the chunk level, then picks the best-matching
chunk per session.

An empty query is not an error — it becomes a **browse** of everything, ordered
by your chosen sort and narrowed by whatever facets are active.

### Exact phrase search

Wrap a phrase in double quotes to require those words together and in that
order. For example, `"repository evidence"` matches *repository evidence*, but
not *evidence from the repository* or *repository logs provide evidence*.

Quoted phrases use the complete keyword index and suppress semantic expansion,
even when **Hybrid** or **Semantic** mode is selected. This keeps every returned
session tied to a literal phrase match. Multiple quoted phrases are all
required. In a mixed query such as `rotation "repository evidence"`, the quoted
phrase is required and the unquoted text is matched as an additional keyword.
Without quotes, terms retain the normal keyword/semantic behavior described
above.

## The sidebar facets

Every filter is additive — combine as many as you like. The result count and an
"active filters" strip update live.

- **Source** — VS Code, Copilot CLI, Cline-family agents, Cursor, ChatGPT, and
  your own notes/uploads. See [Sources](sources.md).
- **Repositories** — the repos a session touched (auto-detected from workspace
  metadata).
- **Topics** — the locally generated topic tags (a tag cloud). You can also add
  or remove tags by hand. Selecting multiple topics uses **match all** semantics:
  a conversation must contain every selected topic. See
  [Managing your archive](managing-your-archive.md#topics--tags).
- **Date range** — a from/to window on each session's activity.
- **Show hidden only** — surface sessions you've hidden so you can review or
  restore them.

Use **Clear filters** to reset everything in one click.

## Sorting

The **Sort** dropdown controls ordering:

| Option          | Behaviour                                                                                                       |
|-----------------|-----------------------------------------------------------------------------------------------------------------|
| **Most recent** | Newest first when browsing. For an active query this keeps **relevance order**, so the best matches stay on top |
| **Oldest**      | Oldest first; undated sessions sort last                                                                        |
| **Longest**     | By turn count — the meatiest conversations first                                                                |
| **Title A–Z**   | Alphabetical                                                                                                    |

## Reading a result

Click any result to open the **detail view**, which shows:

- The full conversation, turn by turn, with assistant *thinking* where captured.
- **Files touched**, **code blocks**, and **tools** that ran during the session.
- The **session id** and a copyable resume command (e.g. `copilot --resume <id>`),
  configurable via `MARK_RESUME_CMD`.
- A **reading-progress** bar as you scroll a long transcript.

### Jump to evidence

Search results open the turn containing their best-matching passage. Mark loads
only the page containing that turn, highlights matching text, and outlines the
selected turn. Use **Load previous turns** or **Load more** to expand its context.
Browse results and document-only records still open at the conversation/document
level when no turn target exists.

The sticky **Find in conversation** control searches all indexed user and
assistant messages, including turns that have not been loaded in the browser.
Press Enter or **Find** to search. Double quotes require a phrase, just as in
keyword search. The previous/next controls move between **matching turns**, not
individual word occurrences, and show your position in the complete match set.
Session titles, tags, attachments, and display-only reasoning are not part of
this transcript search. Semantic results can point to a relevant turn without
a literal highlight; Mark indicates that distinction instead of fabricating one.

An oversized target opens a bounded excerpt around the matched text, with at
most 4,000 characters per message/reasoning field. **Load full turn** remains an
explicit action. Finding or navigating evidence does not eagerly render the
whole conversation or change the archive.

Each turn has a **Copy turn link** button. A link such as
`#/session/example?turn=40&q=%22orbital%20evidence%22` identifies turn 40 and retains
the search query; the turn number in a URL is one-based. Reload and browser
back/forward preserve the target. A missing or invalid turn falls back to the
beginning with an explanatory message. These links refer to your local archive;
they are not public sharing links. They rely on source turn numbering rather
than regenerated chunk IDs and are not immutable snapshots of edited content.

### Evidence API

- Search responses include an additive `match` object containing `turn_index`,
  `source_type`, and `query`. Document matches have no turn target.
- `GET /api/sessions/{id}?turn_index=39` loads the normal-sized page containing
  that zero-based turn and reports `target_turn_found` plus `turns_offset`.
- `GET /api/sessions/{id}/matches?q=...` returns ordered, deduplicated
  `turn_indices`, `total`, `offset`, `limit`, and `has_more`. Pages are limited
  to 100 turns. An optional `turn_index` selects the matching page near a target;
  `target_position` reports how many matches precede it.
- `GET /api/sessions/{id}/turns/39?preview=true&q=...` returns a bounded rendered
  excerpt, marked `preview` when truncated. Omit `preview` to retain the existing
  explicit full-turn read. Evidence queries are limited to 2,000 characters.

Exact-ID evidence reads retain the same visibility behavior as conversation
detail reads, including access to a user-hidden conversation by its ID.

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

| Variable                            | Default                  | Purpose                                                        |
|-------------------------------------|--------------------------|----------------------------------------------------------------|
| `MARK_EMBED_MODEL`                  | `BAAI/bge-small-en-v1.5` | fastembed model id                                             |
| `MARK_EMBED_THREADS`                | a quarter, max 4         | CPU cap for the transformer backend (`0` = all cores, fastest) |
| `MARK_MAX_EMBED_CHUNKS_PER_SESSION` | `40`                     | Cap on embedded chunks per session                             |

> **Keyword search always indexes every chunk** — nothing is lost from FTS. Only
> *embeddings* are capped per session, because semantic search loads vectors into
> memory and one giant agent transcript could otherwise dominate the set. Capped
> sessions are sampled evenly across their complete chunk sequence. Caps of two
> or more can preserve both speakers and evidence from the beginning to the end;
> a cap of one necessarily retains only the first chunk.

Automatic topics prioritize session titles and user intent, keep assistant text
bounded, and discard URL, path, host, filename, and injected-context noise. A
full reindex regenerates existing automatic topics and summaries; manual topics
are preserved.

See the full [configuration reference](configuration.md) for everything else.
