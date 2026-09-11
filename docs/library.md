# Snippet & solution library

The **Library** has two separate views: **Extracted snippets** collects code blocks
from your conversations, and **Curated solutions** keeps the answers and snippets
you explicitly save for reuse.

Open it from the **Library** button in the top bar. `#/library` opens extracted
snippets; `#/library/curated` opens saved solutions. The button remembers the last
Library view used in the current browser session.

## What's in it

During ingest, Mark pulls every fenced code block out of your conversations and
stores it with its language and a link back to the source session. The library
lets you browse and filter them all in one place.

Each snippet card shows:

- The **language** tag.
- The **source session** title (with its source icon and repository), which opens
  the turn containing that snippet in one click when its turn is known.
- The session's last-update date, or creation date when no update is recorded.
- A **copy** button to grab the snippet to your clipboard.
- **Save solution** to preview and save an independent, annotated copy.

## Filtering

- **Filter by content** — literal substring match against the snippet body
  (e.g. find every block mentioning `kubectl` or `JWT`). `%`, `_`, and backslashes
  are literal characters, not query wildcards. This is not semantic or FTS search.
- **Filter by language** — an exact language selection with snippet counts.
- **Commands only** — a toggle that narrows to runnable shell snippets:
  `bash`, `sh`, `shell`, `zsh`, `console`, `powershell`, `ps1`, and similar.
  It temporarily overrides and disables the language selection; turning it off
  restores that language filter. Commands are never executed by Mark.
- **Repository** — an exact project selection. All repositories includes
  snippets whose session has no repository recorded.
- **From / Through (UTC)** — inclusive calendar dates on the conversation's last
  update, falling back to its creation timestamp. These are session dates, not
  individual turn dates. Undated sessions appear only when no date bound is set.

Combine content, repository, and dates with either language or commands-only.
These controls belong to **Extracted snippets**, independently of conversation
search's sidebar filters and the Curated solutions tab.

Dropdown counts cover all visible, browsable snippets, not just matches for the
current filters. Hidden sessions and disabled adapters are excluded from results,
totals, and dropdowns. A selected repository/language that disappears is retained
as **not currently available** so refreshing cannot silently broaden your query.

The **Commands only** view is the quickest way to recover *"that one CLI
incantation I ran three weeks ago."*

## Browsing all results

**Previous / Next** pages show up to **80 snippets at a time**, with an exact
matching total and range such as **81–160 of 187**, rather than stopping at
**80+**. Controls are available above and below multi-page results. Moving pages
replaces the displayed cards instead of growing an unbounded list. Keyboard page
activation moves focus to the range above the new results.

Changing any filter or choosing **Clear filters** returns to the first page.
Opening a source turn and returning, or switching between Library tabs, retains
the extracted page and filters in the current app instance. A full browser reload
starts extracted browsing at its defaults; filter/page URLs and persistent search
history are not part of this increment.

**Refresh** reloads the first page and dropdown choices without changing filters;
it does not ingest new source content. A failed page clears the old count and
offers **Retry this page** at the same offset. Invalid date ranges show an inline
message instead of displaying results for a different scope.

Rows sort by the session's update/creation **instant**, newest first, with a
unique snippet-ID tiebreaker. Pagination neither skips nor duplicates entries in
an unchanged archive. Each response's total and rows share one database snapshot;
separate page requests do not. Ingestion, hiding, deletion, or source enablement
changes between pages can move results. Refresh to start from the latest state.
If a requested page has disappeared, the UI moves to the last available page.

## Extracted snippet API

- `GET /api/snippets`: optional `q`, `language`, `commands`, `repo`, `date_from`,
  `date_to`, `offset`, and `limit`. Dates use `YYYY-MM-DD` and the inclusive UTC
  semantics above. `commands=true` overrides `language` as before.
- Responses retain `snippets` and add `total`, `offset`, `limit`, and `has_more`.
  `offset` defaults to 0 and must be a nonnegative SQLite integer. `limit` defaults
  to 80 and keeps the existing clamp to 1–300; the effective value is returned.
  An offset beyond the end returns an empty page and the exact total, with
  `has_more=false`. Invalid dates, reversed ranges, or invalid offsets return 422.
- `GET /api/snippets/languages`: visible language/count pairs, excluding blocks
  that are not browsable. Counts do not apply the active filters.
- `GET /api/snippets/repositories`: visible repository/count pairs under the same
  browsable-snippet and visibility rules, alphabetically ordered. Sessions without
  a repository are included by All repositories but have no dropdown entry.

Existing snippet fields and the Python `snippets()` list interface remain
compatible. The paged repository interface is `list_snippets()`. No migration,
reindex, or additional dependency is required.

## Curated solutions

### Save an answer or snippet

1. Click **Save answer** below an assistant response, or **Save solution** on an
   extracted snippet.
2. Review the source content and provenance in the dialog. Nothing is saved just
   by opening the preview.
3. Choose a title, add annotations and environment/version prerequisites, and
   optionally add tags or mark the solution as a favorite.
4. Set the review status to **Needs review** (the default), **Verified**, or
   **Outdated**, then save.

Status is your own assessment, not an automated assertion that a command is safe.
Verification notes and prerequisites make that assessment useful later. No
content is sent to an LLM and no saved command is executed by Mark.

Saving captures the **full assistant answer or selected code block**, not a
rendered preview, HTML, reasoning, or the whole conversation. Source copies are
read-only; edit annotations to record corrections or alternatives. Sources above
100,000 characters are rejected rather than silently truncated. Save a smaller
extracted snippet for an oversized answer.

Saving identical content from the same source turn again returns the existing
solution without overwriting its annotations. Unchanged snippets can still be
saved if a sync regenerates their internal IDs while the dialog is open. If the
underlying content changed since preview, saving is refused so you can review the
new source deliberately.

### Browse and maintain

Switch to **Curated solutions** to search titles, copied content, annotations,
tags, and prerequisites. Combine free-text search with an exact tag, review
status, and **Favorites only**. Favorites sort first, followed by the most
recently updated solutions. Previous/next pages show 25 entries at a time with a
total count; this is independent of extracted-snippet filters and pagination.

Use **Open / edit** to update the title, annotations, tags, favorite, review
status, or prerequisites. Tags are normalized to lowercase, deduplicated, and
limited to 20 tags of 40 characters each. Titles allow 160 characters;
annotations allow 10,000 and prerequisites allow 4,000.

The source record shows the original session, turn, repository, source type, and
whether the original content is still available, has changed, or is missing.
**Open source turn** uses the evidence links from ENH-01; unavailable sources do
not leave an active broken link. A changed-source indicator does not overwrite
your saved content or your manually chosen review status.

**Copy content** copies the exact saved text. **Delete saved copy** removes only
that solution and its annotations after confirmation. Concurrent edits use a
revision check: if another window changed the solution, reopen it before saving
or deleting to avoid overwriting newer work.

### Ownership and privacy

Saved solutions are independent, user-owned records in the same local SQLite
database. Re-ingestion cannot replace them. Hiding a source session, disabling its
adapter, or deleting the original conversation **does not hide or delete saved
copies**. They remain visible in Curated solutions, with hidden/disabled or
missing-source information. Delete saved copies separately when removing
sensitive information.

Curated metadata does not modify source conversations or automatic tags and is
not added to global conversation search, Ask, MCP, usage totals, or session
collections in this increment. Use the dedicated curated search to find it.

The new table is added automatically at startup; no reindex or additional package
is needed. Back up the database to preserve saved solutions and annotations as
well as the original archive.

## Curated solution API

- `POST /api/solutions/preview`: read a source for confirmation. Send `kind`,
  `session_id`, and either `turn_index` for an `answer` (zero-based), or
  `snippet_id` for a `snippet`. The result includes the exact content and a
  `source_sha256` identifying that content and its provenance; it creates no copy.
- `POST /api/solutions`: send the `source` reference, preview `source_sha256`,
  `title`, and optional `notes`, `tags`, `favorite`, `status`, and `prerequisites`.
  Returns `{id, created}` with 201 for a new solution or 200 for an existing copy.
- `GET /api/solutions`: optional `q`, `tag`, `status`, `favorite`, `offset`, and
  `limit` (1–100). Returns `solutions`, `total`, `offset`, `limit`, and `has_more`.
  List entries contain only a short content preview; full content is fetched on
  demand.
- `GET /api/solutions/{id}`: full saved content, editable metadata, provenance,
  `source_status`, `source_hidden`, and the current `revision`.
- `PUT /api/solutions/{id}`: replace editable metadata with the current
  `revision`. Content and provenance cannot be modified.
- `DELETE /api/solutions/{id}?revision=N`: delete only that saved solution.

Review statuses are `needs_review`, `verified`, and `outdated`. Missing sources or
solutions return 404; oversized source content returns 413; invalid input returns
422; stale previews or revisions return 409. Existing archive/MCP endpoints are
unchanged.

## Tips

- Click a snippet's title to jump straight to its source turn — the surrounding
  discussion often explains *why* the command was what it was. The content filter
  is carried into the conversation's evidence search. Use **Copy turn link** to
  return to that location later; see [Jump to evidence](searching.md#jump-to-evidence).
- Combine the content filter with **Commands only** to find, say, every `docker`
  command you've ever been given.
- The library reflects whatever is indexed, so it grows automatically as Mark
  picks up new sessions.
