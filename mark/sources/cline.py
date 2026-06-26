from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import config
from ..persist import write_session
from .base import (
    _FENCE_RE,
    _URL_RE,
    ProgressCb,
    WatchedSource,
    _compute_cost,
    _epoch_ms_to_iso,
    _estimate_tokens,
    _parse_iso,
    _repo_from_cwd,
    _ts_diff_seconds,
)

_ENV_DETAILS_RE = re.compile(
    r"<environment_details>.*?</environment_details>", re.DOTALL
)
_TAG_UNWRAP_RE = re.compile(r"</?(?:task|user_message|feedback|answer|thinking)>")
_CWD_RE = re.compile(r"Current Working Directory \(([^)]+)\)")


def _cline_source_name(ext_id: str, ext_map: dict[str, str]) -> str:
    if ext_id in ext_map:
        return ext_map[ext_id]
    base = ext_id.split(".")[-1].lower()
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "agent"


def _clean_user_text(text: str) -> str:
    text = _ENV_DETAILS_RE.sub(" ", text)
    text = _TAG_UNWRAP_RE.sub("", text)
    return text.strip()


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "") or ""
    if kind == "tool_use":
        name = block.get("name", "tool")
        arg = (
            json.dumps(block.get("input"))[:60]
            if block.get("input") is not None
            else ""
        )
        return f"\n`▷ {name}` {arg}\n"
    if kind == "tool_result":
        # Tool outputs (file reads, command dumps) are bulky and low-value for
        # search — keep only a short trace.
        content = block.get("content")
        text = (
            " ".join(_block_text(b) for b in content)
            if isinstance(content, list)
            else str(content or "")
        )
        text = " ".join(text.split())
        return f"  ⮑ {text[:100]}\n" if text else ""
    if kind == "image":
        return "[image]"
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_block_text(b) for b in content).strip()
    return ""


def _has_real_text(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict)
            and b.get("type") == "text"
            and (b.get("text") or "").strip()
            for b in content
        )
    return False


def _cline_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair real user prompts with the assistant's full response (incl. tool activity)."""
    turns: list[dict[str, Any]] = []
    cur_user: str | None = None
    cur_asst: list[str] = []
    cur_ts: Any = None

    def flush() -> None:
        nonlocal cur_user, cur_asst, cur_ts
        if cur_user is not None or cur_asst:
            asst = "".join(cur_asst).strip()
            user = (cur_user or "").strip()
            if user or asst:
                code_blocks = [
                    {"language": (lang or "").strip() or None, "content": code.strip()}
                    for lang, code in _FENCE_RE.findall(asst)
                ]
                urls = list(
                    dict.fromkeys(
                        u.rstrip(".,);") for u in _URL_RE.findall(f"{user} {asst}")
                    )
                )
                turns.append(
                    {
                        "turn_index": len(turns),
                        "user_message": user,
                        "assistant_response": asst,
                        "thinking": "",
                        "tools": [],
                        "timestamp": _epoch_ms_to_iso(cur_ts),
                        "files": [],
                        "urls": urls,
                        "code_blocks": code_blocks,
                    }
                )
        cur_user, cur_asst, cur_ts = None, [], None

    for m in messages:
        if not isinstance(m, dict):
            continue
        role, content, ts = m.get("role"), m.get("content"), m.get("ts")
        if role == "user" and _has_real_text(content):
            flush()
            cur_user = _clean_user_text(_content_text(content))
            cur_ts = ts
        elif role == "user":  # tool_result feeding back to the assistant
            cur_asst.append("\n" + _content_text(content))
        elif role == "assistant":
            if cur_ts is None:
                cur_ts = ts
            cur_asst.append(_content_text(content) + "\n")
    flush()
    return turns


def _ui_messages(task_dir: Path) -> list[dict[str, Any]]:
    """Cline's per-task UI log (``ui_messages.json``); [] when absent/unreadable."""
    try:
        data = json.loads((task_dir / "ui_messages.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _api_req_payloads(ui: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The decoded ``api_req_started`` payloads (one per model request)."""
    out: list[dict[str, Any]] = []
    for m in ui:
        if not isinstance(m, dict) or m.get("say") != "api_req_started":
            continue
        try:
            payload = json.loads(m.get("text") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _ui_token_totals(
    payloads: list[dict[str, Any]],
) -> tuple[int, int, int, int, float] | None:
    """Sum per-request (in, out, cache_read, cache_write, cost) from Cline's UI log.

    Real Cline (``saoudrizwan.claude-dev``) never writes ``history_item.json``; the
    authoritative token/cost totals live in the ``api_req_started`` records of
    ``ui_messages.json`` instead. Values may be ints or numeric strings.
    """
    if not payloads:
        return None
    tin = tout = cr = cw = 0
    cost = 0.0
    for j in payloads:
        tin += int(float(j.get("tokensIn") or 0))
        tout += int(float(j.get("tokensOut") or 0))
        cr += int(float(j.get("cacheReads") or 0))
        cw += int(float(j.get("cacheWrites") or 0))
        with contextlib.suppress(TypeError, ValueError):
            cost += float(j.get("cost") or 0)
    return tin, tout, cr, cw, cost


def _cline_model(
    task_dir: Path, history: dict[str, Any], payloads: list[dict[str, Any]]
) -> str | None:
    """Best model id from task_metadata, then the API config name, then the UI log."""
    tm = task_dir / "task_metadata.json"
    try:
        usage = json.loads(tm.read_text()).get("model_usage") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        usage = []
    ids = [
        u.get("model_id") for u in usage if isinstance(u, dict) and u.get("model_id")
    ]
    if ids:
        model = Counter(ids).most_common(1)[0][0]
        if isinstance(model, str):
            return model.split("/", 1)[-1] if "/" in model else model
    # Fallbacks for forks (Zoo Code/Roo) without task_metadata.model_usage.
    cfg = (history.get("apiConfigName") or "").strip()
    if cfg and cfg.lower() != "default":
        return cfg
    for j in payloads:
        for key in ("model", "apiModelId", "modelId"):
            val = j.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().split("/", 1)[-1]
    return None


def _cwd_from_messages(messages: list[dict[str, Any]]) -> str | None:
    """Recover the workspace path from the agent's ``<environment_details>`` header."""
    for m in messages:
        if not isinstance(m, dict):
            continue
        match = _CWD_RE.search(_content_text(m.get("content")))
        if match:
            return match.group(1).strip()
    return None


def _parse_cline_task(task_dir: Path, source: str) -> dict[str, Any] | None:
    api = task_dir / "api_conversation_history.json"
    try:
        raw = api.read_bytes()
        messages = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(messages, list) or not messages:
        return None
    turns = _cline_turns(messages)
    if not turns:
        return None

    history = {}
    hi = task_dir / "history_item.json"
    if hi.exists():
        try:
            history = json.loads(hi.read_text())
        except (OSError, json.JSONDecodeError):
            history = {}

    ui = _ui_messages(task_dir)
    payloads = _api_req_payloads(ui)
    model = _cline_model(task_dir, history, payloads)
    # Cline omits history_item.json; fall back to the env-header cwd for the repo.
    workspace = history.get("workspace") or _cwd_from_messages(messages)
    title = (history.get("task") or turns[0]["user_message"] or "Untitled").strip()
    title = title.splitlines()[0][:90] if title else "Untitled"

    tokens_in = int(history.get("tokensIn") or 0)
    tokens_out = int(history.get("tokensOut") or 0)
    cache_r = int(history.get("cacheReads") or 0)
    cache_w = int(history.get("cacheWrites") or 0)
    cost = history.get("totalCost") or 0
    # Real Cline keeps token/cost totals in ui_messages.json, not history_item.json.
    if tokens_in == 0 and tokens_out == 0:
        ui_totals = _ui_token_totals(payloads)
        if ui_totals:
            tokens_in, tokens_out, cache_r, cache_w, ui_cost = ui_totals
            if not cost:
                cost = ui_cost
    estimated = tokens_in == 0 and tokens_out == 0
    if estimated:
        tokens_in = sum(_estimate_tokens(t["user_message"]) for t in turns)
        tokens_out = sum(_estimate_tokens(t["assistant_response"]) for t in turns)
    if not cost:
        cost = _compute_cost(
            model, tokens_in, tokens_out, cache_r, cache_w, input_includes_cache=False
        )

    # Collect every epoch-ms timestamp we can find (task-dir name, history save
    # time, UI-log messages, and per-turn stamps); created = earliest, updated =
    # latest. This recovers timestamps for Cline (which omits history_item.json and
    # message ts) and guarantees updated_at >= created_at for Zoo Code, whose
    # history_item.ts is a save time that can post-date the final message.
    stamps = [t["timestamp"] for t in turns if t["timestamp"]]
    epochs: list[int] = []
    if task_dir.name.isdigit():
        epochs.append(int(task_dir.name))
    if isinstance(history.get("ts"), (int, float)):
        epochs.append(int(history["ts"]))
    epochs.extend(
        int(m["ts"])
        for m in ui
        if isinstance(m, dict) and isinstance(m.get("ts"), (int, float))
    )
    for s in stamps:
        dt = _parse_iso(s)
        if dt:
            epochs.append(int(dt.timestamp() * 1000))
    if epochs:
        created = _epoch_ms_to_iso(min(epochs))
        updated = _epoch_ms_to_iso(max(epochs))
    else:
        created = stamps[0] if stamps else None
        updated = stamps[-1] if stamps else created

    return {
        "id": f"{source}-{task_dir.name}",
        "source": source,
        "title": title,
        "workspace_id": None,
        "repository": _repo_from_cwd(None, workspace),
        "repo_path": workspace,
        "requester": None,
        "responder": source,
        "created_at": created,
        "updated_at": updated,
        "source_path": str(task_dir),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "turns": turns,
        "metrics": {
            "duration_seconds": (
                _ts_diff_seconds(stamps[0], stamps[-1])
                if len(stamps) >= 2
                else _ts_diff_seconds(created, updated)
            ),
            "model": model,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "premium_requests": None,
            "aiu": None,
            "est_cost_usd": round(float(cost), 4),
            "tokens_estimated": 1 if estimated else 0,
        },
    }


def _iter_cline_task_dirs(
    roots: list[Path], ext_map: dict[str, str]
) -> Iterable[tuple[Path, str]]:
    """Yield (task_dir, source) for every Cline-family extension found."""
    for gs in roots:
        for ext_dir in gs.glob("*/tasks"):
            if not ext_dir.is_dir():
                continue
            source = _cline_source_name(ext_dir.parent.name, ext_map)
            for task_dir in ext_dir.iterdir():
                if (
                    task_dir.is_dir()
                    and (task_dir / "api_conversation_history.json").exists()
                ):
                    yield task_dir, source


class ClineSource(WatchedSource):
    key = "cline"
    row_sources = tuple(sorted(set(config.CLINE_FAMILY_SOURCES.values())))

    def default_config(self) -> config.SourceConfig:
        return config.SourceConfig(
            key=self.key,
            roots=config.vscode_global_storage_roots(),
            label="Coding agents (Cline family)",
            options={"extensions": {}},
        )

    def fingerprint(self, cfg: config.SourceConfig) -> str:
        count = 0
        newest = 0
        for gs in cfg.roots:
            for f in gs.glob("*/tasks/*/api_conversation_history.json"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                count += 1
                if st.st_mtime_ns > newest:
                    newest = st.st_mtime_ns
        return f"ag:{count}:{newest}"

    def ingest(
        self,
        cur,
        existing: dict[str, str],
        cfg: config.SourceConfig,
        *,
        rebuild: bool,
        progress: ProgressCb | None = None,
    ) -> dict[str, int]:
        """Index Cline / Zoo Code / Roo / Kilo task histories from globalStorage."""
        ext_map = {
            **config.CLINE_FAMILY_SOURCES,
            **(cfg.options.get("extensions") or {}),
        }
        counts = {"added": 0, "updated": 0, "skipped": 0}
        seen = 0
        for task_dir, source in _iter_cline_task_dirs(cfg.roots, ext_map):
            session = _parse_cline_task(task_dir, source)
            if not session:
                continue
            prior = existing.get(session["id"])
            if prior is not None and prior == session["content_hash"] and not rebuild:
                counts["skipped"] += 1
                continue
            write_session(cur, session)
            counts["added" if prior is None else "updated"] += 1
            seen += 1
            if progress and seen % 50 == 0:
                progress(f"Indexed {seen} coding-agent tasks...")
        return counts
