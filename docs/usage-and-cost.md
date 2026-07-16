# Usage & cost analytics

The **Usage** view turns your archive into a spend-and-effort dashboard: how much
your AI coding sessions cost, how long they ran, how many tokens they burned, and
where that effort went — all computed locally from data already on disk.

Open it from the **Usage** button in the top bar (or the `#/usage` deep link).

## What you get

The dashboard rolls up totals and breaks them down several ways:

- **Totals** — sessions, estimated USD cost, premium requests, input/output
  tokens, total duration, and AIU (AI units).
- **By day** — a timeline of sessions, cost, and premium requests.
- **By model** — which models cost you the most (top 12).
- **By repository** — where the spend landed (top 12).
- **By source** — VS Code vs Copilot CLI vs agents vs your notes.

Hidden sessions are excluded from every total, so the numbers reflect what you
actually care about. See [Managing your archive](managing-your-archive.md#hide-a-session).

## Where the numbers come from

### Real metrics (Copilot CLI)

Every Copilot CLI session is enriched from its per-session `events.jsonl` with
**real** metrics — model name, wall-clock duration, input/output/cache token
counts, premium requests, and AIU. These are exact, not estimated.

### Estimated metrics (VS Code & others)

Sources that don't log token usage (such as VS Code chat) fall back to a
**text-based estimate**: tokens are approximated from message length (~4 chars
per token) and duration from the first/last turn timestamps. These sessions are
**flagged as estimated** so you can tell them apart from measured ones.

## How cost is computed

Mark prices token counts against a built-in **public list-price table** (USD per
1M tokens), matched by the most specific model-name substring. The table was
last verified on **July 14, 2026** against the official
[OpenAI](https://developers.openai.com/api/docs/pricing),
[Anthropic](https://platform.claude.com/docs/en/docs/about-claude/pricing),
[Google](https://ai.google.dev/gemini-api/docs/pricing), and
[xAI](https://docs.x.ai/docs/models) pricing pages. The calculation is careful
not to over-count long agent sessions:

- **Fresh input**, **cache reads**, and **cache writes** are each priced
  separately. Cache reads are generally far cheaper than fresh input; cache
  writes use the model's published rate where one exists.
- Token-reporting conventions differ by source — the Copilot CLI reports input
  tokens *inclusive* of cache, while Cline-family agents report them *exclusive*.
  Mark normalises both so neither is overcharged.
- Models that aren't billed per token (Cursor Composer, local/self-hosted models
  like `gpt-oss`, `llama`) price to **zero** rather than inheriting a default
  tier.

> All costs are **estimates**. They depend on public list prices and won't
> reflect your specific plan, discounts, or included quota.

The built-in values are standard text API rates. For models with request-level
long-context pricing, Mark uses the base rate because imported session totals do
not preserve each request's prompt size. Batch, flex, priority, regional/data
residency, cache-storage, and tool-call charges are also excluded. Claude Sonnet
5 currently uses its introductory rate, which Anthropic says ends after
**August 31, 2026**.

Maintainers should follow [Maintaining model pricing](model-pricing-maintenance.md)
for the checked-in registry, scheduled audit, source authority, and review-date
workflow.

## Customising prices

The built-in table covers common Claude, GPT, Gemini, and Grok tiers. To override
it entirely, point `MARK_PRICING_FILE` at a JSON file:

```json
{
  "claude-sonnet-5": [2.0, 10.0, 0.20, 2.50, 4.0],
  "gpt-5.5":         [5.0, 30.0, 0.50, 5.0],
  "my-local-model": [0.0, 0.0, 0.0],
  "_default":      [3.0, 15.0, 0.30]
}
```

Each value is `[input, output, cached_input]` or
`[input, output, cached_input, cache_write_5m, cache_write_1h]` in **USD per 1
million tokens**. The fourth and fifth values are optional; omitting them uses
1.25 and 2 times input, respectively. Keys are matched by normalised substring
against the model name, with the longest match winning, so `gpt-5.5` takes
precedence over `gpt-5`. `_default` is the fallback for anything unmatched.

```bash
export MARK_PRICING_FILE=~/.mark/pricing.json
mark
```

The file is re-read when it changes. If it's missing or malformed, Mark logs a
warning and falls back to the built-in table — a typo never silently produces
wrong-but-plausible costs.

Costs are stored with each indexed session. After changing a custom pricing file
or installing a release with new built-in prices, request one full rebuild to
reprice unchanged **watched-source** sessions from their original metrics:

```bash
curl -X POST 'http://127.0.0.1:8765/api/reindex?rebuild=true'
```

The regular re-scan remains incremental and intentionally skips unchanged
sessions. One-shot ChatGPT, Grok, and similar imports are not retained as source
files by Mark, so unchanged imported sessions cannot currently be repriced.

## Per-session cost

Every conversation's detail view shows its own metrics — model, duration, token
counts, and estimated cost — alongside its session id and resume command. Group
spend across a whole effort with [Collections](collections.md#collection-overview).
