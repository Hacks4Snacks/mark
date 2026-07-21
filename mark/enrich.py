from __future__ import annotations

import re
from collections import Counter
from itertools import pairwise
from typing import Any

import numpy as np

from . import config, embeddings

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_SYNTAX_RE = re.compile(r"[#>*_~|]+")
_WS_RE = re.compile(r"\s+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.#_-]{1,}")
_TOPIC_CONTEXT_RE = re.compile(
    r"<(?P<tag>[A-Za-z][A-Za-z0-9_-]*(?:context|info|instructions|memory|"
    r"tools|attachments?))\b[^>]*>.*?</(?P=tag)>",
    re.I | re.S,
)
_TOPIC_ORPHAN_CONTEXT_RE = re.compile(
    r"</?[A-Za-z][A-Za-z0-9_-]*(?:context|info|instructions|memory|tools|"
    r"attachments?)\b[^>]*>",
    re.I,
)
_TOPIC_URL_RE = re.compile(r"\b(?:https?|file)://[^\s<>'\"]+", re.I)
_TOPIC_PATH_RE = re.compile(r"(?<![\w.-])(?:~?/|[A-Za-z]:[\\/])[^\s<>'\"]+")
_TOPIC_RELATIVE_PATH_RE = re.compile(
    r"(?<![\w.-])(?:(?:\.\.?|[A-Za-z0-9_.-]+)[\\/])+"
    r"[A-Za-z0-9_.-]+\.(?:css|db|go|html|ini|java|js|json|jsx|log|md|py|sh|"
    r"sql|swift|toml|ts|tsx|txt|xml|yaml|yml)(?![\w.-])",
    re.I,
)
_TOPIC_FILE_SUFFIXES = {
    "css",
    "db",
    "go",
    "html",
    "ini",
    "java",
    "js",
    "json",
    "jsx",
    "log",
    "md",
    "py",
    "sh",
    "sql",
    "swift",
    "toml",
    "ts",
    "tsx",
    "txt",
    "xml",
    "yaml",
    "yml",
}
_TOPIC_HOST_SUFFIXES = {"com", "dev", "io", "net", "org"}
_DOTTED_TECH_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\.(?:io|js|net)$")
_SHORT_TOPIC_TERMS = {"ai", "ca", "db", "ip", "os", "ui", "vm"}
_ENVIRONMENT_TERM_RE = re.compile(r"^(?:dev|stg|stage|prod|test|qa)\d+(?:-\d+)?$", re.I)

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
_STOP.update(
    {
        "session",
        "sessions",
        "file",
        "files",
        "path",
        "paths",
        "filepath",
        "directory",
        "directories",
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
        "hack",
        "http",
        "https",
        "users",
        "www",
        "command",
        "concept",
        "design",
        "env",
        "however",
        "include",
        "including",
        "involving",
        "poc",
        "supporting",
        "working",
    }
)

_CLOSING_SENTENCE_RE = re.compile(
    r"^(?:no problem(?: at all)?|you(?:'re| are) welcome|happy to help|"
    r"glad (?:i|we) could help|let me know if (?:you )?(?:need|want)|"
    r"feel free to (?:ask|reach out))\b",
    re.I,
)


def _plaintext(markdown: str) -> str:
    if not markdown:
        return ""
    text = _FENCE_RE.sub(" ", markdown)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_SYNTAX_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _topic_plaintext(text: str) -> str:
    text = _TOPIC_CONTEXT_RE.sub(" ", text or "")
    text = _TOPIC_ORPHAN_CONTEXT_RE.sub(" ", text)
    text = _TOPIC_URL_RE.sub(" ", text)
    text = _TOPIC_PATH_RE.sub(" ", text)
    text = _TOPIC_RELATIVE_PATH_RE.sub(" ", text)
    return _plaintext(text)


def _clip_words(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    clipped = text[:cap].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return clipped or text[:cap].rstrip()


def _topic_term_is_noise(term: str, occurrences: int = 1) -> bool:
    if occurrences >= 2 and _DOTTED_TECH_RE.fullmatch(term):
        return False
    for token in term.split():
        lowered = token.lower().strip(".-_#")
        if not lowered:
            return True
        if _ENVIRONMENT_TERM_RE.fullmatch(lowered):
            return True
        if "." not in lowered:
            continue
        suffix = lowered.rsplit(".", 1)[-1]
        if suffix in _TOPIC_FILE_SUFFIXES or suffix in _TOPIC_HOST_SUFFIXES:
            return True
    return False


def _topic_segments(title: str, turns: list[dict[str, Any]]) -> list[tuple[str, float]]:
    title_text = _topic_plaintext(title)
    parts: list[tuple[str, float]] = [(title_text, 3.0)] if title_text else []
    for turn in turns:
        user = _clip_words(_topic_plaintext(turn.get("user_message", "")), 1600)
        assistant = _clip_words(
            _topic_plaintext(turn.get("assistant_response", "")), 1000
        )
        if user:
            parts.append((user, 2.0))
        if assistant:
            parts.append((assistant, 1.0))
    return parts


def _topic_corpus(title: str, turns: list[dict[str, Any]]) -> str:
    return "\n".join(text for text, _weight in _topic_segments(title, turns))


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
            lead = _topic_plaintext(t["user_message"]).split(". ")[0].strip()
            lead = lead[:200]
            break

    blob = "\n".join(
        f"{_topic_plaintext(t.get('user_message', ''))} "
        f"{_topic_plaintext(t.get('assistant_response', ''))}"
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


def _topic_words(text: str) -> list[str]:
    words = [w.strip(".-_#+") for w in _WORD_RE.findall(_topic_plaintext(text).lower())]
    return [
        word
        for word in words
        if word
        and word not in _STOP
        and (len(word) > 2 or word in _SHORT_TOPIC_TERMS)
        and len(word) <= config.MAX_TAG_CHARS
        and not word.replace(".", "").isdigit()
    ]


def _candidate_segments(
    segments: list[tuple[str, float]], max_candidates: int = 30
) -> list[tuple[str, float]]:
    unigrams: dict[str, float] = {}
    bigrams: dict[str, float] = {}
    occurrences: Counter[str] = Counter()
    for text, weight in segments:
        words = _topic_words(text)
        for word, word_count in Counter(words).items():
            unigrams[word] = unigrams.get(word, 0.0) + float(word_count) * weight
            occurrences[word] += word_count
        for phrase, phrase_count in Counter(
            f"{first} {second}" for first, second in pairwise(words)
        ).items():
            bigrams[phrase] = bigrams.get(phrase, 0.0) + float(phrase_count) * weight
            occurrences[phrase] += phrase_count
    raw: dict[str, float] = {}
    for w, c in unigrams.items():
        if not _topic_term_is_noise(w, occurrences[w]):
            raw[w] = float(c)
    for bg, c in bigrams.items():
        if occurrences[bg] >= 2 and not _topic_term_is_noise(bg, occurrences[bg]):
            raw[bg] = c * 2.5  # repeated phrases are stronger topics than unigrams
    if not raw:
        return []
    top = sorted(raw.items(), key=lambda kv: kv[1], reverse=True)[:max_candidates]
    mx = max(c for _, c in top)
    return [(term, c / mx) for term, c in top]


def _candidates(text: str, max_candidates: int = 30) -> list[tuple[str, float]]:
    return _candidate_segments([(text, 1.0)], max_candidates)


def _keywords(text: str, top_k: int = 6) -> list[tuple[str, float]]:
    cands = _candidates(text)
    if not cands:
        return []
    terms = [t for t, _ in cands]
    freq = dict(cands)

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
    title: str, turns: list[dict[str, Any]]
) -> tuple[str, list[tuple[str, float]]]:
    """Fast, embedding-free summary + keyphrases for bulk session indexing."""
    return _summarize_fast(turns), _keywords_freq_segments(
        _topic_segments(title, turns)
    )


def _summarize_fast(turns: list[dict[str, Any]]) -> str:
    """Cheap extractive summary: opening intent + first substantive reply."""
    lead = ""
    for t in turns:
        if t.get("user_message"):
            candidate = _topic_plaintext(t["user_message"]).strip()
            if candidate:
                lead = candidate
                break
    lead = _clip_words(lead.split(". ")[0], 160)

    body = ""
    for t in reversed(turns):
        plain = _topic_plaintext(t.get("assistant_response", ""))
        if len(plain) > 40:
            sentences = _sentences(plain) or [plain]
            substantive = [
                sentence
                for sentence in sentences
                if not _CLOSING_SENTENCE_RE.match(sentence)
            ]
            if not substantive:
                continue
            body = " ".join(substantive[:2])
            break

    summary = " ".join(
        p for p in (lead, body) if p and not (body and lead and body.startswith(lead))
    )
    summary = summary or lead
    return (_clip_words(summary, 377) + "...") if len(summary) > 380 else summary


def _keywords_freq(text: str, top_k: int = 6) -> list[tuple[str, float]]:
    """Frequency-only keyphrases (no embeddings)."""
    cands = _candidates(text)
    return _select_keywords(cands, top_k)


def _keywords_freq_segments(
    segments: list[tuple[str, float]], top_k: int = 6
) -> list[tuple[str, float]]:
    return _select_keywords(_candidate_segments(segments), top_k)


def _select_keywords(
    candidates: list[tuple[str, float]], top_k: int
) -> list[tuple[str, float]]:
    selected: list[tuple[str, float]] = []
    for term, score in candidates:
        if any(term in s or s in term for s, _ in selected):
            continue
        selected.append((term, round(score, 3)))
        if len(selected) >= top_k:
            break
    return selected


def enrich_text(title: str, text: str) -> tuple[str, list[tuple[str, float]]]:
    """Embedding-free enrichment for uploaded documents/notes."""
    turn = [{"user_message": title, "assistant_response": text}]
    summary = _summarize_fast(turn)
    tags = _keywords_freq(f"{title}\n{_plaintext(text)}")
    return summary, tags
