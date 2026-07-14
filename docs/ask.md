# Ask your history

The **✦ Ask** view lets you ask questions about your own past conversations in
plain English and get a **cited** answer synthesised from the most relevant
sessions. It's a retrieval-augmented generation (RAG) feature that runs entirely
on your machine.

> **Disabled by default, optional, and fully local.** Ask is still being refined,
> so it ships **off** — turn it on by setting `MARK_ENABLE_ASK=1` before starting
> Mark. While it's disabled the **✦ Ask** button and view never appear and its API
> routes stay unmounted. It's also the one feature that needs a model: Mark uses a
> **local [Ollama](https://ollama.com) server** — no cloud, no API keys. When Ask
> is enabled but Ollama isn't running, the view simply shows setup hints. Every
> other feature keeps working regardless.

## Setup

1. **Enable the feature.** Ask is disabled by default; turn it on by setting
   `MARK_ENABLE_ASK=1` before starting Mark:

   ```bash
   export MARK_ENABLE_ASK=1
   ```

2. Install [Ollama](https://ollama.com).
3. Pull any chat model and start the server:

   ```bash
   ollama pull llama3.2     # any installed model works
   ollama serve
   ```

4. Open Mark → **✦ Ask**. Once Ollama is reachable, the view is ready.

## How it works

When you ask a question, Mark:

1. **Retrieves** the most relevant *passages* from your history using the same
   hybrid (keyword + semantic) search that powers the rest of the app — pinning
   the exact turns that answer your question rather than whole sessions. Exact
   repository names and deterministic date phrases such as *today*, *yesterday*,
   *past week*, *last 30 days*, and *since 2026-01-01* are converted into
   pre-ranking scope. *Latest* and *most recent* add recency-first ordering.
2. **Reranks** those passages with a local cross-encoder (when the `semantic`
   extra is installed) so the most on-point excerpts win; otherwise it keeps the
   hybrid-search order.
3. **Packs** the top passages — each widened with a little surrounding context
   and numbered as a source — into the model's context window, sized to the
   model you're running instead of a fixed per-source slice. Several passages
   from one session share its citation number.
4. Has the **local** model synthesise an answer that cites the sources it used
   (e.g. `[1]`, `[2]`), and **streams it back token by token**.

Expand any citation to inspect the exact bounded passage excerpts Mark supplied
to the model, including turn and date metadata, then open the full conversation
when more context is needed. Sources are listed only after their passage is
accepted into the prompt budget.

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
