# mark — Data Quality Audit

**Scope:** all sources · **Audited:** 2026-06-26 (read-only) · **Fixes applied:** 2026-06-26
**Database:** `/Users/m/.mark/mark.db` (opened read-only via `file:...?mode=ro`) · **SQLite:** 3.51.0

> This document tracks **open** data-quality findings. Findings are removed once their
> fix lands. Of the 9 findings from the initial audit, **8 have been remediated**; the
> data-overview, coverage, and appendix sections below remain the original 2026-06-26
> audit snapshot (pre-fix) for reference.

---

## 1. Executive summary

mark's ingest is **trustworthy**. Content is well-formed (no placeholder titles, no blank
turns, no duplicate-hash sessions, no orphaned child rows), referential counts are
internally consistent (`turn_count` matches `turns` exactly; `search_index` exactly mirrors
`chunks`), financial accuracy is excellent where real metrics exist (sampled CLI/Cursor
costs reproduce to the cent, cache-aware), and on-disk coverage is effectively complete for
every source.

The high-impact Cline adapter bug has been **fixed**: the adapter now reads Cline's real
on-disk layout (`ui_messages.json` token/cost, task-dir / UI-log timestamps, env-header
workspace), restoring timestamps, repository attribution, and real token/cost metrics for
the whole `cline` source. The pricing-table `*-mini` miss, Zoo Code timestamp inversion,
empty tool-only CLI turns, unlisted-model fallthroughs, the chunk-cap user-priority, and
U+FFFD noise are likewise fixed.

**One finding remains open (F4):** Zoo Code does not record its model per task on disk
(every task's `apiConfigName` is `"default"`, and there is no model field in
`task_metadata.json` / `ui_messages.json`), so its `model` stays NULL and its computed cost
falls back to the sonnet price tier. This is a source-data limitation, not an adapter
defect — see below.

### Scorecard

| Dimension | Rating | One-line justification |
|---|---|---|
| **Cleanliness** | **Pass** | No placeholder titles, blank turns, duplicate `content_hash`, or FK orphans; U+FFFD/control-char noise is now stripped on write. |
| **Completeness** | **Pass** | The Cline source now lands real timestamps, repo, and token/cost; CLI tool-only turns are no longer rendered empty. (Residual: Zoo Code `model` — F4 — is absent from the source itself.) |
| **Consistency** | **Warn** | Integrity counts perfect and timestamp ordering is now guaranteed; the only remaining issue is F4 — Zoo Code's NULL model defaulting to the sonnet price tier. |
| **Accuracy** | **Pass** | Sampled CLI/Cursor/VS Code sessions match disk exactly (title, turns, model, timestamps, cache-aware cost to the cent). |

**Overall verdict:** Healthy, accurate, fully-covered index. After remediation, only the
Zoo Code model attribution (F4) is open, and it is bounded by what the source stores.

---

## 2. Data overview *(original 2026-06-26 audit snapshot, pre-fix)*

### Rows per table

| Table | Rows |
|---|---|
| sessions | 2,538 |
| turns | 14,771 |
| documents | 345 |
| session_files | 10,271 |
| session_refs | 80,346 |
| code_blocks | 8,631 |
| tags | 15,050 |
| chunks | 26,571 |
| embeddings | 2,816 → 3,072 (advancing live during audit) |
| search_index (FTS5) | 26,571 |
| collections | 0 |
| collection_members | 0 |
| meta | 0 |

### Sessions, estimation, and cost per source

| source | sessions | turns | % tokens_estimated | NULL model | NULL repo | total est_cost_usd |
|---|---|---|---|---|---|---|
| automation | 1,719 | 1,925 | 3.5% | 60 | 98.4% | $797.56 |
| cursor | 381 | 5,296 | 25.5% | 43.6% | 60.6% | $578.17 |
| cli | 216 | 1,377 | 13.9% | 25 | 95.4% | $2,279.70 |
| cline | 144 | 4,225 | 100% (pre-fix) | 8 | 100% (pre-fix) | $226.66 (pre-fix, all estimated) |
| zoocode | 41 | 1,867 | 7.3% | 100% | 0% | $548.08 |
| vscode | 37 | 81 | 100% (by design) | 100% (by design) | 0% | $0.21 |
| chatgpt | 0 | — | — | — | — | — |

> The `cline` estimation/NULL-repo figures above are pre-fix. After the F1 fix, re-running
> ingest populates real `created_at`/`updated_at`, `repository`, and measured token/cost for
> `cline` (verified on disk — e.g. `cline-1752350725043` now resolves to `claude-sonnet-4`,
> repo `HomeMaintenance`, 19,595/314 tokens, $0.0635, `tokens_estimated=0`).

No unknown `source` values exist. `chatgpt` is not present (no export imported) — N/A.

---

## 3. Coverage (on-disk source of truth vs indexed) *(2026-06-26 snapshot)*

| source | on-disk (parseable) | indexed | coverage | note |
|---|---|---|---|---|
| vscode | 37 (of 54; 17 turnless) | 37 | **100%** | 17 skipped files genuinely have no user turns. |
| cursor | 382 non-empty composers (of 688; 306 empty) | 381 | **99.7%** | 306 empty composers correctly skipped. |
| cline | 144 tasks with `api_conversation_history.json` (of 146 dirs) | 144 | **100%** | 2 dirs lack the conversation file → unparseable. |
| zoocode | 41 tasks | 41 | **100%** | — |
| cli + automation | 1,933 store sessions with turns | 1,935 | **100%** | `events.jsonl` reconstruction recovers turnless store rows. No loss. |
| chatgpt | no export on disk | 0 | N/A | No live store. |

**No silent ingest loss was found in any source.**

---

## 4. Open findings

---

### F4 — Zoo Code has no per-task model → cost defaults to the sonnet tier — **Medium** · Consistency

**Affected scope:** 41 / 41 (100%) `zoocode` sessions have NULL `model`. When a session's
`history_item.json.totalCost` is `0`, the cost is computed with `price_for(None)` → the
`_default` (sonnet) tier, which may over/under-state cost for non-sonnet models.

**Example ids:** `zoocode-019e98e8-b44b-74ef-b1eb-7382dbe4a6fa`, `zoocode-019ef660-2a70-777c-8eb7-1024a48e8641`.

**Root cause — source data, not adapter logic:** Zoo Code does not persist the model id per
task. Confirmed on disk across all 41 tasks:
- `task_metadata.json` is **absent** for Zoo Code (so `_cline_model`'s `model_usage` path finds nothing).
- `history_item.json.apiConfigName` is `"default"` for **all 41** tasks (a profile name, not a model id); `apiProtocol` is only `"openai"`.
- `ui_messages.json` `api_req_started` payloads carry `tokensIn/Out/cache/cost` but **no** model/`apiModelId` field.

```
# all 41 zoocode tasks:
apiConfigName distribution: {'default': 41}
api_req_started keys: ['apiProtocol','tokensIn','tokensOut','cacheWrites','cacheReads','cost']   # no model
```
```sql
SELECT source, SUM(model IS NULL) FROM sessions WHERE source='zoocode';  -- 41
```

**Status / partial mitigation already in place:** `mark/sources/cline.py` `_cline_model`
now falls back, in order, to `task_metadata.model_usage` → `history_item.apiConfigName`
(when not `"default"`) → a `model`/`apiModelId` field in `api_req_started`. This resolves
the model for forks that *do* record one, but cannot for the current Zoo Code data, where
none of those sources contain it.

**Remediation (to fully close):** read Zoo Code's globally-configured model from its
extension state (`globalStorage/zoocodeorganization.zoo-code/...` settings / `state.vscdb`
`ItemTable`) and attribute it to tasks by time window. This is approximate (global, not
per-task) and a larger change; until then the model remains NULL and cost defaults to the
sonnet tier for Zoo Code sessions without a recorded `totalCost`.

---

## 5. Filesystem validation (what was sampled, and whether it matched) *(2026-06-26 snapshot)*

| id | source | check | result |
|---|---|---|---|
| `aa114208-...` | vscode | requests/title/timestamps vs `chatSessions/*.json` | **Exact** — 19 requests = `turn_count` 19; dates and title match. |
| `cf0d18b5-...` | cli | model + cache-aware cost vs `events.jsonl` shutdown `modelMetrics` | **Exact** — opus; recomputed cost **$237.1155 = stored**. |
| `cursor-760d7f14-...` | cursor | cumulative-input handling + cost vs bubbles | **Exact** — recomputed **$17.3269 = stored**; no double-count. |
| `cline-1752350725043` | cline | post-fix re-parse vs task dir | **Now exact** — `created/updated` 2025-07-12 (from dir name + UI ts), repo `HomeMaintenance`, model `claude-sonnet-4`, 19,595/314 tokens, $0.0635, measured. |
| `zoocode-019e98e8-...` | zoocode | timestamp ordering + model | Ordering now guaranteed (`updated ≥ created`); model still NULL (F4). |

---

## 6. Open recommendations

1. **Close F4** — recover Zoo Code's model from its extension state and attribute by time
   window so cost stops defaulting to the sonnet tier; or accept it as a known source
   limitation. All other audit findings (F1–F3, F5–F9) have been remediated in code.
2. **Re-ingest to materialize the fixes** — the fixes change how sources are parsed but do
   not rewrite existing rows; run a rebuild (`ingest_all(rebuild=True)`) so the corrected
   Cline timestamps/metrics/repo and the new chunking/pricing take effect across history.

---

## 7. Appendix — commands used in the audit (copy-pasteable)

```bash
DB="file:$HOME/.mark/mark.db?mode=ro"

# --- Overview ---
sqlite3 "$DB" "SELECT COUNT(*) FROM sessions;"   # repeat per table
sqlite3 -column -header "$DB" "SELECT source, COUNT(*) sessions, SUM(turn_count) turns FROM sessions GROUP BY source ORDER BY sessions DESC;"
sqlite3 -column -header "$DB" "SELECT source, COUNT(*) total, SUM(tokens_estimated) est, ROUND(100.0*SUM(tokens_estimated)/COUNT(*),1) pct FROM sessions GROUP BY source;"

# --- Embeddings / search parity ---
sqlite3 -column -header "$DB" "SELECT s.source, COUNT(*) FROM chunks c JOIN sessions s ON s.id=c.session_id LEFT JOIN embeddings e ON e.chunk_id=c.id WHERE e.chunk_id IS NULL AND s.source!='automation' GROUP BY s.source;"
sqlite3 "$DB" "SELECT (SELECT COUNT(*) FROM chunks), (SELECT COUNT(*) FROM search_index);"

# --- Cleanliness ---
sqlite3 "$DB" "SELECT COUNT(*) FROM turns WHERE COALESCE(TRIM(user_message),'')='' AND COALESCE(TRIM(assistant_response),'')='';"
sqlite3 "$DB" "SELECT COUNT(*) FROM turns WHERE user_message LIKE '%'||char(65533)||'%' OR assistant_response LIKE '%'||char(65533)||'%';"
sqlite3 "$DB" "SELECT COUNT(*),SUM(c) FROM (SELECT content_hash,COUNT(*) c FROM sessions WHERE content_hash IS NOT NULL GROUP BY content_hash HAVING c>1);"
for t in turns chunks session_files code_blocks session_refs embeddings; do sqlite3 "$DB" "SELECT '$t', COUNT(*) FROM $t x LEFT JOIN sessions s ON s.id=x.session_id WHERE s.id IS NULL;"; done

# --- Completeness / Consistency ---
sqlite3 -column -header "$DB" "SELECT source, SUM(model IS NULL), SUM(input_tokens IS NULL), SUM(est_cost_usd IS NULL) FROM sessions GROUP BY source;"
sqlite3 -column -header "$DB" "SELECT source, SUM(created_at IS NULL), SUM(updated_at IS NULL) FROM sessions GROUP BY source;"
sqlite3 -column -header "$DB" "SELECT source, COUNT(*) FROM (SELECT s.id FROM sessions s LEFT JOIN turns t ON t.session_id=s.id GROUP BY s.id HAVING s.turn_count<>COUNT(t.id));"
sqlite3 -column -header "$DB" "SELECT source, COUNT(*) FROM sessions WHERE updated_at<created_at GROUP BY source;"
sqlite3 -column -header "$DB" "SELECT model, source, COUNT(*) n, SUM(tokens_estimated) est FROM sessions WHERE model IS NOT NULL GROUP BY model, source ORDER BY n DESC;"

# --- Coverage (filesystem) ---
ls "$HOME/Library/Application Support/Code/User/workspaceStorage"/*/chatSessions/*.json | wc -l
ls -d "$HOME/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks"/*/ | wc -l
sqlite3 "file:$HOME/.copilot/session-store.db?mode=ro" "SELECT COUNT(DISTINCT session_id) FROM turns;"
sqlite3 "file:$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb?mode=ro" "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE 'composerData:%';"
```
