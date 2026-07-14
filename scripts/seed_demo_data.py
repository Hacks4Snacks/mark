#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Default to a dedicated demo directory BEFORE importing mark, so config picks it
# up and we never clobber the user's real archive. An explicit MARK_DATA_DIR
# (e.g. the user already exported one) is respected.
os.environ.setdefault("MARK_DATA_DIR", str(Path.home() / ".mark-demo"))

# Make the repo importable when run as `python scripts/seed_demo_data.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mark import collections as mark_collections
from mark import config, db, embeddings, ingest, persist
from mark.sources.base import (
    FENCE_RE,
    compute_cost,
    estimate_tokens,
)

NOW = datetime.now(timezone.utc)


def T(user, assistant, *, thinking=None, tools=(), files=(), urls=()):
    """Build one turn. Code blocks are auto-extracted from the assistant text."""
    return {
        "user": user.strip(),
        "assistant": assistant.strip(),
        "thinking": thinking.strip() if thinking else None,
        "tools": list(tools),
        "files": list(files),
        "urls": list(urls),
    }


# --------------------------------------------------------------------------- #
# Demo conversations. Neutral, fictional repos/paths — no real private content.
# --------------------------------------------------------------------------- #
def _specs():
    return [
        # ---- Copilot CLI (real metrics) -------------------------------------
        {
            "source": "cli",
            "repo": "payments-api",
            "repo_path": "/home/dev/projects/payments-api",
            "model": "claude-sonnet-4.5",
            "day": 1,
            "dur": 612,
            "premium": 6,
            "aiu": 0.41,
            "inp": 92000,
            "cread": 71000,
            "cwrite": 8200,
            "outp": 4300,
            "title": "Fix intermittent 504s on /checkout under load",
            "turns": [
                T(
                    "We're seeing intermittent 504 Gateway Timeouts on POST /checkout "
                    "during traffic spikes. Help me find the root cause.",
                    "Let's start by correlating the 504s with backend latency. The "
                    "gateway times out at 30s, so something downstream is blocking. "
                    "First, let's check the connection pool — exhaustion there causes "
                    "requests to queue until the gateway gives up.\n\n"
                    "```bash\nkubectl -n payments logs deploy/checkout --since=15m "
                    "| grep -i 'pool\\|timeout' | tail -50\n```",
                    thinking="504 at the gateway = upstream didn't respond in time. "
                    "Usual suspects: DB connection pool exhaustion, a slow external "
                    "call without a timeout, or thread starvation. Pool exhaustion "
                    "best matches 'only under load'.",
                    tools=["run_in_terminal", "read_file"],
                    files=["src/checkout/handler.go", "src/db/pool.go"],
                ),
                T(
                    "Logs show 'acquire connection timeout' from the pool right before "
                    "each 504.",
                    "That confirms pool exhaustion. The pool is capped at 10 but the "
                    "checkout path holds a connection across a slow external fraud "
                    "check. Move the fraud call outside the transaction and raise the "
                    "pool ceiling to match peak concurrency:\n\n"
                    "```go\n// db/pool.go\ncfg.MaxOpenConns = 40\ncfg.MaxIdleConns = 20\n"
                    "cfg.ConnMaxLifetime = 30 * time.Minute\n```\n\n"
                    "Then wrap the fraud check with its own 2s timeout so it can never "
                    "pin a DB connection again.",
                    tools=["edit", "run_in_terminal"],
                    files=["src/db/pool.go", "src/checkout/handler.go"],
                ),
                T(
                    "Applied it and ran the load test — p99 dropped from 31s to 480ms.",
                    "That's the fix. The connection was the bottleneck, not the "
                    "database itself. To keep this from regressing, add a pool "
                    "saturation alert at 80% utilisation and a checkout latency SLO. "
                    "Nice work.",
                ),
            ],
        },
        {
            "source": "cli",
            "repo": "auth-service",
            "repo_path": "/home/dev/projects/auth-service",
            "model": "claude-opus-4.1",
            "day": 3,
            "dur": 884,
            "premium": 11,
            "aiu": 1.12,
            "inp": 140000,
            "cread": 118000,
            "cwrite": 9000,
            "outp": 6100,
            "title": "JWT refresh-token rotation invalidates valid sessions",
            "turns": [
                T(
                    "Users get logged out randomly. I think refresh-token rotation is "
                    "racing — two tabs refresh at once and one wins.",
                    "Classic refresh-rotation race. When two requests present the same "
                    "refresh token, the first rotates it and the second is treated as "
                    "reuse, so the whole token family is revoked. The fix is a short "
                    "grace window plus a single-flight lock per token family in Redis.",
                    thinking="Refresh reuse detection is firing on a legitimate "
                    "concurrent refresh. Need to make rotation atomic per family and "
                    "tolerate the old token for a few seconds.",
                    tools=["read_file", "grep"],
                    files=["internal/auth/refresh.go"],
                ),
                T(
                    "Show me the grace-window approach.",
                    "Keep the previous token valid for a 10s grace period and serialise "
                    "rotation with a Redis lock keyed by family id:\n\n"
                    '```go\nlock := rdb.SetNX(ctx, "rot:"+familyID, 1, 5*time.Second)\n'
                    "if !lock.Val() { return waitForRotated(ctx, familyID) }\n"
                    "// rotate, then keep old token usable for graceWindow\n"
                    'rdb.Set(ctx, "grace:"+oldJTI, newJTI, 10*time.Second)\n```\n\n'
                    "Reuse detection then only triggers outside the grace window, which "
                    "is the genuine-theft case.",
                    tools=["edit"],
                    files=["internal/auth/refresh.go", "internal/auth/redis.go"],
                    urls=["https://datatracker.ietf.org/doc/html/rfc6749"],
                ),
            ],
        },
        {
            "source": "cli",
            "repo": "mark",
            "repo_path": "/home/dev/projects/mark",
            "model": "gpt-5",
            "day": 5,
            "dur": 430,
            "premium": 4,
            "aiu": 0.28,
            "inp": 61000,
            "cread": 47000,
            "cwrite": 6400,
            "outp": 3200,
            "title": "Add reciprocal-rank-fusion to hybrid search",
            "turns": [
                T(
                    "I want hybrid search to fuse keyword and vector results with RRF "
                    "instead of my current weighted-sum hack.",
                    "RRF is a great choice — it's scale-free, so you don't have to "
                    "normalise BM25 against cosine. Rank each list independently, then "
                    "sum 1/(k+rank) per document:\n\n"
                    "```python\ndef rrf(rankings, k=60):\n    scores = {}\n"
                    "    for ranking in rankings:\n        for rank, doc_id in "
                    "enumerate(ranking):\n            scores[doc_id] = scores.get(doc_id, 0)"
                    " + 1/(k + rank)\n    return sorted(scores, key=scores.get, "
                    "reverse=True)\n```\n\n"
                    "Fuse at the chunk level, then collapse to sessions by taking each "
                    "session's best chunk.",
                    thinking="Weighted sums need per-query normalisation and are brittle."
                    " RRF only needs ranks, which is exactly what FTS5 and the vector "
                    "index already give us.",
                    tools=["read_file", "edit"],
                    files=["mark/search.py"],
                ),
            ],
        },
        {
            "source": "cli",
            "repo": "data-pipeline",
            "repo_path": "/home/dev/projects/data-pipeline",
            "model": "claude-sonnet-4.5",
            "day": 8,
            "dur": 356,
            "premium": 3,
            "aiu": 0.22,
            "inp": 54000,
            "cread": 41000,
            "cwrite": 5200,
            "outp": 2400,
            "title": "Airflow DAG exhausting retries on the nightly load",
            "turns": [
                T(
                    "The nightly ETL DAG keeps exhausting all 5 retries on the "
                    "transform task and paging me at 3am.",
                    "The retries are masking a deterministic failure — retrying a bad "
                    "input five times just delays the page. Let's separate transient "
                    "errors (worth retrying) from data errors (not). Add an "
                    "`on_failure_callback` that inspects the exception and fails fast on "
                    "schema violations:\n\n"
                    "```python\ndef classify(context):\n    exc = context['exception']\n"
                    "    if isinstance(exc, SchemaError):\n        context['ti']"
                    ".max_tries = context['ti'].try_number  # stop retrying\n```",
                    tools=["read_file", "run_in_terminal"],
                    files=["dags/nightly_load.py"],
                ),
            ],
        },
        {
            "source": "cli",
            "repo": "storefront-web",
            "repo_path": "/home/dev/projects/storefront-web",
            "model": "gpt-5",
            "day": 11,
            "dur": 520,
            "premium": 5,
            "aiu": 0.33,
            "inp": 70000,
            "cread": 54000,
            "cwrite": 6800,
            "outp": 3900,
            "title": "Hydration mismatch on the Next.js product page",
            "turns": [
                T(
                    "Next.js throws 'Text content did not match' on the product page. "
                    "It only happens for logged-in users.",
                    "A hydration mismatch means the server and client rendered "
                    "different HTML. 'Only for logged-in users' points at something "
                    "client-only leaking into the server render — almost always a date "
                    "formatted in the user's locale, or reading `localStorage` during "
                    "render.\n\n"
                    "```tsx\n// Defer locale-formatted price to after mount\nconst [price, "
                    "setPrice] = useState<string | null>(null);\nuseEffect(() => "
                    "setPrice(fmt(amount)), [amount]);\n```",
                    thinking="Server has no localStorage and no user TZ, so any "
                    "render that reads them diverges on the client. Defer to useEffect.",
                    tools=["read_file", "edit"],
                    files=["app/product/[id]/page.tsx"],
                ),
            ],
        },
        {
            "source": "cli",
            "repo": "infra-terraform",
            "repo_path": "/home/dev/projects/infra-terraform",
            "model": "claude-sonnet-4.5",
            "day": 14,
            "dur": 690,
            "premium": 7,
            "aiu": 0.51,
            "inp": 88000,
            "cread": 69000,
            "cwrite": 7600,
            "outp": 4600,
            "title": "Terraform plan shows constant drift on security groups",
            "turns": [
                T(
                    "Every `terraform plan` wants to recreate the same security-group "
                    "rules even though nothing changed.",
                    "Perma-diff on SG rules is almost always inline `ingress`/`egress` "
                    "blocks fighting with rules something else manages (or AWS "
                    "reordering them). Move to standalone "
                    "`aws_security_group_rule` resources so Terraform tracks each rule "
                    "by id:\n\n"
                    '```hcl\nresource "aws_security_group_rule" "https" {\n  type      '
                    '        = "ingress"\n  from_port         = 443\n  to_port           '
                    '= 443\n  protocol          = "tcp"\n  security_group_id = '
                    'aws_security_group.web.id\n  cidr_blocks       = ["0.0.0.0/0"]\n}\n```',
                    tools=["read_file", "edit", "run_in_terminal"],
                    files=["modules/network/security.tf"],
                ),
            ],
        },
        {
            "source": "cli",
            "repo": "ml-recommender",
            "repo_path": "/home/dev/projects/ml-recommender",
            "model": "claude-opus-4.1",
            "day": 18,
            "dur": 940,
            "premium": 9,
            "aiu": 0.88,
            "inp": 121000,
            "cread": 99000,
            "cwrite": 8800,
            "outp": 5400,
            "title": "Train/serve skew in feature scaling",
            "turns": [
                T(
                    "Offline AUC is 0.91 but online it behaves like 0.7. I suspect "
                    "train/serve skew.",
                    "A 20-point gap online is the signature of train/serve skew. The "
                    "most common cause is scaling features with statistics computed at "
                    "training time but recomputed (or skipped) at serving time. Persist "
                    "the fitted scaler and load the exact same one online:\n\n"
                    "```python\n# train: fit once, persist\njoblib.dump(scaler, "
                    "'artifacts/scaler.pkl')\n# serve: load the SAME scaler, never refit\n"
                    "scaler = joblib.load('artifacts/scaler.pkl')\nx = scaler.transform(raw)\n```\n\n"
                    "Then log feature distributions in both paths and diff them — the "
                    "skewed feature will jump out.",
                    thinking="Online ~ random-ish vs strong offline = the model sees "
                    "differently-distributed inputs at serving. Scaler mismatch is the "
                    "textbook cause.",
                    tools=["read_file", "grep", "edit"],
                    files=["src/features/scaling.py", "src/serving/predict.py"],
                ),
            ],
        },
        {
            "source": "cli",
            "repo": "mark",
            "repo_path": "/home/dev/projects/mark",
            "model": "gpt-5",
            "day": 22,
            "dur": 470,
            "premium": 4,
            "aiu": 0.30,
            "inp": 64000,
            "cread": 50000,
            "cwrite": 6200,
            "outp": 3300,
            "title": "Expose archive to agents over an MCP stdio server",
            "turns": [
                T(
                    "I want to expose search over my history as an MCP tool so other "
                    "agents can recall past solutions.",
                    "Perfect fit for an MCP stdio server. Expose three tools — "
                    "`search_history`, `get_session`, `list_recent` — backed by the "
                    "same search layer the UI uses:\n\n"
                    "```python\n@server.tool()\nasync def search_history(query: str, "
                    "mode: str = 'hybrid') -> list[dict]:\n    return search.search(query,"
                    " mode=mode, limit=10)\n```\n\n"
                    "Run it over stdio so there's no network surface and no API keys.",
                    tools=["read_file", "edit"],
                    files=["mark/mcp_server.py"],
                ),
            ],
        },
        # ---- VS Code chat (token counts estimated from text) ----------------
        {
            "source": "vscode",
            "repo": "storefront-web",
            "repo_path": "/home/dev/projects/storefront-web",
            "model": None,
            "day": 2,
            "dur": 240,
            "title": "Convert class components to hooks",
            "turns": [
                T(
                    "Convert this React class component to a function component with "
                    "hooks.",
                    "Map lifecycle methods to effects: `componentDidMount` + "
                    "`componentWillUnmount` become a single `useEffect` with a cleanup "
                    "return, and instance state becomes `useState`:\n\n"
                    "```jsx\nfunction Timer() {\n  const [n, setN] = useState(0);\n  "
                    "useEffect(() => {\n    const id = setInterval(() => setN(x => x + 1), "
                    "1000);\n    return () => clearInterval(id);\n  }, []);\n  return "
                    "<span>{n}</span>;\n}\n```",
                    files=["src/components/Timer.jsx"],
                ),
            ],
        },
        {
            "source": "vscode",
            "repo": "mobile-ios",
            "repo_path": "/home/dev/projects/mobile-ios",
            "model": None,
            "day": 6,
            "dur": 300,
            "title": "SwiftUI List scrolling jank with async images",
            "turns": [
                T(
                    "My SwiftUI List stutters when scrolling rows that load remote "
                    "images.",
                    "The jank is from decoding full-size images on the main thread and "
                    "re-fetching on every cell reuse. Use `AsyncImage` with a "
                    "downsampled cache and give rows a stable identity so they don't "
                    "re-render:\n\n"
                    "```swift\nList(items) { item in\n    Row(item: item)\n        "
                    ".id(item.id)\n}\n.listStyle(.plain)\n```\n\n"
                    "Pre-decode and downsample to the display size off the main actor; "
                    "that removes almost all of the stutter.",
                    files=["Sources/Feed/FeedView.swift"],
                ),
            ],
        },
        {
            "source": "vscode",
            "repo": "infra-terraform",
            "repo_path": "/home/dev/projects/infra-terraform",
            "model": None,
            "day": 9,
            "dur": 360,
            "title": "Write a reusable VPC module with private subnets",
            "turns": [
                T(
                    "Draft a Terraform module for a VPC with public and private "
                    "subnets across two AZs.",
                    "Here's a compact module interface — subnets derived from the VPC "
                    "CIDR with `cidrsubnet`, a NAT gateway per AZ for private egress:\n\n"
                    '```hcl\nvariable "cidr" { default = "10.0.0.0/16" }\nresource '
                    '"aws_subnet" "private" {\n  count             = 2\n  vpc_id      '
                    "      = aws_vpc.this.id\n  cidr_block        = cidrsubnet(var.cidr, "
                    "4, count.index + 2)\n  availability_zone = data.aws_availability_zones"
                    ".available.names[count.index]\n}\n```",
                    files=["modules/vpc/main.tf", "modules/vpc/variables.tf"],
                ),
            ],
        },
        {
            "source": "vscode",
            "repo": "ml-recommender",
            "repo_path": "/home/dev/projects/ml-recommender",
            "model": None,
            "day": 13,
            "dur": 180,
            "title": "Cosine vs dot product for embedding similarity",
            "turns": [
                T(
                    "When should I use cosine similarity vs raw dot product for "
                    "embedding search?",
                    "If your vectors are L2-normalised, cosine similarity and dot "
                    "product give the same ranking — cosine is just the dot product of "
                    "unit vectors. Use cosine (or normalise first) when magnitude is an "
                    "artefact of length and you only care about direction; use raw dot "
                    "product when magnitude carries signal, as with some learned "
                    "retrieval models trained on inner product.",
                ),
            ],
        },
        {
            "source": "vscode",
            "repo": "data-pipeline",
            "repo_path": "/home/dev/projects/data-pipeline",
            "model": None,
            "day": 16,
            "dur": 210,
            "title": "Pandas groupby blowing up memory on a wide frame",
            "turns": [
                T(
                    "A groupby().apply() on a 12M-row frame eats all my RAM.",
                    "`groupby().apply()` materialises every group and is the slow, "
                    "memory-hungry path. Replace it with a vectorised aggregation, and "
                    "downcast keys to categories so the group index is cheap:\n\n"
                    "```python\ndf['key'] = df['key'].astype('category')\nout = "
                    "df.groupby('key', observed=True)['amount'].agg(['sum', 'mean'])\n```\n\n"
                    "If it still doesn't fit, process in chunks and combine partial "
                    "aggregates.",
                    files=["src/transform/aggregate.py"],
                ),
            ],
        },
        {
            "source": "vscode",
            "repo": "mark",
            "repo_path": "/home/dev/projects/mark",
            "model": None,
            "day": 20,
            "dur": 160,
            "title": "Dark-mode color tokens for the UI",
            "turns": [
                T(
                    "Give me a small set of CSS custom properties for a clean dark "
                    "theme.",
                    "Define semantic tokens on `:root` and override them under a "
                    "`[data-theme=dark]` selector so components never hard-code "
                    "colours:\n\n"
                    "```css\n:root {\n  --bg: #ffffff;\n  --fg: #1b1f24;\n  --accent: "
                    "#3b82f6;\n}\n[data-theme=dark] {\n  --bg: #0f1216;\n  --fg: #e6e9ef;\n"
                    "  --accent: #59c2ff;\n}\n```",
                    files=["mark/web/styles.css"],
                ),
            ],
        },
        {
            "source": "vscode",
            "repo": "auth-service",
            "repo_path": "/home/dev/projects/auth-service",
            "model": None,
            "day": 27,
            "dur": 200,
            "title": "Sensible argon2id parameters for password hashing",
            "turns": [
                T(
                    "What argon2id parameters should I use for hashing passwords on a "
                    "typical web backend?",
                    "A good 2024-era baseline is argon2id with 19 MiB of memory, a time "
                    "cost of 2, and parallelism of 1, tuned so a single hash takes "
                    "~50-100ms on your hardware:\n\n"
                    "```python\nfrom argon2 import PasswordHasher\nph = PasswordHasher("
                    "time_cost=2, memory_cost=19456, parallelism=1)\n```\n\n"
                    "Measure on your real boxes and raise the cost until you hit your "
                    "latency budget.",
                ),
            ],
        },
        # ---- Cline-family + Cursor ------------------------------------------
        {
            "source": "cline",
            "repo": "payments-api",
            "repo_path": "/home/dev/projects/payments-api",
            "model": "claude-sonnet-4.5",
            "day": 4,
            "dur": 760,
            "inp": 28000,
            "cread": 17000,
            "outp": 3100,
            "title": "Add idempotency keys to payment-intent creation",
            "turns": [
                T(
                    "Add idempotency keys so a retried create-payment-intent never "
                    "double-charges.",
                    "I'll add an `Idempotency-Key` header, persist the first response "
                    "keyed by it, and replay that stored response on any retry within "
                    "24h:\n\n"
                    "```go\nif prior, ok := store.Get(key); ok {\n    return "
                    "prior.Response, nil  // replay, don't re-charge\n}\nresp := "
                    "createIntent(req)\nstore.Put(key, resp, 24*time.Hour)\n```\n\n"
                    "I've added a unique index on the key so concurrent retries can't "
                    "both insert.",
                    tools=["read_file", "edit", "run_in_terminal"],
                    files=["src/payments/intents.go", "src/store/idempotency.go"],
                ),
            ],
        },
        {
            "source": "cline",
            "repo": "auth-service",
            "repo_path": "/home/dev/projects/auth-service",
            "model": "claude-sonnet-4.5",
            "day": 12,
            "dur": 540,
            "inp": 21000,
            "cread": 12000,
            "outp": 2200,
            "title": "Rate-limit the login endpoint with a sliding window",
            "turns": [
                T(
                    "Add a per-IP sliding-window rate limit to the login endpoint.",
                    "Added a Redis sliding-window limiter — 10 attempts per IP per "
                    "minute, sorted-set based so it's accurate at the edges:\n\n"
                    '```go\npipe.ZRemRangeByScore(key, "0", fmt.Sprint(now-window))\n'
                    "pipe.ZAdd(key, redis.Z{Score: now, Member: reqID})\npipe.ZCard(key)\n"
                    "```\n\nOver the limit returns 429 with a `Retry-After`. Genuine "
                    "users almost never hit 10/min, but credential-stuffing does.",
                    tools=["edit", "run_in_terminal"],
                    files=["internal/middleware/ratelimit.go"],
                ),
            ],
        },
        {
            "source": "cursor",
            "repo": "storefront-web",
            "repo_path": "/home/dev/projects/storefront-web",
            "model": "claude-4.5-sonnet",
            "day": 7,
            "dur": 410,
            "inp": 19000,
            "cread": 11000,
            "outp": 2600,
            "title": "Refactor cart state from Context to Zustand",
            "turns": [
                T(
                    "Our cart Context re-renders the whole tree on every update. Move "
                    "it to Zustand with selectors.",
                    "Done. Zustand lets components subscribe to just the slice they "
                    "use, so adding an item no longer re-renders the header or footer:\n\n"
                    "```ts\nexport const useCart = create<CartState>((set) => ({\n  "
                    "items: [],\n  add: (p) => set((s) => ({ items: [...s.items, p] })),\n"
                    "}));\n// only re-renders when the count changes:\nconst count = "
                    "useCart((s) => s.items.length);\n```",
                    tools=["edit"],
                    files=["src/state/cart.ts", "src/components/Header.tsx"],
                ),
            ],
        },
        {
            "source": "cursor",
            "repo": "ml-recommender",
            "repo_path": "/home/dev/projects/ml-recommender",
            "model": "gpt-5",
            "day": 24,
            "dur": 380,
            "inp": 17000,
            "cread": 9000,
            "outp": 2300,
            "title": "Vectorise the feature pipeline with NumPy",
            "turns": [
                T(
                    "This feature loop is slow — rewrite it vectorised with NumPy.",
                    "Replaced the Python loop with broadcasting; it's ~50x faster on "
                    "the 2M-row sample:\n\n"
                    "```python\n# before: for-loop over rows\n# after: vectorised\n"
                    "z = (x - x.mean(axis=0)) / (x.std(axis=0) + 1e-8)\nfeatures = "
                    "np.hstack([z, np.log1p(counts)])\n```\n\n"
                    "Same outputs to 1e-6, just without the per-row Python overhead.",
                    tools=["edit", "run_in_terminal"],
                    files=["src/features/build.py"],
                ),
            ],
        },
        {
            "source": "cursor",
            "repo": "payments-api",
            "repo_path": "/home/dev/projects/payments-api",
            "model": "claude-4.5-sonnet",
            "day": 30,
            "dur": 600,
            "inp": 24000,
            "cread": 15000,
            "outp": 3000,
            "title": "Add OpenTelemetry tracing to the checkout flow",
            "turns": [
                T(
                    "Instrument the checkout flow with OpenTelemetry so I can see where "
                    "the latency goes.",
                    "Wired up OTel spans across the handler, the fraud check, and the "
                    "DB calls, exporting OTLP to the collector:\n\n"
                    '```go\nctx, span := tracer.Start(ctx, "checkout")\ndefer span.End()\n'
                    'span.SetAttributes(attribute.String("cart.id", cartID))\n```\n\n'
                    "Now each request is a single trace and the slow fraud call shows "
                    "up as the widest span.",
                    tools=["edit"],
                    files=["src/checkout/handler.go", "src/telemetry/otel.go"],
                ),
            ],
        },
        {
            "source": "cline",
            "repo": "mobile-ios",
            "repo_path": "/home/dev/projects/mobile-ios",
            "model": "claude-sonnet-4.5",
            "day": 35,
            "dur": 300,
            "inp": 15000,
            "cread": 8000,
            "outp": 1800,
            "title": "Fix a retain cycle in the feed view model",
            "turns": [
                T(
                    "Instruments shows the feed view model is leaking. Probably a "
                    "retain cycle in a closure.",
                    "Found it — the network callback captures `self` strongly. Capture "
                    "it weakly so the view model can deallocate:\n\n"
                    "```swift\nclient.fetch { [weak self] result in\n    guard let self "
                    "else { return }\n    self.apply(result)\n}\n```\n\n"
                    "Leak is gone in Instruments after the change.",
                    tools=["read_file", "edit"],
                    files=["Sources/Feed/FeedViewModel.swift"],
                ),
            ],
        },
        # ---- ChatGPT (imported export; estimated tokens) --------------------
        {
            "source": "chatgpt",
            "repo": None,
            "repo_path": None,
            "model": "gpt-4o",
            "day": 10,
            "dur": 0,
            "title": "Postgres vs DynamoDB for an event store",
            "turns": [
                T(
                    "I'm building an event store. Should I use Postgres or DynamoDB?",
                    "For an event store, the deciding question is your access pattern. "
                    "If you mostly append events and read them back by aggregate id in "
                    "order, both work — but Postgres gives you transactional appends, "
                    "easy projections with SQL, and strong consistency out of the box. "
                    "Reach for DynamoDB when you need single-digit-millisecond writes at "
                    "massive scale and can model everything around the partition key. "
                    "For most teams starting out, Postgres is the lower-risk choice and "
                    "you can add a stream/CDC later.",
                ),
            ],
        },
        {
            "source": "chatgpt",
            "repo": None,
            "repo_path": None,
            "model": "o3",
            "day": 21,
            "dur": 0,
            "title": "Explain the Raft consensus algorithm simply",
            "turns": [
                T(
                    "Explain Raft consensus like I'm comfortable with code but new to "
                    "distributed systems.",
                    "Raft keeps a replicated log consistent across servers by electing "
                    "a single leader. Time is split into terms; in each term servers "
                    "vote for one leader. The leader takes all client writes, appends "
                    "them to its log, and replicates to followers. An entry is "
                    "'committed' once a majority have stored it — that majority overlap "
                    "is what guarantees safety across leader changes. If the leader "
                    "dies, a follower times out, bumps the term, and runs an election. "
                    "The two ideas that make it click: a majority quorum, and an "
                    "append-only log that only ever moves forward.",
                ),
            ],
        },
        {
            "source": "chatgpt",
            "repo": None,
            "repo_path": None,
            "model": "gpt-4o",
            "day": 33,
            "dur": 0,
            "title": "Bash script to rotate and compress logs",
            "turns": [
                T(
                    "Write a small bash script to rotate logs older than 7 days and "
                    "gzip them.",
                    "Here's a self-contained rotation script you can drop in a "
                    "cron job:\n\n"
                    "```bash\n#!/usr/bin/env bash\nset -euo pipefail\nLOG_DIR=/var/log/app\n"
                    "find \"$LOG_DIR\" -name '*.log' -mtime +7 -print0 \\\n  | while "
                    'IFS= read -r -d \'\' f; do\n      gzip -9 "$f" && echo "rotated $f"\n'
                    "    done\n```\n\nRun it daily; `-mtime +7` matches files older than "
                    "seven days and `gzip -9` gives the best compression.",
                ),
            ],
        },
    ]


def _content_hash(title: str, turns: list[dict]) -> str:
    h = hashlib.sha256(title.encode("utf-8"))
    for t in turns:
        h.update(t["user"].encode("utf-8", "ignore"))
        h.update(t["assistant"].encode("utf-8", "ignore"))
    return h.hexdigest()


def _metrics(spec: dict, turns: list[dict]) -> dict:
    src = spec["source"]
    if src == "cli":
        inp, cread = spec["inp"], spec["cread"]
        cwrite, outp = spec.get("cwrite", 0), spec["outp"]
        model = spec["model"]
        return {
            "duration_seconds": spec["dur"],
            "model": model,
            "input_tokens": inp,
            "output_tokens": outp,
            "premium_requests": spec.get("premium"),
            "aiu": spec.get("aiu"),
            "est_cost_usd": compute_cost(model, inp, outp, cread, cwrite, True),
            "tokens_estimated": 0,
        }
    if src in ("cline", "cursor"):
        inp, cread, outp = spec["inp"], spec.get("cread", 0), spec["outp"]
        model = spec["model"]
        return {
            "duration_seconds": spec["dur"],
            "model": model,
            "input_tokens": inp,
            "output_tokens": outp,
            "premium_requests": None,
            "aiu": None,
            "est_cost_usd": compute_cost(model, inp, outp, cread, 0, False),
            "tokens_estimated": 0,
        }
    # vscode / chatgpt: estimate tokens from text, flag as estimated.
    text_in = " ".join(t["user"] for t in turns)
    text_out = " ".join(t["assistant"] for t in turns)
    inp, outp = estimate_tokens(text_in), estimate_tokens(text_out)
    model = spec.get("model")
    return {
        "duration_seconds": spec["dur"] or None,
        "model": model,
        "input_tokens": inp,
        "output_tokens": outp,
        "premium_requests": None,
        "aiu": None,
        "est_cost_usd": compute_cost(model, inp, outp),
        "tokens_estimated": 1,
    }


def _build_session(spec: dict, idx: int) -> dict:
    src = spec["source"]
    sid = f"demo-{src}-{idx:02d}"
    created = NOW - timedelta(days=spec["day"], minutes=spec["dur"] / 60.0)
    dur = spec["dur"] or 120
    raw_turns = spec["turns"]
    turns: list[dict] = []
    for i, rt in enumerate(raw_turns):
        # Spread turn timestamps evenly across the session's duration.
        offset = (dur / max(1, len(raw_turns))) * i
        ts = (created + timedelta(seconds=offset)).isoformat()
        code_blocks = [
            {"language": lang or "text", "content": body.strip()}
            for lang, body in FENCE_RE.findall(rt["assistant"])
        ]
        turns.append(
            {
                "turn_index": i,
                "user_message": rt["user"],
                "assistant_response": rt["assistant"],
                "thinking": rt["thinking"],
                "tools": rt["tools"],
                "timestamp": ts,
                "files": rt["files"],
                "urls": rt["urls"],
                "code_blocks": code_blocks,
            }
        )
    updated = (created + timedelta(seconds=dur)).isoformat()
    metrics = _metrics(spec, raw_turns)
    source_path = {
        "cli": str(Path.home() / ".copilot" / "session-store.db"),
        "vscode": f"~/Library/Application Support/Code/User/workspaceStorage/{sid}",
        "cline": f"~/.vscode/globalStorage/cline/{sid}.json",
        "cursor": f"~/Library/Application Support/Cursor/workspaceStorage/{sid}",
        "chatgpt": "conversations.json",
    }.get(src, sid)
    return {
        "id": sid,
        "source": src,
        "title": spec["title"],
        "workspace_id": spec["repo"],
        "repository": spec["repo"],
        "repo_path": spec["repo_path"],
        "requester": "you",
        "responder": metrics.get("model") or "assistant",
        "created_at": created.isoformat(),
        "updated_at": updated,
        "metrics": metrics,
        "source_path": source_path,
        "content_hash": _content_hash(spec["title"], raw_turns),
        "turns": turns,
        "extra_files": [],
        "attachments": [],
    }


def _seed_collections() -> None:
    """Create a few auto-updating collections (rule-based) if none exist."""
    existing = {c["name"] for c in mark_collections.list_collections()}
    wanted = [
        (
            "Checkout reliability",
            "Everything touching the payments checkout path",
            "flame",
            "#f0a45b",
            {"repo": "payments-api"},
            True,
        ),
        (
            "Building Mark",
            "Work on the Mark app itself",
            "sparkles",
            "#59c2ff",
            {"repo": "mark"},
            False,
        ),
        (
            "Explainers & learning",
            "Conceptual Q&A imported from ChatGPT",
            "message",
            "#56d6a0",
            {"source": "chatgpt"},
            False,
        ),
    ]
    for name, desc, icon, color, rule, pinned in wanted:
        if name not in existing:
            mark_collections.create(name, desc, icon, color, rule, pinned)


def _reset_database(conn) -> int:
    """Clear demo content while preserving monotonic semantic cache identity."""
    conn.execute("BEGIN EXCLUSIVE")
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'embed_generation'"
    ).fetchone()
    try:
        next_generation = int(row[0]) + 1 if row else 1
    except (TypeError, ValueError):
        next_generation = 1
    conn.execute("DELETE FROM search_index")
    for table in (
        "collection_members",
        "collections",
        "source_file_stat",
        "tombstones",
        "sessions",
        "meta",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('embed_generation', ?)",
        (str(next_generation),),
    )
    conn.commit()
    return next_generation


def _targets_real_archive(data_dir: Path, db_path: Path) -> bool:
    real = (Path.home() / ".mark").resolve()
    real_db = (real / "mark.db").resolve()
    configured_data = data_dir.resolve()
    configured_db = db_path.resolve()
    if real == configured_data or real_db == configured_db:
        return True
    try:
        return (
            real_db.exists()
            and configured_db.exists()
            and os.path.samefile(real_db, configured_db)
        )
    except OSError:
        return False


# Real sources are kept ENABLED for the demo archive (so the seeded sessions of
# those sources stay visible), but their roots are redirected to an empty folder
# so the server's startup import finds nothing on disk.


def _write_sources_toml() -> Path:
    """Redirect real sources at a non-existent path so the demo imports no live data.

    Disabling the sources instead would hide their seeded demo sessions (the
    visibility layer filters out sessions whose adapter is disabled), so we keep
    them enabled and point every root at one path that doesn't exist. Each
    adapter then discovers nothing (globs match nothing; store files don't
    exist) without ever touching your real history.
    """
    sentinel = config.DATA_DIR / "_no_real_sources"
    if sentinel.exists():
        shutil.rmtree(sentinel, ignore_errors=True)
    path = Path(
        os.environ.get("MARK_SOURCES_FILE", config.DATA_DIR / "sources.toml")
    ).expanduser()
    body = (
        "# Written by seed_demo_data.py. Sources stay ENABLED so the seeded demo\n"
        "# sessions remain visible, but every root points at a path that does not\n"
        "# exist, so launching this archive never imports your real conversations.\n"
        "# Delete this file to let the demo sync your real sources.\n\n"
        f"[sources.vscode]\nroots = ['{sentinel}']\n\n"
        f"[sources.copilot_cli]\nroots = ['{sentinel}']\n\n"
        f"[sources.cline]\nroots = ['{sentinel}']\n\n"
        f"[sources.cursor]\nroots = ['{sentinel}']\n\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed Mark with demo data.")
    ap.add_argument("--reset", action="store_true", help="wipe the demo DB first")
    ap.add_argument("--no-embed", action="store_true", help="skip embeddings")
    ap.add_argument(
        "--force",
        action="store_true",
        help="allow seeding into the real ~/.mark directory",
    )
    args = ap.parse_args()

    if _targets_real_archive(config.DATA_DIR, config.DB_PATH) and not args.force:
        print(
            f"Refusing to seed demo data into your real archive ({config.DB_PATH}).\n"
            "Set MARK_DATA_DIR to a demo location (default ~/.mark-demo) or pass "
            "--force.",
            file=sys.stderr,
        )
        return 2

    print(f"Demo data dir: {config.DATA_DIR}")
    config.ensure_dirs()
    with embeddings.writer_lock():
        db.init_db()
        if args.reset:
            conn = db.connect()
            try:
                _reset_database(conn)
                conn.execute("VACUUM")
            finally:
                conn.close()
            print("Reset: cleared existing demo database.")

        specs = _specs()
        with db.transaction() as conn:
            cur = conn.cursor()
            for idx, spec in enumerate(specs, start=1):
                persist._write_session(cur, _build_session(spec, idx))
            conn.commit()
    print(f"Wrote {len(specs)} sessions.")

    _seed_collections()
    print("Created collections.")

    toml_path = _write_sources_toml()
    print(f"Redirected real sources to an empty dir via {toml_path}")

    if not args.no_embed:
        print("Building embeddings (this can take a moment the first time)...")
        try:
            ingest.ensure_index_ready(progress=lambda m: print("  " + m, end="\r"))
            print("\nEmbeddings ready.")
        except Exception as exc:
            print(f"\nEmbedding skipped ({exc}); keyword search still works.")

    db.set_meta("last_ingest", NOW.isoformat())
    print(
        "\nDone. Launch the demo (your real sources are redirected away, so this\n"
        "archive only ever shows the seeded demo data):\n"
        f"    MARK_DATA_DIR={config.DATA_DIR} {sys.executable} -m mark\n"
        "\nIf `mark` is on your PATH (e.g. the venv is active) you can instead run:\n"
        f"    MARK_DATA_DIR={config.DATA_DIR} mark\n"
        "\nThen open http://127.0.0.1:8765 in your browser.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
