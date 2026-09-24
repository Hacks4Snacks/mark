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
last verified on **September 10, 2026** against the official
[OpenAI](https://developers.openai.com/api/docs/pricing),
[Anthropic](https://platform.claude.com/docs/en/about-claude/pricing),
[Google](https://ai.google.dev/gemini-api/docs/pricing), and
[xAI](https://docs.x.ai/developers/models) pricing pages, plus
[Cursor](https://cursor.com/docs/models) for explicit Composer 2.5 variants.
The registry has **102 canonical entries**, including historical models and
fallbacks; it is not a claim that every model is available to your account.
The calculation is careful
not to over-count long agent sessions:

- **Fresh input**, **cache reads**, and **cache writes** are each priced
  separately. Cache reads are generally far cheaper than fresh input; cache
  writes use the model's published rate where one exists.
- Token-reporting conventions differ by source — the Copilot CLI reports input
  tokens *inclusive* of cache, while Cline-family agents report them *exclusive*.
  Mark normalises both so neither is overcharged.
- Explicit **Composer 2.5** and **Composer 2.5 Fast** IDs use Cursor's published
  rates. Unspecified legacy `composer`, `gpt-oss`, and `llama` IDs retain the
  existing **zero** fallback. That is an estimation convention, not a claim that
  hosted usage or local compute is free.

> All costs are **estimates**. They depend on public list prices and won't
> reflect your specific plan, discounts, or included quota.

The built-in values are standard text API rates. For models with request-level
long-context pricing, Mark uses the base rate because imported session totals do
not preserve each request's prompt size. Batch, flex, fast/priority processing,
regional/data residency, cache-storage, and tool-call charges are excluded unless
an explicitly priced model variant identifies that tariff (Composer 2.5 Fast).
Cursor-specific rates for third-party models, its per-token surcharge, and
subscription usage pools are not modeled; third-party names use direct-provider
standard estimates. Copilot premium requests and AIU remain source-reported
counters, not conversions from this price table.

### September 2026 pricing highlights

All figures are **USD per million tokens** at the standard, shortest-context
text rate. For the full catalog and historical variants, see
[the registry](../mark/model_pricing.json).

| Model | Input | Output | Cached input |
| --- | ---: | ---: | ---: |
| GPT-6 Astra | $10.00 | $50.00 | $1.00 |
| GPT-5.6 Sol | $4.00 | $20.00 | $0.40 |
| GPT-5.6 Terra | $2.00 | $12.00 | $0.20 |
| GPT-5.6 Luna | $0.20 | $1.20 | $0.02 |
| Claude Fable 5.1 / Mythos 5.1 | $10.00 | $50.00 | $0.25 |
| Claude Opus 5 | $5.00 | $25.00 | $0.50 |
| Claude Sonnet 5 | $2.00 | $10.00 | $0.20 |
| Gemini 3.6 / 3.7 / 3.8 Flash | $0.75 | $3.75 | $0.075 |
| Gemini 3.5 Flash-Lite | $0.30 | $2.50 | $0.03 |
| Grok 4.6 | $2.00 | $6.00 | $0.50 |
| Grok 4.5 | $2.00 | $6.00 | $0.30 |
| Composer 2.5 | $0.50 | $2.50 | $0.20 |
| Composer 2.5 Fast | $3.00 | $15.00 | $0.50 |

- **Sonnet 5:** its $2/$10 launch price is now permanent. Anthropic canceled the
  previously scheduled September 1 increase; the old August 31 review gate is removed.
- **GPT-5.6 Sol:** promotional prices are available **at least through November
  21, 2026**. The registry requires another review on that date rather than
  inventing a fixed expiration or future rate.
- **Gemini 3.6–3.8 Flash:** current prices run through **December 31, 2026**.
  Announced January 1, 2027 rates are $1.50 input, $7.50 output, and $0.15 cached
  input. A December 31 review gate records the required update.
- **Mythos** and **Daybreak** entries can require approval. Catalog entries and
  aliases enable cost matching, not model provisioning or account access.

Review dates are maintenance/CI gates, not automatic runtime tariff changes.

Maintainers should follow [Maintaining model pricing](model-pricing-maintenance.md)
for the checked-in registry, scheduled audit, source authority, and review-date
workflow.

## Customising prices

The built-in table covers common Claude, GPT, Gemini, and Grok tiers. To override
it entirely, point `MARK_PRICING_FILE` at a JSON file:

```json
{
  "claude-sonnet-5": [2.0, 10.0, 0.20, 2.50, 4.0],
  "gpt-6-astra":    [10.0, 50.0, 1.0, 12.50],
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

The fourth value is the generic cache-creation rate under a historical
`cache_write_5m` field name. For GPT-5.6 and GPT-6 it stores the published
**30-minute** cache-write tariff; it does not imply five-minute retention.
Anthropic retains distinct five-minute and one-hour write rates. When a provider
publishes no separate cache-write tariff, built-in entries use ordinary input
pricing for reported creation tokens, not an extra surcharge. A missing cache
discount is not treated as free input.

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
Installing this catalog does not rewrite stored costs or rebuild the archive.
An explicit rebuild uses the **current catalog**, not historical effective-date
selection. Provider aliases that now redirect to a different model may therefore
give a different estimate for older usage.

## Per-session cost

Every conversation's detail view shows its own metrics — model, duration, token
counts, and estimated cost — alongside its session id and resume command. Group
spend across a whole effort with [Collections](collections.md#collection-overview).
