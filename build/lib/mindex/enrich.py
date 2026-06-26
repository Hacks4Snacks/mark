"""Local, offline enrichment: extractive summaries and keyword tags.

No LLM and no network calls. Summaries are built by ranking sentences against
the conversation's own centroid embedding; tags use a KeyBERT-style rerank of
candidate phrases against that same centroid. Whatever embedding backend is
active (transformer or the built-in vectorizer) is reused here.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import numpy as np

from . import embeddings

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_SYNTAX_RE = re.compile(r"[#>*_~|]+")
_WS_RE = re.compile(r"\s+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.#_-]{1,}")

_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "if",
    "then",
    "this",
    "that",
    "these",
    "those",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "as",
    "at",
    "by",
    "it",
    "its",
    "i",
    "you",
    "we",
    "they",
    "can",
    "will",
    "would",
    "should",
    "could",
    "do",
    "does",
    "did",
    "not",
    "no",
    "from",
    "so",
    "my",
    "your",
    "me",
    "he",
    "she",
    "them",
    "there",
    "here",
    "what",
    "which",
    "who",
    "when",
    "where",
    "how",
    "why",
    "all",
    "any",
    "some",
    "more",
    "most",
    "other",
    "into",
    "than",
    "too",
    "very",
    "just",
    "also",
    "use",
    "using",
    "used",
    "want",
    "need",
    "like",
    "get",
    "got",
    "make",
    "made",
    "let",
    "please",
    "okay",
    "ok",
    "yes",
    "thanks",
    "thank",
    "now",
    "out",
    "up",
    "about",
    "have",
    "has",
    "had",
    "may",
    "might",
    "must",
    "one",
    "two",
    "see",
    "add",
    "added",
}

# Generic words that recur across many sessions and make poor topic tags.
_STOP |= {
    "session",
    "sessions",
    "file",
    "files",
    "task",
    "tasks",
    "work",
    "output",
    "result",
    "results",
    "continue",
    "prior",
    "specified",
    "based",
    "step",
    "steps",
    "following",
    "current",
    "given",
    "above",
    "below",
    "via",
}


def _plaintext(markdown: str) -> str:
    if not markdown:
        return ""
    text = _FENCE_RE.sub(" ", markdown)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_SYNTAX_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for raw in _SENT_RE.split(text):
        s = raw.strip()
        if 25 <= len(s) <= 320:
            out.append(s)
    return out


def _summarize(turns: list[dict[str, Any]]) -> str:
    lead = ""
    for t in turns:
        if t.get("user_message"):
            lead = _plaintext(t["user_message"]).split(". ")[0].strip()
            lead = lead[:200]
            break

    blob = "\n".join(
        f"{t.get('user_message', '')} {_plaintext(t.get('assistant_response', ''))}"
        for t in turns
    )
    sents = _sentences(blob)
    if not sents:
        return lead

    emb = embeddings.get_embedder()
    vecs = emb.embed(sents)
    centroid = vecs.mean(axis=0, keepdims=True)
    centroid /= np.linalg.norm(centroid) or 1.0
    scores = (vecs @ centroid.T).ravel()

    ranked = sorted(range(len(sents)), key=lambda i: scores[i], reverse=True)
    chosen: list[int] = []
    for i in ranked:
        if any(_overlap(sents[i], sents[j]) > 0.6 for j in chosen):
            continue
        chosen.append(i)
        if len(chosen) >= 3:
            break
    chosen.sort()

    chosen_sents = [sents[i] for i in chosen]
    pieces: list[str] = []
    if lead and not any(s.startswith(lead) for s in chosen_sents):
        pieces.append(lead)
    pieces += [s for s in chosen_sents if s != lead]
    summary = " ".join(pieces)
    return (summary[:380].rstrip() + "...") if len(summary) > 380 else summary


def _overlap(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _candidates(text: str, max_candidates: int = 30) -> list[tuple[str, float]]:
    words = [w.strip(".-_#+") for w in _WORD_RE.findall(text.lower())]
    filt = [
        w
        for w in words
        if w and w not in _STOP and len(w) > 2 and not w.replace(".", "").isdigit()
    ]
    unigrams = Counter(filt)
    bigrams = Counter(f"{a} {b}" for a, b in zip(filt, filt[1:]))
    raw: dict[str, float] = {}
    for w, c in unigrams.items():
        raw[w] = float(c)
    for bg, c in bigrams.items():
        if c >= 2:
            raw[bg] = c * 1.5  # phrases are more informative
    if not raw:
        return []
    top = sorted(raw.items(), key=lambda kv: kv[1], reverse=True)[:max_candidates]
    mx = max(c for _, c in top)
    return [(term, c / mx) for term, c in top]


def _keywords(text: str, top_k: int = 6) -> list[tuple[str, float]]:
    cands = _candidates(text)
    if not cands:
        return []
    terms = [t for t, _ in cands]
    freq = {t: f for t, f in cands}

    emb = embeddings.get_embedder()
    doc_vec = emb.embed([text[:4000]])
    term_vecs = emb.embed(terms)
    sims = (term_vecs @ doc_vec.T).ravel()
    sims = (sims - sims.min()) / (np.ptp(sims) or 1.0)

    scored = [(t, 0.6 * float(sims[i]) + 0.4 * freq[t]) for i, t in enumerate(terms)]
    scored.sort(key=lambda x: x[1], reverse=True)

    selected: list[tuple[str, float]] = []
    for term, score in scored:
        if any(term in s or s in term for s, _ in selected):
            continue
        selected.append((term, round(score, 3)))
        if len(selected) >= top_k:
            break
    return selected


def enrich_session(
    title: str, turns: list[dict[str, Any]], *, light: bool = False
) -> tuple[str, list[tuple[str, float]]]:
    corpus = f"{title}\n" + "\n".join(
        f"{t.get('user_message', '')} {_plaintext(t.get('assistant_response', ''))}"
        for t in turns
    )
    if light:
        # No embedding calls — used for bulk indexing of many sessions.
        return _summarize_fast(turns), _keywords_freq(corpus)
    return _summarize(turns), _keywords(corpus)


def _summarize_fast(turns: list[dict[str, Any]]) -> str:
    """Cheap extractive summary: opening intent + first substantive reply."""
    lead = ""
    for t in turns:
        if t.get("user_message"):
            lead = _plaintext(t["user_message"]).strip()
            break
    lead = lead.split(". ")[0][:160].strip()

    body = ""
    for t in turns:
        plain = _plaintext(t.get("assistant_response", ""))
        if len(plain) > 40:
            sents = _sentences(plain) or [plain]
            body = " ".join(sents[:2])
            break

    summary = " ".join(
        p for p in (lead, body) if p and not (body and lead and body.startswith(lead))
    )
    summary = summary or lead
    return (summary[:380].rstrip() + "...") if len(summary) > 380 else summary


def _keywords_freq(text: str, top_k: int = 6) -> list[tuple[str, float]]:
    """Frequency-only keyphrases (no embeddings)."""
    cands = _candidates(text)
    selected: list[tuple[str, float]] = []
    for term, score in cands:
        if any(term in s or s in term for s, _ in selected):
            continue
        selected.append((term, round(score, 3)))
        if len(selected) >= top_k:
            break
    return selected


def enrich_text(title: str, text: str) -> tuple[str, list[tuple[str, float]]]:
    """Enrichment for uploaded documents/notes."""
    turn = [{"user_message": title, "assistant_response": text}]
    summary = _summarize(turn)
    tags = _keywords(f"{title}\n{_plaintext(text)}")
    return summary, tags
