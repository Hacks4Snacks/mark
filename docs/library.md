# Snippet & command library

The **Library** is a fast index of every **code block** Mark extracted from your
conversations — across all sources — so you can find that one command or snippet
without remembering which chat it came from.

Open it from the **Library** button in the top bar (or the `#/library` deep link).

## What's in it

During ingest, Mark pulls every fenced code block out of your conversations and
stores it with its language and a link back to the source session. The library
lets you browse and filter them all in one place.

Each snippet card shows:

- The **language** tag.
- The **source session** title (with its source icon and repository), which opens
  the full conversation in one click.
- A **copy** button to grab the snippet to your clipboard.

## Filtering

- **Filter by content** — free-text match against the snippet body (e.g. find
  every block mentioning `kubectl` or `JWT`).
- **Filter by language** — a dropdown of every language found, with counts.
- **Commands only** — a toggle that narrows to runnable shell snippets:
  `bash`, `sh`, `shell`, `zsh`, `console`, `powershell`, `ps1`, and similar.

The **Commands only** view is the quickest way to recover *"that one CLI
incantation I ran three weeks ago."*

## Tips

- Click a snippet's title to jump straight to the conversation it came from — the
  surrounding discussion often explains *why* the command was what it was.
- Combine the content filter with **Commands only** to find, say, every `docker`
  command you've ever been given.
- The library reflects whatever is indexed, so it grows automatically as Mark
  picks up new sessions.
