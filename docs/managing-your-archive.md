# Managing your archive

Beyond the chats Mark indexes automatically, you can add your own content and
curate what's in the archive. This page covers notes, file uploads, importing
exports, tags, hiding, deleting, and exporting.

## Add your own notes & files

Click **Add** in the top bar to drop content into your archive. It becomes
searchable alongside everything else.

### Notes

Write a free-form note with an optional title. Good for capturing a decision, a
snippet, or a lesson you want to find later. Notes are stored as a session in the
same local database.

### File uploads

Upload a file and Mark extracts its text so it's searchable. Recognised text
formats are read directly (`.txt`, `.md`, source code, `.json`, `.yaml`, `.toml`,
and many more). **PDF** extraction works if the optional `pdf` extra is
installed:

```bash
pip install 'markive[pdf]'
```

| Variable                | Default  | Purpose               |
|-------------------------|----------|-----------------------|
| `MARK_MAX_UPLOAD_BYTES` | `25 MiB` | Largest file accepted |
| `MARK_MAX_EXTRACTED_TEXT_CHARS` | `5000000` | Maximum extracted text retained |
| `MARK_MAX_PDF_PAGES` | `1000` | Maximum pages inspected per PDF |
| `MARK_PDF_EXTRACT_TIMEOUT` | `30` | Maximum PDF extraction seconds |
| `MARK_PDF_EXTRACT_MEMORY_BYTES` | `512 MiB` | Isolated PDF extraction memory ceiling |

Uploaded files are stored under `~/.mark/uploads/`.

### Importing an export

If you upload a **recognised export** — for example a ChatGPT
`conversations.json` — Mark imports it as **many sessions** instead of a single
document, so each conversation is searchable on its own. Anything unrecognised is
stored as one searchable document.

## Topics & tags

Every session gets locally generated **topic tags** at ingest (no LLM, no API
keys). You can curate them on any conversation:

- **Add a tag** — type a topic; it's normalised (lower-cased, trimmed, max 40
  chars).
- **Remove a tag** — drop one that doesn't fit.

Tags power the **Topics** facet in the [sidebar](searching.md#the-sidebar-facets)
and the rules behind [collections](collections.md).

## Hide a session

Hiding removes a session from listings and from every aggregate (Usage totals,
collection overviews) **without deleting it**. Use it to declutter noisy or
irrelevant sessions.

- Toggle **Show hidden only** in the sidebar to review what you've hidden.
- Unhide at any time to restore it everywhere.

Hiding is fully reversible and changes no underlying data.

## Delete a session

Deleting is **permanent**. Mark removes the session and writes a *tombstone* so a
later re-scan of the original source can't silently restore it. Reach for **hide**
unless you truly want the data gone.

## Attachments (agent-created files)

When an agent created or edited a file during a session, Mark records it and — up
to a size cap — snapshots its content so you can view and **download** it later,
even if the original file has since changed or been removed.

| Variable                    | Default   | Purpose                                                              |
|-----------------------------|-----------|----------------------------------------------------------------------|
| `MARK_MAX_ATTACHMENT_BYTES` | `512 KiB` | Largest agent file snapshotted; larger files record path + size only |

Preview and download use only the immutable snapshot captured by Mark. Content
is unavailable when it was too large to capture or fails snapshot verification;
the original live path is never served.

## Export a conversation to Markdown

Any conversation can be exported as a clean **Markdown** file — every turn, with
tool calls noted — via the detail view (or `GET /api/sessions/<id>/export.md`).
This is the same rendering the [MCP server](mcp.md) returns to an agent, so it's
ideal for sharing a solution or pasting into a doc.

The browser renders conversations incrementally: it starts with 20 turns and
offers **Load more** for the next page. An unusually large individual turn or
uploaded document stays as a compact placeholder until explicitly loaded.
Attachment bodies likewise load only when expanded. This keeps opening and
scrolling large records responsive; Markdown export still includes the complete
conversation or document without pagination.

Long file, link, and attachment lists are also paged. The detail sidebar shows
the loaded and total counts and keeps the remaining metadata reachable through
**Load more** without putting it in the initial response.

## What lives where

| Path                   | Contents                                                              |
|------------------------|-----------------------------------------------------------------------|
| `~/.mark/mark.db`      | The index: sessions, turns, files, tags, cost, embeddings, tombstones |
| `~/.mark/uploads/`     | Files you uploaded                                                    |
| `~/.mark/sources.toml` | Optional source overrides                                             |

Everything is local. To start fresh, stop Mark and delete `~/.mark/` (or the
directory set by `MARK_DATA_DIR`).
