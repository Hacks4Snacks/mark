from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from collections.abc import Iterator

from . import config, search

_STATUS_TIMEOUT = 2.5
_GEN_TIMEOUT = 180

_SYSTEM = (
    "You are Mark, answering a question about the user's OWN past AI coding "
    "conversations. Use ONLY the provided context excerpts, and answer ONLY the "
    "specific question asked — do not summarise or comment on excerpts that are "
    "unrelated to the question. Cite the sources you actually rely on with their "
    "bracket numbers, e.g. [1], [2]. If the context does not contain the answer, "
    "say so plainly in one sentence rather than guessing. Be concise and practical."
)


def _get_json(url: str, timeout: float = _STATUS_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _pick_model(models: list[str]) -> str | None:
    if not models:
        return None
    pref = config.OLLAMA_MODEL
    if pref:
        for m in models:
            if m == pref or m.split(":")[0] == pref.split(":")[0]:
                return m
    # Prefer a small, fast, general-purpose model when several are installed.
    for want in (
        "llama3.2",
        "llama3.1",
        "qwen2.5",
        "mistral",
        "gemma",
        "phi",
        "llama3",
    ):
        for m in models:
            if m.split(":")[0].startswith(want):
                return m
    return models[0]


def status() -> dict[str, Any]:
    """Probe the local Ollama server; report availability + chosen model."""
    url = config.OLLAMA_URL
    try:
        tags = _get_json(url + "/api/tags")
        models = [m["name"] for m in tags.get("models", [])]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return {"available": False, "url": url, "models": [], "model": None}
    return {
        "available": bool(models),
        "url": url,
        "models": models,
        "model": _pick_model(models),
    }


def _session_excerpt(s: dict[str, Any], max_chars: int = 1800) -> str:
    parts: list[str] = []
    for t in (s.get("turns") or [])[:8]:
        if t.get("user_message"):
            parts.append("User: " + t["user_message"].strip())
        if t.get("assistant_response"):
            parts.append("Assistant: " + t["assistant_response"].strip())
    if not parts and (s.get("document") or {}).get("content"):
        parts.append(s["document"]["content"].strip())
    text = "\n".join(parts)
    return text[:max_chars]


def build_context(
    question: str, limit: int = 6, session_ids: set[str] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Retrieve the most relevant sessions and assemble a citation context."""
    results = search.search(question, mode="hybrid", limit=limit, only_ids=session_ids)
    # Keep the whole context within a local model's window by sharing a fixed
    # character budget across however many sources were requested.
    per_source = max(700, min(1800, 22000 // max(len(results), 1)))
    blocks: list[str] = []
    sources: list[dict[str, Any]] = []
    for i, s in enumerate(results, 1):
        sid = s["id"]
        full = search.get_session(sid)
        excerpt = _session_excerpt(full, per_source) if full else ""
        blocks.append(
            f"[{i}] {s.get('title') or 'Untitled'} "
            f"(source={s.get('source')}, repo={s.get('repository') or '-'})\n{excerpt}"
        )
        sources.append(
            {
                "n": i,
                "id": sid,
                "title": s.get("title"),
                "source": s.get("source"),
                "repository": s.get("repository"),
                "updated_at": s.get("updated_at") or s.get("created_at"),
            }
        )
    return "\n\n---\n\n".join(blocks), sources


def stream_answer(
    question: str, limit: int = 6, session_ids: set[str] | None = None
) -> Iterator[dict[str, Any]]:
    """Yield events: {sources}, then {token}* , then {done} or {error}."""
    st = status()
    if not st["available"] or not st["model"]:
        yield {
            "type": "error",
            "error": "Ollama is not running locally. Start it with `ollama serve`.",
        }
        return

    context, sources = build_context(question, limit, session_ids=session_ids)
    yield {"type": "sources", "sources": sources}
    if not context.strip():
        yield {
            "type": "token",
            "text": "I couldn't find anything relevant in your archive for that question.",
        }
        yield {"type": "done", "model": st["model"]}
        return

    payload = {
        "model": st["model"],
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": f"Question: {question}\n\nContext from your past conversations:\n\n{context}",
            },
        ],
        "stream": True,
        # Ollama defaults num_ctx to ~2048, which would silently truncate our
        # retrieved context. Give the model room to actually read every source.
        "options": {"num_ctx": 8192},
    }
    req = urllib.request.Request(
        config.OLLAMA_URL + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_GEN_TIMEOUT) as r:
            for raw in r:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                content = (obj.get("message") or {}).get("content")
                if content:
                    yield {"type": "token", "text": content}
                if obj.get("done"):
                    break
    except (urllib.error.URLError, OSError, ValueError) as e:
        yield {"type": "error", "error": f"Ollama request failed: {e}"}
        return
    yield {"type": "done", "model": st["model"]}
