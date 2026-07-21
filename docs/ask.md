# Ask your history

The **✦ Ask** view lets you ask questions about your own past conversations in
plain English and get a **cited** answer synthesised from the most relevant
sessions. It's a retrieval-augmented generation (RAG) feature that runs entirely
on your machine.

> **Disabled by default, optional, and fully local.** Ask is still being refined,
> so it ships **off** — turn it on by setting `MARK_ENABLE_ASK=1` before starting
> Mark. While it's disabled the **✦ Ask** button and view never appear and its API
> routes stay unmounted. Structured duration analysis works directly from stored
> metrics. Narrative lookups and summaries use a **local
> [Ollama](https://ollama.com) server** — no cloud, no API keys. When Ollama isn't
> running, the view remains available for analytics and shows narrative setup
> guidance. Every other feature keeps working regardless.

By default Ollama runs on loopback, so narrative evidence stays on this machine.
If `MARK_OLLAMA_URL` points to another host, Mark sends the question and selected
evidence to that configured endpoint.

## Setup

1. **Enable the feature.** Ask is disabled by default; turn it on by setting
   `MARK_ENABLE_ASK=1` before starting Mark:

   ```bash
   export MARK_ENABLE_ASK=1
   ```

2. For narrative questions, install [Ollama](https://ollama.com).
3. Pull any chat model and start the server:

   ```bash
   ollama pull llama3.2     # any installed model works
   ollama serve
   ```

4. Open Mark → **✦ Ask**. Duration analysis works immediately; Ollama enables
   narrative lookups and summaries.

## How it works

When you ask a question, Mark:

1. **Plans** the request deterministically. Evidence lookups use matching turns;
   summaries use stored session summaries and metadata; duration questions rank
   recorded session duration (then turn count). The Ask view shows the selected
   mode and any repository or date scope before the evidence list. Structured
   summary and duration modes compare the strongest 12 ranked sessions by
   default so small local models are not overwhelmed.
   Explicit requests such as *"find conversations about certificates"* return
   cited matching conversations directly instead of asking the model to
   reinterpret a search result.
2. **Retrieves** the most relevant *passages* from your history using the same
   hybrid (keyword + semantic) search that powers the rest of the app — pinning
   the exact turns that answer your question rather than whole sessions. When a
   chunk matches, Mark restores both the user and assistant sides of that turn.
   Exact
   repository names and deterministic date phrases such as *today*, *yesterday*,
   *past week*, *last 30 days*, and *since 2026-01-01* are converted into
   pre-ranking scope. *Latest* and *most recent* add recency-first ordering.
3. **Reranks** those passages with a local cross-encoder (when the `semantic`
   extra is installed), rejects passages below its configured relevance floor,
   and lets the most on-point excerpts win. Without a reranker, Ask falls back
   to content-only keyword passages rather than treating unvalidated semantic
   neighbors or metadata-only matches as evidence. This favors precision over
   synonym recall on minimal installations.
4. **Packs** the top passages — each widened with a little surrounding context
   and numbered as a source — into the model's context window, sized to the
   model you're running instead of a fixed per-source slice. Mark admits one
   passage per session before adding second passages, and skips oversized blocks
   when smaller relevant evidence can still fit. Several passages from one
   session share its citation number. Evidence is serialized into delimited JSON
   records so archived instructions remain data rather than prompt structure.
5. Returns structured duration analysis directly from recorded metrics, with
   citations. For evidence lookups and summaries, the **local** model synthesises
   a cited answer (e.g. `[1]`, `[2]`) and **streams it back token by token**.

Expand any context item to inspect the exact bounded passage excerpts Mark supplied
to the model, including turn and date metadata, then open the full conversation
when more context is needed. Context is listed only after its passage is accepted
into the prompt budget; items cited by the generated answer are highlighted.

The model is instructed to answer **only** from your excerpts and to say plainly
when the answer isn't in your history — rather than guessing. Your archive never
leaves your machine.

## Model selection

If you don't specify a model, Mark auto-picks a small, fast, general-purpose one
from what you have installed (it prefers `llama3.2`, then `llama3.1`, `qwen2.5`,
`mistral`, `gemma`, `phi`, …). Override it explicitly:

| Variable            | Default                  | Purpose                       |
|---------------------|--------------------------|-------------------------------|
| `MARK_OLLAMA_MODEL` | *(auto-pick)*            | Force a specific Ollama model |
| `MARK_OLLAMA_URL`   | `http://localhost:11434` | Ollama endpoint               |

```bash
export MARK_OLLAMA_MODEL=qwen2.5
export MARK_OLLAMA_URL=http://localhost:11434
mark
```

Use a non-loopback endpoint only when you trust that server with the retrieved
archive excerpts.

## Ask a single collection

You can scope a question to **one [collection](collections.md)** instead of your
whole history. That focuses the answer on a single effort — *"what did I conclude
about retries in the auth refactor?"* — and avoids pulling in unrelated sessions.

## Tips

- **Be specific.** "How did I configure the retry backoff for the Kafka consumer"
  beats "kafka stuff" — the retrieval step rewards detail.
- **Follow the citations.** Each bracket number maps to a real session; open it to
  read the full context behind the answer.
- **No answer is a valid answer.** If your history genuinely doesn't cover the
  question, the model will say so rather than fabricate.
