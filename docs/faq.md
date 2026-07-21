# FAQ & troubleshooting

Quick answers to common questions. If something here doesn't cover it, the
[other docs](README.md) go deeper per feature.

## Privacy & data

**Does anything leave my machine?**
No. Mark runs 100% locally — no telemetry, no accounts, no API keys. The only
network calls are to optional local services *you* run (an Ollama server for
[Ask](ask.md)). Search, embeddings, summaries, and topic tags are all generated
on-device.

**Does Mark modify my original chat history?**
No. Sources are read **read-only**; live databases are read as a consistent
snapshot. Mark only writes its own index under `~/.mark/`.

**Where is my data stored?**
In `~/.mark/` by default (`mark.db` and `uploads/`). Override with
`MARK_DATA_DIR`. See [Managing your archive](managing-your-archive.md#what-lives-where).

**How do I wipe everything and start over?**
Stop Mark and delete the data directory: `rm -rf ~/.mark` (or your
`MARK_DATA_DIR`). Your original chat stores are untouched.

## Setup & install

**`mark` isn't found after install.**
Use the alias `markive`, or run `python -m mark`. With `pipx`, ensure
`pipx ensurepath` has run and your shell is reloaded.

**Do I need the `[semantic]` extra?**
No — Mark works without it using a built-in offline vectorizer. The extra adds a
transformer model for higher-quality "find by meaning" search. See
[Searching → Semantic engine](searching.md#semantic-engine).

**Port 8765 is already in use.**
Set a different port: `MARK_PORT=9000 mark`.

## Indexing & syncing

**The first launch is slow / pegs my CPU.**
First-time indexing embeds your whole history. The transformer backend caps
itself at about a quarter of your cores (max 4) by default so it stays in the
background — raise it with `MARK_EMBED_THREADS` (or `0` for all cores) for maximum
speed. Search works on whatever is indexed so far and fills in as it goes.

**New sessions aren't showing up.**
Mark auto-syncs while running, but you can force it with the **⟳** button. If you
disabled syncing (`MARK_AUTO_SYNC=0`), Mark still scans once at startup, but ⟳
is the only trigger after that and failed scans are not retried automatically.
Confirm the source is enabled and its roots exist — see
[Sources & syncing](sources.md).

**A source isn't detected.**
Check that its path matches what Mark probes (different OSes and editor channels
use different paths). Override the roots in `~/.mark/sources.toml` or via
`MARK_SOURCE_<NAME>_ROOTS`. For Cline forks Mark doesn't recognise, add a label
override.

**I disabled a source but its sessions are still there.**
That's intentional — disabling stops scanning but never deletes indexed data. To
remove sessions, delete them explicitly. See
[Managing your archive](managing-your-archive.md#delete-a-session).

## Search

**Semantic search returns weak results.**
You may be on the built-in fallback vectorizer. Install the transformer backend
(`pip install 'markive[semantic]'` or `pip install -r requirements-optional.txt`)
and check the sidebar status card for the active engine.

**I want exact-string matching.**
Switch the mode toggle to **Keyword** for FTS5/BM25 exact-term search. See
[Searching & filtering](searching.md#search-modes).

## Cost numbers

**The costs look wrong.**
They're **estimates** from public list prices and won't reflect your plan,
discounts, or included quota. VS Code sessions (no token logs) use a text-based
estimate, flagged as such. Override prices with `MARK_PRICING_FILE` — see
[Usage & cost](usage-and-cost.md#customising-prices).

## Ask

**I don't see the ✦ Ask button or view.**
Ask is **disabled by default** while it's being refined. Enable it by setting
`MARK_ENABLE_ASK=1` before starting Mark; the button, view, and its API routes
only appear when the flag is on.

**The Ask view says no local LLM was detected.**
Duration analysis still works directly from stored metrics. Narrative lookups
and summaries need a local [Ollama](https://ollama.com) server. Run `ollama
serve` and pull a model (`ollama pull llama3.2`). Point Mark elsewhere with
`MARK_OLLAMA_URL` / `MARK_OLLAMA_MODEL` if needed. See [Ask your history](ask.md).

## MCP

**My agent can't see my history.**
Install the extra (`pip install 'markive[mcp]'`), register the `mark-mcp` stdio
server, and make sure it can read the same `~/.mark/mark.db` (set `MARK_DATA_DIR`
in the server's environment if your data is elsewhere). See [MCP server](mcp.md).
