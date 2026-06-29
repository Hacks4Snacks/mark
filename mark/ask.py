from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from . import config, db, embeddings, search

_STATUS_TIMEOUT = 2.5
_SHOW_TIMEOUT = 5.0
_GEN_TIMEOUT = 180

# Rough characters-per-token, used only to turn the model's token window into a
# character budget for packing context. Deliberately low so we under-fill rather
# than overflow num_ctx (real text runs ~3.5-4 chars/token).
_CHARS_PER_TOKEN = 3.5

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


def _post_json(
    url: str, payload: dict[str, Any], timeout: float = _STATUS_TIMEOUT
) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
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


def _model_num_ctx(model: str) -> int | None:
    """Best-effort trained context length for an Ollama model via /api/show."""
    try:
        data = _post_json(
            config.OLLAMA_URL + "/api/show", {"name": model}, _SHOW_TIMEOUT
        )
    except (urllib.error.URLError, OSError, ValueError):
        return None
    info = data.get("model_info") or {}
    for key, val in info.items():
        if key.endswith(".context_length") and isinstance(val, int) and val > 0:
            return val
    return None


def _effective_num_ctx(model: str) -> int:
    """Context window mark will actually request: the model's own trained length,
    clamped to a configured ceiling so a 128K model can't blow up RAM/latency."""
    probed = _model_num_ctx(model) or config.ASK_DEFAULT_NUM_CTX
    return max(2048, min(probed, config.ASK_NUM_CTX_CAP))


def _truncate(text: str, cap: int) -> str:
    text = (text or "").strip()
    if cap <= 0 or len(text) <= cap:
        return text
    return text[:cap].rstrip() + " …"


def _format_turn(turn: dict[str, Any], cap: int) -> str:
    parts: list[str] = []
    um = (turn.get("user_message") or "").strip()
    ar = (turn.get("assistant_response") or "").strip()
    if um:
        parts.append("User: " + um)
    if ar:
        parts.append("Assistant: " + ar)
    return _truncate("\n".join(parts), cap)


def _load_turns_map(session_ids: list[str]) -> dict[str, dict[int, dict[str, Any]]]:
    """Fetch turns for the given sessions, keyed by session then turn index, so a
    matched passage can be widened with its neighbouring turns."""
    if not session_ids:
        return {}
    out: dict[str, dict[int, dict[str, Any]]] = {}
    with db.cursor() as cur:
        placeholders = ",".join("?" * len(session_ids))
        rows = cur.execute(
            "SELECT session_id, turn_index, user_message, assistant_response "
            f"FROM turns WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
    for r in rows:
        out.setdefault(r["session_id"], {})[r["turn_index"]] = {
            "user_message": r["user_message"],
            "assistant_response": r["assistant_response"],
        }
    return out


def _rerank(question: str, passages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder passages by cross-encoder relevance when a reranker is available;
    otherwise keep the fused retrieval order."""
    if len(passages) <= 1:
        return passages
    scores = embeddings.rerank(question, [p["content"] for p in passages])
    if not scores:
        return passages
    order = sorted(range(len(passages)), key=lambda i: scores[i], reverse=True)
    return [passages[i] for i in order]


def _passage_body(
    passage: dict[str, Any],
    turns_map: dict[str, dict[int, dict[str, Any]]],
    radius: int,
    turn_cap: int,
    emitted_chunks: set[int],
    emitted_turns: set[tuple[str, int]],
) -> str:
    """Render one passage as context: its matched slice, widened by up to
    ``radius`` neighbouring turns, with already-emitted text de-duplicated."""
    cid = passage["chunk_id"]
    sid = passage["session_id"]
    ti = passage["turn_index"]
    emitted_chunks.add(cid)
    # Document/note chunks have no turn structure: use the chunk text as-is.
    if ti is None:
        return _truncate(passage["content"], turn_cap)

    turns = turns_map.get(sid, {})
    parts: list[str] = []
    # Preceding context turns.
    for j in range(ti - radius, ti):
        if (sid, j) in emitted_turns or j not in turns:
            continue
        txt = _format_turn(turns[j], turn_cap)
        if txt:
            parts.append(txt)
            emitted_turns.add((sid, j))
    # The matched slice itself (chunks are already 'User:'/'Assistant:'-prefixed).
    parts.append(_truncate(passage["content"], turn_cap))
    emitted_turns.add((sid, ti))
    # Following context turns.
    for j in range(ti + 1, ti + radius + 1):
        if (sid, j) in emitted_turns or j not in turns:
            continue
        txt = _format_turn(turns[j], turn_cap)
        if txt:
            parts.append(txt)
            emitted_turns.add((sid, j))
    return "\n".join(x for x in parts if x)


def build_context(
    question: str,
    *,
    char_budget: int,
    max_sessions: int,
    session_ids: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Assemble a citation context from the most relevant *passages*.

    Retrieval is passage-level: the actual matched chunks (plus a little
    neighbouring-turn context), reranked, then packed in relevance order until
    ``char_budget`` is exhausted. ``max_sessions`` caps how many distinct
    sessions may be cited (breadth); depth is governed by the budget, not a fixed
    per-source slice. Multiple passages from one session share its citation
    number, and the answer can draw on far more grounded context than the old
    "first few turns of N sessions" approach.
    """
    passages = search.search_passages(
        question,
        limit=config.ASK_MAX_CANDIDATE_PASSAGES,
        per_session_cap=config.ASK_PER_SESSION_PASSAGES,
        only_ids=session_ids,
    )
    if not passages:
        return "", []
    passages = _rerank(question, passages)

    radius = config.ASK_NEIGHBOR_TURNS
    turn_cap = config.ASK_MAX_TURN_CHARS
    turns_map = (
        _load_turns_map(list({p["session_id"] for p in passages})) if radius > 0 else {}
    )

    cite: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    emitted_chunks: set[int] = set()
    emitted_turns: set[tuple[str, int]] = set()
    blocks: list[str] = []
    used = 0
    sep = "\n\n---\n\n"

    for p in passages:
        sid = p["session_id"]
        if p["chunk_id"] in emitted_chunks:
            continue
        # Cap breadth: don't open a NEW session once the source budget is full,
        # but keep enriching sessions already cited.
        if sid not in cite and len(cite) >= max_sessions:
            continue

        body = _passage_body(
            p, turns_map, radius, turn_cap, emitted_chunks, emitted_turns
        )
        if not body:
            continue

        if sid not in cite:
            n = len(cite) + 1
            cite[sid] = n
            sources.append(
                {
                    "n": n,
                    "id": sid,
                    "title": p.get("title"),
                    "source": p.get("source"),
                    "repository": p.get("repository"),
                    "updated_at": p.get("updated_at"),
                }
            )
        header = (
            f"[{cite[sid]}] {p.get('title') or 'Untitled'} "
            f"(source={p.get('source')}, repo={p.get('repository') or '-'})\n"
        )
        block = header + body
        cost = len(block) + len(sep)
        if blocks and used + cost > char_budget:
            break  # budget full; passages are relevance-ordered so stop here.
        if not blocks and cost > char_budget:
            # Always include at least the single best passage, trimmed to fit.
            block = header + _truncate(body, max(0, char_budget - len(header)))
        blocks.append(block)
        used += cost

    return sep.join(blocks), sources


def stream_answer(
    question: str,
    limit: int | None = None,
    session_ids: set[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield events: {sources}, then {token}* , then {done} or {error}.

    ``limit`` caps how many distinct sessions the answer may cite; the amount of
    context per answer is governed by the model's own context window, not by
    ``limit``.
    """
    st = status()
    if not st["available"] or not st["model"]:
        yield {
            "type": "error",
            "error": "Ollama is not running locally. Start it with `ollama serve`.",
        }
        return

    model = st["model"]
    num_ctx = _effective_num_ctx(model)
    prompt_tokens = max(1024, num_ctx - config.ASK_RESERVE_OUTPUT_TOKENS)
    # ~10% slack for the system prompt, the question, and chat-template tokens.
    char_budget = max(2000, int(prompt_tokens * 0.9 * _CHARS_PER_TOKEN))
    max_sessions = limit if (limit and limit > 0) else config.ASK_DEFAULT_SOURCES

    context, sources = build_context(
        question,
        char_budget=char_budget,
        max_sessions=max_sessions,
        session_ids=session_ids,
    )
    yield {"type": "sources", "sources": sources}
    if not context.strip():
        yield {
            "type": "token",
            "text": "I couldn't find anything relevant in your archive for that question.",
        }
        yield {"type": "done", "model": model}
        return

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": f"Question: {question}\n\nContext from your past conversations:\n\n{context}",
            },
        ],
        "stream": True,
        # Match num_ctx to the model so retrieved context isn't silently truncated
        # (Ollama otherwise defaults to ~2048).
        "options": {"num_ctx": num_ctx},
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
    yield {"type": "done", "model": model}
