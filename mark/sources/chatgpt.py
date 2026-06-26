from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import (
    _FENCE_RE,
    _URL_RE,
    ImportSource,
    _derive_title,
    _estimate_metrics,
)


def _iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _load(data: bytes) -> list | None:
    """Parse export bytes into a list of conversations, or None if not ChatGPT."""
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("conversations"), list):
            obj = obj["conversations"]
        elif isinstance(obj.get("mapping"), dict):
            obj = [obj]  # a single exported conversation
    return obj if isinstance(obj, list) else None


def _is_convo(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("mapping"), dict)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    ct = content.get("content_type")
    if ct == "text":
        return "\n".join(p for p in content.get("parts") or [] if isinstance(p, str))
    if ct in ("code", "execution_output"):
        return content.get("text") or ""
    if ct == "multimodal_text":
        out: list[str] = []
        for p in content.get("parts") or []:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict) and isinstance(p.get("text"), str):
                out.append(p["text"])
        return "\n".join(out)
    return ""


def _ordered_messages(convo: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """Visible (role, text, create_time) along the active branch, in order."""
    mapping = convo.get("mapping") or {}
    chain: list[str] = []
    node = convo.get("current_node")
    seen: set[str] = set()
    while node and node in mapping and node not in seen:
        seen.add(node)
        chain.append(node)
        node = mapping[node].get("parent")
    chain = chain[::-1] if chain else list(mapping.keys())

    msgs: list[tuple[str, str, Any]] = []
    for nid in chain:
        m = (mapping.get(nid) or {}).get("message")
        if not isinstance(m, dict):
            continue
        role = (m.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        if (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        text = _text(m.get("content")).strip()
        if not text:
            continue
        msgs.append((role, text, m.get("create_time")))
    return msgs


def _turns(msgs: list[tuple[str, str, Any]]) -> list[dict[str, Any]]:
    paired: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for role, text, ts in msgs:
        if role == "user":
            if cur is not None:
                paired.append(cur)
            cur = {"user": text, "asst": "", "ts": ts}
        else:
            if cur is None:
                cur = {"user": "", "asst": "", "ts": ts}
            cur["asst"] += ("\n\n" if cur["asst"] else "") + text
    if cur is not None:
        paired.append(cur)

    turns: list[dict[str, Any]] = []
    for t in paired:
        user, asst = t["user"].strip(), t["asst"].strip()
        if not user and not asst:
            continue
        code_blocks = [
            {"language": (lang or "").strip() or None, "content": code.strip()}
            for lang, code in _FENCE_RE.findall(asst)
        ]
        urls = list(
            dict.fromkeys(u.rstrip(".,);") for u in _URL_RE.findall(f"{user} {asst}"))
        )
        turns.append(
            {
                "turn_index": len(turns),
                "user_message": user,
                "assistant_response": asst,
                "thinking": "",
                "tools": [],
                "timestamp": _iso(t["ts"]),
                "files": [],
                "urls": urls,
                "code_blocks": code_blocks,
            }
        )
    return turns


class ChatGptSource(ImportSource):
    key = "chatgpt"
    label = "ChatGPT export"

    def detect(self, filename: str, data: bytes) -> bool:
        convos = _load(data)
        if not convos:
            return False
        return any(_is_convo(c) for c in convos[:5])

    def parse_export(self, data: bytes) -> Iterable[dict[str, Any]]:
        for convo in _load(data) or []:
            if not _is_convo(convo):
                continue
            turns = _turns(_ordered_messages(convo))
            if not turns:
                continue
            raw = json.dumps(convo, sort_keys=True, ensure_ascii=False).encode("utf-8")
            cid = (
                convo.get("conversation_id")
                or convo.get("id")
                or hashlib.sha256(raw).hexdigest()[:16]
            )
            title = (convo.get("title") or _derive_title(turns)).strip()
            stamps = [t["timestamp"] for t in turns if t["timestamp"]]
            created = _iso(convo.get("create_time")) or (stamps[0] if stamps else None)
            updated = (
                _iso(convo.get("update_time"))
                or (stamps[-1] if stamps else None)
                or created
            )
            yield {
                "id": f"chatgpt-{cid}",
                "source": "chatgpt",
                "title": (title or "ChatGPT conversation")[:200],
                "workspace_id": None,
                "repository": None,
                "repo_path": None,
                "requester": None,
                "responder": "ChatGPT",
                "created_at": created,
                "updated_at": updated,
                "source_path": None,
                "content_hash": hashlib.sha256(raw).hexdigest(),
                "turns": turns,
                "metrics": _estimate_metrics(turns),
            }
