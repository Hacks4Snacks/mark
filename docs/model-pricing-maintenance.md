# Maintaining model pricing

Mark ships a versioned model catalog in [the pricing registry](../mark/model_pricing.json). Runtime cost
calculation is fully local and deterministic; Mark never downloads model or
pricing data while starting, indexing, or serving requests.

## Authority order

Use sources in this order when changing the registry:

1. The provider's official pricing and model documentation.
2. Provider API model catalogs, when they are available without account-specific
   assumptions.
3. LiteLLM's model map as a discovery and cross-check source only.

Never copy a LiteLLM difference into the registry without confirming it against
the linked official provider page. Hosting channel, region, service tier, and
data-residency prices can differ from direct standard API rates.

## Registry fields

Top-level metadata controls maintenance policy:

- `revision` identifies one reviewed registry snapshot.
- `verified_at` is the last complete manual review date.
- `warn_after_days` and `fail_after_days` drive the CI freshness gate.
- `providers` records official source and audit URLs, pricing-section boundaries,
   required sentinels, normalized pricing snapshots, LiteLLM provider names, and
   model-family discovery prefixes.

Official source snapshots cover only provider-specific pricing content. The
audit fails closed when an expected section boundary, model identifier, or price
marker disappears, so a client-rendered shell cannot be accepted as a clean
source.

Each model records a canonical matching key, provider, lifecycle `status`,
prices, and optional aliases. Keep deprecated and retired entries so historical
sessions retain the price for the model they actually used.

Use `effective_from`, `effective_until`, and `review_after` for temporary or
scheduled prices. A due `review_after` date fails CI even when the overall
registry is otherwise fresh.

These dates are maintenance metadata, not automatic rate selection by session
timestamp. Keep a `notes` field for approval requirements, redirects, historical
tariffs, and announced replacement prices. Do not infer retirement from model age
or the word "legacy"; check the provider's direct-API lifecycle notices.

`audit: false` excludes fallback or intentionally unsupported entries from
tracked LiteLLM comparisons. `litellm_ignore_fields` records a reviewed,
field-specific disagreement where the official provider remains authoritative.

For xAI, the current `embedded` audit reads the raw
`globalThis.__XAI_PUBLIC_MODELS__` JSON assignment without executing JavaScript
or decoding HTML entities inside the script. It hashes language-model prices,
context thresholds, and aliases, ignores non-pricing metadata and media-only
clusters, and deduplicates identical regional records. The older RSC format is
still supported for existing snapshots. Malformed or incomplete data fails closed.
Cursor's audit covers its own **Cursor Models** pricing section, not subscription
plans or a competing authority for third-party model rates.

## Latest completed review: 2026-09-10

Revision **2026-09-10.1** contains **102 canonical entries** (previously 73),
including historical releases and local fallback keys. Changes include:

- GPT-6 Astra, Daybreak Cyber/aliases, Chat Latest, and the reduced GPT-5.6 family
   rates. Explicit old ChatGPT/Codex snapshots and o-series Pro variants prevent
   broad name matches from assigning a different model's tariff.
- Claude Fable/Mythos 5.1's lower $0.25/MTok cache-read rate and Opus 5. Sonnet 5's
   $2/$10 price is now standard, so its expired introductory review date is removed.
- Gemini 3.6, 3.7, and 3.8 Flash promotional rates and Gemini 3.5 Flash-Lite.
   Retired Gemini 3 Pro and 3.1 Flash-Lite Preview remain separate from active models.
- Grok 4.6, Grok 4.5's $0.30/MTok cached input, multi-agent IDs, and reviewed alias
   destinations. `grok-build-latest` maps to 4.5; Code Fast aliases map to Build 0.1.
- Explicit Composer 2.5 and Fast rates from Cursor. The unspecified legacy
   `composer` fallback remains unchanged and is not a statement of free usage.

Evidence for the manual review:

| Provider | Pricing authority | Model identity / lifecycle evidence |
| --- | --- | --- |
| OpenAI | [Standard pricing](https://developers.openai.com/api/docs/pricing.md) | [Changelog](https://developers.openai.com/api/docs/changelog.md), [deprecations](https://developers.openai.com/api/docs/deprecations.md), [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra.md), [Chat Latest](https://developers.openai.com/api/docs/models/chat-latest.md) |
| Anthropic | [Model pricing](https://platform.claude.com/docs/en/about-claude/pricing.md) | [Deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations.md), [Fable 5.1](https://platform.claude.com/docs/en/models/fable-5-1/overview.md), [Mythos 5.1](https://platform.claude.com/docs/en/models/mythos-5-1/overview.md) |
| Google | [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) | [Models](https://ai.google.dev/gemini-api/docs/models), [deprecations](https://ai.google.dev/gemini-api/docs/deprecations), [changelog](https://ai.google.dev/gemini-api/docs/changelog) |
| xAI | [Models and pricing](https://docs.x.ai/developers/models) | [Grok 4.5 aliases](https://docs.x.ai/developers/models/grok-4.5), [Build 0.1 aliases](https://docs.x.ai/developers/models/grok-build-0.1), [retirement redirects](https://docs.x.ai/developers/migration/may-15-retirement) |
| Cursor | [Models and pricing](https://cursor.com/docs/models) | Composer 2.5 and explicit Fast rows; no third-party hosting-rate overrides |

Upcoming mandatory reviews: **October 23** for scheduled OpenAI retirements,
**November 21** for Sol's promotion, **December 11** for GPT-5/o3 retirements, and
**December 31** for the Gemini Flash promotion. The normal 30/60-day freshness
policy remains unchanged. "At least through" is recorded as a review date, not a
guessed `effective_until`.

Verification: **550 isolated tests passed**, Ruff and editor checks passed, and
independent review passed. The live audit found no official-source drift or
tracked price conflicts. Missing LiteLLM Composer/Cyber entries, omitted cache
fields, historical/experimental candidates, and unresolved rolling Gemini
aliases remain informational—not guessed prices or suppressed errors.
No stored sessions were repriced and no live deployment was rebuilt.

## Local commands

Validate structure, freshness, and mandatory review dates without network
access:

```bash
python scripts/update_model_pricing.py --check
```

Run the live audit and write the same report used by automation:

```bash
python scripts/update_model_pricing.py \
  --audit \
  --report /tmp/model-pricing-audit.md
```

Exit codes are:

- `0`: valid registry, with no actionable source or tracked-price drift.
- `1`: invalid/expired registry or an unavailable upstream source.
- `2`: an official source changed or a tracked LiteLLM price conflicts.

Missing models, omitted LiteLLM price fields, and newly discovered models are
informational. They appear in the report for review, but do not open a pull
request by themselves. An omitted field is reported as unverifiable rather than
treated as agreement.

## Reviewing an update

The weekly `Model Pricing Audit` workflow first runs with read-only repository
permissions. Every run appends its report to the job summary and uploads the
report as an artifact, including clean and failed audits.

A separate write-enabled job runs only for clean or actionable results. It uses
the GitHub CLI to maintain a draft pull request on the
`automation/model-pricing-audit` branch when actionable drift appears. It forces
an existing review pull request back to draft, and closes the pull request and
deletes the branch when the baseline is clean again. Audit failures and unknown
exit codes fail the workflow without receiving write permissions.

For each report:

1. Open the linked official provider page and identify the real model or price
   change.
2. Update canonical models, aliases, lifecycle status, prices, and effective or
   review dates in `mark/model_pricing.json`.
3. Increment `revision` and set `verified_at` to the completed review date.
4. After manually verifying every official page, accept their normalized
   snapshots:

   ```bash
   python scripts/update_model_pricing.py \
     --accept-source-snapshots \
     --verified-at YYYY-MM-DD
   ```

5. Run `--check`, the live `--audit`, focused cost tests, and the full suite.
6. Remove `.pricing-audit/model-pricing-audit.md` from the draft branch before
   merging the actual registry update.

Do not refresh source snapshots merely to clear the workflow. A changed snapshot
is the signal that the official source needs human review.

## Availability semantics

The registry is a known-model catalog, not an account-specific availability
claim. Model availability can vary by account, region, hosting channel, and
product. Unknown model IDs continue to use the configured fallback or a custom
`MARK_PRICING_FILE`; they are not blocked by the catalog.

## Repricing stored sessions

Updated registry prices apply to newly calculated metrics. A full rebuild
reprices unchanged watched-source sessions from their original usage records:

```bash
curl -X POST 'http://127.0.0.1:8765/api/reindex?rebuild=true'
```

Unchanged one-shot imports cannot currently be repriced because Mark does not
retain their original export bytes and complete cache-token breakdowns.
