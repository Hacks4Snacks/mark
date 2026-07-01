from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .base import (
    FENCE_RE,
    URL_RE,
    ImportSource,
    derive_title,
    estimate_metrics,
    parse_iso,
)

# The ``/c/<conversation-id>`` segment of a grok.com share URL — a stable id that
# survives re-export, so the same conversation dedups instead of duplicating.
_CONVO_URL_RE = re.compile(r"/c/([0-9a-fA-F][0-9a-fA-F-]{15,})")

# Speaker labels seen across validated Grok exporters, mapped to canonical roles.
# Kept liberal so a new exporter that says "You"/"Assistant" still normalises to
# the same two roles without a new handler.
_SPEAKER_ROLE = {
    "human": "user",
    "user": "user",
    "you": "user",
    "grok": "assistant",
    "assistant": "assistant",
}

# Grok's default answer mode carries no signal; only the deliberate modes
# (deepsearch/deepersearch/think/fun) are worth surfacing per turn.
_DEFAULT_MODE = "standard"


def _load(data: bytes) -> Any | None:
    """Parse export bytes to a JSON value, or ``None`` when it isn't JSON."""
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None


def _iso(ts: Any) -> str | None:
    """Normalise a message timestamp (ISO string or epoch number) to ISO-8601."""
    if isinstance(ts, str):
        dt = parse_iso(ts)
        return dt.isoformat() if dt else None
    if isinstance(ts, (int, float)):
        # Milliseconds vs seconds: 10^11 ~= year 5138 in seconds, so anything
        # larger is milliseconds.
        seconds = ts / 1000 if ts >= 1e11 else ts
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    return None


class _GrokFormat(ABC):
    """One validated Grok export shape (a specific tool + version).

    A format handler pins a strict detection signature and maps its raw JSON onto
    the shared *normalised conversation* — ``{url?, created?, messages}`` where
    each message is ``{role, text, mode, ts}`` — that :func:`_build_session`
    turns into a canonical session. Adding support for another export tool is a
    new handler here plus a fixture/test, never a new adapter or a change to the
    persistence contract.
    """

    @abstractmethod
    def matches(self, obj: Any) -> bool:
        """True if ``obj`` is this exact export format (cheap, high-signal)."""

    @abstractmethod
    def conversations(self, obj: Any) -> Iterable[dict[str, Any]]:
        """Yield one normalised conversation dict per conversation in ``obj``."""


class EnhancedGrokExportV2(_GrokFormat):
    """The *Enhanced Grok Export* userscript (greasyfork 537266), ``exportVersion``
    ``2.x``: a single top-level object with ``platform == "grok"`` and a
    ``conversation`` list of ``{speaker, content, mode, timestamp}`` messages.
    """

    def matches(self, obj: Any) -> bool:
        if not isinstance(obj, dict):
            return False
        if str(obj.get("platform", "")).strip().lower() != "grok":
            return False
        convo = obj.get("conversation")
        return isinstance(convo, list) and any(
            isinstance(m, dict) and "speaker" in m and "content" in m for m in convo
        )

    def conversations(self, obj: dict[str, Any]) -> Iterable[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for m in obj.get("conversation") or []:
            if not isinstance(m, dict):
                continue
            role = _SPEAKER_ROLE.get(str(m.get("speaker", "")).strip().lower())
            content = m.get("content")
            text = content.strip() if isinstance(content, str) else ""
            if role is None or not text:
                continue
            mode = m.get("mode")
            messages.append(
                {
                    "role": role,
                    "text": text,
                    "mode": mode.strip() if isinstance(mode, str) else "",
                    "ts": m.get("timestamp"),
                }
            )
        if messages:
            url = obj.get("url")
            yield {
                "url": url if isinstance(url, str) else None,
                "created": obj.get("exportDate"),
                "messages": messages,
            }


#: Registered, validated Grok export formats. Detection tries each in turn; the
#: first match parses. New tools/versions append here.
_HANDLERS: list[_GrokFormat] = [EnhancedGrokExportV2()]


def _turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each user prompt with the assistant reply(ies) that follow it."""
    paired: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for m in messages:
        if m["role"] == "user":
            if cur is not None:
                paired.append(cur)
            cur = {"user": m["text"], "asst": "", "modes": [], "ts": m["ts"]}
        else:
            if cur is None:
                cur = {"user": "", "asst": "", "modes": [], "ts": m["ts"]}
            cur["asst"] += ("\n\n" if cur["asst"] else "") + m["text"]
        if m["mode"]:
            cur["modes"].append(m["mode"])

    if cur is not None:
        paired.append(cur)

    turns: list[dict[str, Any]] = []
    for t in paired:
        user, asst = t["user"].strip(), t["asst"].strip()
        if not user and not asst:
            continue
        code_blocks = [
            {"language": (lang or "").strip() or None, "content": code.strip()}
            for lang, code in FENCE_RE.findall(asst)
        ]
        urls = list(
            dict.fromkeys(u.rstrip(".,);") for u in URL_RE.findall(f"{user} {asst}"))
        )
        # A Grok "mode" is display-only metadata about how the turn was answered.
        # It rides in the thinking lane, which persist stores verbatim but never
        # chunks — so it stays visible without polluting search or tool stats.
        modes = list(
            dict.fromkeys(mode for mode in t["modes"] if mode.lower() != _DEFAULT_MODE)
        )
        thinking = f"Mode: {', '.join(modes)}" if modes else ""
        turns.append(
            {
                "turn_index": len(turns),
                "user_message": user,
                "assistant_response": asst,
                "thinking": thinking,
                "tools": [],
                "timestamp": _iso(t["ts"]),
                "files": [],
                "urls": urls,
                "code_blocks": code_blocks,
            }
        )
    return turns


def _build_session(convo: dict[str, Any]) -> dict[str, Any] | None:
    """Assemble a canonical session dict from one normalised conversation."""
    turns = _turns(convo["messages"])
    if not turns:
        return None

    # Hash the normalised messages (not the raw file) so the signature is
    # per-conversation and changes only when this conversation changes.
    raw = json.dumps(convo["messages"], sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    url = convo.get("url")
    cid = None
    if url:
        m = _CONVO_URL_RE.search(url)
        if m:
            cid = m.group(1)
    if not cid:
        cid = content_hash[:16]

    stamps = [t["timestamp"] for t in turns if t["timestamp"]]
    created = _iso(convo.get("created")) or (stamps[0] if stamps else None)
    updated = (stamps[-1] if stamps else None) or created
    title = (derive_title(turns) or "Grok conversation")[:200]

    return {
        "id": f"grok-{cid}",
        "source": "grok",
        "title": title,
        "workspace_id": None,
        "repository": None,
        "repo_path": None,
        "requester": None,
        "responder": "Grok",
        "created_at": created,
        "updated_at": updated,
        "source_path": url,
        "content_hash": content_hash,
        "turns": turns,
        "metrics": estimate_metrics(turns),
    }


class GrokSource(ImportSource):
    """Import Grok conversations exported by any *validated* Grok export tool.

    This is an agent-*family* importer: it recognises a curated set of tested
    export formats (see :data:`_HANDLERS`), each with a strict signature, and
    normalises them all to a single ``grok`` session type. Unrecognised exports
    are declined so they fall back to a plain-document upload rather than being
    parsed on a guess.
    """

    key = "grok"
    label = "Grok export"

    def detect(self, filename: str, data: bytes) -> bool:
        obj = _load(data)
        return obj is not None and any(h.matches(obj) for h in _HANDLERS)

    def parse_export(self, data: bytes) -> Iterable[dict[str, Any]]:
        obj = _load(data)
        if obj is None:
            return
        handler = next((h for h in _HANDLERS if h.matches(obj)), None)
        if handler is None:
            return
        for convo in handler.conversations(obj):
            session = _build_session(convo)
            if session:
                yield session
