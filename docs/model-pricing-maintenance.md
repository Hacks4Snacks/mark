# Maintaining model pricing

Mark ships a versioned model catalog in `mark/model_pricing.json`. Runtime cost
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

`audit: false` excludes fallback or intentionally unsupported entries from
tracked LiteLLM comparisons. `litellm_ignore_fields` records a reviewed,
field-specific disagreement where the official provider remains authoritative.

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
