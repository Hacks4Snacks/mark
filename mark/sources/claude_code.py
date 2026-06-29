from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import config
from ..persist import load_file_signatures, record_file_signature, write_session
from .base import (
    FENCE_RE,
    URL_RE,
    ProgressCb,
    WatchedSource,
    compute_cost,
    derive_title,
    estimate_tokens,
    parse_iso,
    repo_from_cwd,
    ts_diff_seconds,
)

# Claude Code wraps a few prose-only system payloads in tags that add no search
# value; unwrap/strip them so a real prompt reads cleanly.
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_TAG_UNWRAP_RE = re.compile(
    r"</?(?:command-name|command-message|command-args"
    r"|local-command-stdout|local-command-stderr|user-prompt-submit-hook)>"
)

# Built-in file-writing tools whose input carries the path of a file the agent
# creates or edits. (Read/Grep/Glob/Bash are not file *writes*.)
_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_FILE_ARG_KEYS = ("file_path", "notebook_path", "path")


def _tool_file_path(name: str | None, args: Any) -> str | None:
    if not name or name not in _FILE_WRITE_TOOLS or not isinstance(args, dict):
        return None
    for key in _FILE_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _block_text(block: Any) -> str:
    """Render one Anthropic content block to compact, searchable text."""
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
    """True when a user message carries an actual prompt (not just tool_result)."""
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


def _clean_user_text(text: str) -> str:
    text = _SYSTEM_REMINDER_RE.sub(" ", text)
    text = _TAG_UNWRAP_RE.sub("", text)
    return text.strip()


def _claude_turns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each real user prompt with the assistant's full response.

    Tool activity (assistant ``tool_use`` and the ``tool_result`` the user role
    feeds back) is folded into the assistant side of the same turn, so one
    prompt that drives a multi-step agent run is a single turn — matching the
    other coding-agent adapters. ``isMeta`` (system-injected) user messages and
    ``isSidechain`` (subagent) lines are skipped; subagent token usage is still
    counted in :func:`_session_metrics` for cost fidelity.
    """
    turns: list[dict[str, Any]] = []
    cur_user: str | None = None
    cur_asst: list[str] = []
    cur_thinking: list[str] = []
    cur_tools: list[str] = []
    cur_files: list[str] = []
    cur_ts: str | None = None

    def flush() -> None:
        nonlocal cur_user, cur_asst, cur_thinking, cur_tools, cur_files, cur_ts
        if cur_user is not None or cur_asst:
            asst = "".join(cur_asst).strip()
            user = (cur_user or "").strip()
            if user or asst:
                code_blocks = [
                    {"language": (lang or "").strip() or None, "content": code.strip()}
                    for lang, code in FENCE_RE.findall(asst)
                ]
                urls = list(
                    dict.fromkeys(
                        u.rstrip(".,);") for u in URL_RE.findall(f"{user} {asst}")
                    )
                )
                turns.append(
                    {
                        "turn_index": len(turns),
                        "user_message": user,
                        "assistant_response": asst,
                        "thinking": "\n\n".join(cur_thinking).strip(),
                        "tools": list(dict.fromkeys(cur_tools)),
                        "timestamp": cur_ts,
                        "files": list(dict.fromkeys(cur_files)),
                        "urls": urls,
                        "code_blocks": code_blocks,
                    }
                )
        cur_user, cur_asst, cur_thinking = None, [], []
        cur_tools, cur_files, cur_ts = [], [], None

    for ev in events:
        if not isinstance(ev, dict) or ev.get("isSidechain"):
            continue
        kind = ev.get("type")
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        ts = ev.get("timestamp")
        if kind == "user":
            if ev.get("isMeta"):
                continue
            if _has_real_text(content):
                flush()
                cur_user = _clean_user_text(_content_text(content))
                cur_ts = ts
            else:  # tool_result feeding back to the assistant
                cur_asst.append("\n" + _content_text(content))
        elif kind == "assistant":
            if cur_ts is None:
                cur_ts = ts
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "thinking":
                        th = (b.get("thinking") or "").strip()
                        if th:
                            cur_thinking.append(th)
                    elif btype == "tool_use":
                        nm = b.get("name")
                        if nm:
                            cur_tools.append(nm)
                        fp = _tool_file_path(nm, b.get("input"))
                        if fp:
                            cur_files.append(fp)
            cur_asst.append(_content_text(content) + "\n")
    flush()
    return turns


def _session_metrics(
    events: list[dict[str, Any]], turns: list[dict[str, Any]]
) -> dict[str, Any]:
    """Real token/model/cost totals from the per-message ``usage`` records.

    Sums every assistant message (including subagent sidechains) since each
    carries its own Anthropic ``usage``. ``input_tokens`` is exclusive of cache,
    so cost prices fresh input, cache reads, and cache writes separately.
    """
    inp = outp = cread = cwrite = 0
    models: list[str] = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        model = msg.get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            models.append(model)
        usage = msg.get("usage") or {}
        inp += int(usage.get("input_tokens") or 0)
        outp += int(usage.get("output_tokens") or 0)
        cread += int(usage.get("cache_read_input_tokens") or 0)
        cwrite += int(usage.get("cache_creation_input_tokens") or 0)

    model = Counter(models).most_common(1)[0][0] if models else None
    estimated = inp == 0 and outp == 0
    if estimated:
        inp = sum(estimate_tokens(t["user_message"]) for t in turns)
        outp = sum(estimate_tokens(t["assistant_response"]) for t in turns)
        cost = compute_cost(model, inp, outp)
    else:
        cost = compute_cost(model, inp, outp, cread, cwrite, input_includes_cache=False)

    stamps = [t["timestamp"] for t in turns if t["timestamp"]]
    return {
        "duration_seconds": (
            ts_diff_seconds(stamps[0], stamps[-1]) if len(stamps) >= 2 else None
        ),
        "model": model,
        "input_tokens": inp,
        "output_tokens": outp,
        "premium_requests": None,
        "aiu": None,
        "est_cost_usd": cost,
        "tokens_estimated": 1 if estimated else 0,
    }


def _first_value(events: list[dict[str, Any]], key: str) -> str | None:
    for ev in events:
        val = ev.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _session_title(events: list[dict[str, Any]], turns: list[dict[str, Any]]) -> str:
    """Claude's own generated summary if the transcript carries one, else derive."""
    summary: str | None = None
    for ev in events:
        if ev.get("type") == "summary":
            text = (ev.get("summary") or "").strip()
            if text:
                summary = text  # keep the last (most recent) summary
    return summary or derive_title(turns)


def _bounds(stamps: list[str]) -> tuple[str | None, str | None]:
    """Earliest and latest ISO timestamps (created/updated) by parsed instant."""
    dated: list[tuple[datetime, str]] = []
    for s in stamps:
        d = parse_iso(s)
        if d is not None:
            dated.append((d, s))
    if not dated:
        return None, None
    dated.sort(key=lambda x: x[0])
    return dated[0][1], dated[-1][1]


def parse_transcript(path: Path) -> dict[str, Any] | None:
    """Parse one ``<session>.jsonl`` transcript into a canonical session dict.

    Returns ``None`` for an unreadable, empty, or turn-less transcript so the
    caller can skip it.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    if not events:
        return None

    turns = _claude_turns(events)
    if not turns:
        return None

    cwd = _first_value(events, "cwd")
    stamps = [
        ev["timestamp"]
        for ev in events
        if isinstance(ev.get("timestamp"), str) and ev["timestamp"]
    ]
    created, updated = _bounds(stamps)
    session_id = path.stem

    return {
        "id": f"claude-code-{session_id}",
        "source": "claude-code",
        "title": _session_title(events, turns),
        "workspace_id": None,
        "repository": repo_from_cwd(None, cwd),
        "repo_path": cwd,
        "requester": None,
        "responder": "Claude Code",
        "created_at": created,
        "updated_at": updated or created,
        "source_path": str(path),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "turns": turns,
        "metrics": _session_metrics(events, turns),
    }


def _iter_transcripts(roots: list[Path]) -> Iterable[Path]:
    """Yield every ``projects/<encoded-cwd>/<session>.jsonl`` transcript.

    The one-level glob excludes the deeper ``<session>/subagents/*.jsonl`` and
    ``<session>/tool-results/*`` sidecars, which are folded into their parent.
    """
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/*.jsonl")):
            if path.is_file():
                yield path


class ClaudeCodeSource(WatchedSource):
    key = "claude_code"
    row_sources = ("claude-code",)

    def default_config(self) -> config.SourceConfig:
        return config.SourceConfig(
            key=self.key,
            roots=config.claude_projects_roots(),
            label="Claude Code",
        )

    def fingerprint(self, cfg: config.SourceConfig) -> str:
        count = 0
        newest = 0
        for root in cfg.roots:
            for f in root.glob("*/*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                count += 1
                if st.st_mtime_ns > newest:
                    newest = st.st_mtime_ns
        return f"cc:{count}:{newest}"

    def ingest(
        self,
        cur,
        existing: dict[str, str],
        cfg: config.SourceConfig,
        *,
        rebuild: bool,
        progress: ProgressCb | None = None,
    ) -> dict[str, int]:
        """Index Claude Code session transcripts from ``~/.claude/projects``."""
        counts = {"added": 0, "updated": 0, "skipped": 0}
        sigs = load_file_signatures(cur, prefix="cc:")
        seen = 0
        for path in _iter_transcripts(cfg.roots):
            sp = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            sig = f"{st.st_mtime_ns}:{st.st_size}"
            # The content hash is derived solely from the transcript bytes, so an
            # unchanged file can't yield a changed session — skip the parse.
            if not rebuild and sigs.get(f"cc:{sp}") == sig:
                counts["skipped"] += 1
                continue
            session = parse_transcript(path)
            record_file_signature(cur, f"cc:{sp}", sig)
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
                progress(f"Indexed {seen} Claude Code sessions...")
        return counts
