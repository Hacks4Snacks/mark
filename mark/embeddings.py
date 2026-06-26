from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Sequence
from itertools import pairwise

import numpy as np

from . import config

_lock = threading.Lock()
_embedder: Embedder | None = None

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
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
    "from",
    "so",
    "my",
    "your",
    "me",
    "he",
    "she",
    "them",
}


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


class Embedder:
    """Common interface over the active embedding backend."""

    name: str = "builtin-hash"
    dim: int = config.HASH_EMBED_DIM
    kind: str = "builtin"  # 'transformer' | 'builtin'

    def embed(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


class _FastEmbed(Embedder):
    kind = "transformer"

    def __init__(self) -> None:
        from fastembed import TextEmbedding  # type: ignore

        self.name = config.EMBED_MODEL
        # Cap ONNX inference threads so a first-time index doesn't peg every core
        # (fastembed defaults to all logical CPUs). 0 -> None = use all cores.
        self._model = TextEmbedding(
            model_name=config.EMBED_MODEL,
            threads=config.EMBED_THREADS or None,
        )
        # Probe dimensionality once.
        probe = next(iter(self._model.embed(["dimension probe"])))
        self.dim = int(np.asarray(probe).shape[-1])

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = np.asarray(list(self._model.embed(list(texts))), dtype=np.float32)
        return _normalize(vecs)


class _Model2Vec(Embedder):
    kind = "transformer"

    def __init__(self) -> None:
        from model2vec import StaticModel  # type: ignore

        self.name = "minishlab/potion-base-8M"
        self._model = StaticModel.from_pretrained(self.name)
        self.dim = int(self._model.dim)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = np.asarray(self._model.encode(list(texts)), dtype=np.float32)
        return _normalize(vecs)


class _HashEmbed(Embedder):
    """Stateless feature-hashing vectorizer (word + character n-grams).

    Not a transformer, but a genuine vector space: paraphrases that share words
    or sub-words land near each other, which already beats raw substring search
    and works with zero downloads on any Python version.
    """

    name = "builtin-hash"
    kind = "builtin"

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or config.HASH_EMBED_DIM

    def _tokens(self, text: str) -> list[str]:
        text = text.lower()
        words = [w for w in _WORD_RE.findall(text) if w not in _STOP and len(w) > 1]
        toks: list[str] = list(words)
        # word bigrams capture short phrases
        toks += [f"{a}_{b}" for a, b in pairwise(words)]
        # character n-grams give sub-word / typo robustness
        joined = " ".join(words)
        for n in (3, 4, 5):
            toks += [
                f"#{joined[i : i + n]}"
                for i in range(0, max(0, len(joined) - n + 1), 2)
            ]
        return toks

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        counts: dict[int, float] = {}
        signs: dict[int, float] = {}
        for tok in self._tokens(text):
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if (h[4] & 1) else -1.0
            counts[idx] = counts.get(idx, 0.0) + 1.0
            signs[idx] = sign
        for idx, c in counts.items():
            v[idx] = signs[idx] * (1.0 + np.log(c))  # sublinear term frequency
        return v

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        mat = np.vstack([self._vec(t or "") for t in texts])
        return _normalize(mat)


def _build() -> Embedder:
    for factory in (_FastEmbed, _Model2Vec):
        try:
            emb = factory()
            return emb
        except Exception:  # ImportError, model download failure, runtime issues
            continue
    return _HashEmbed()


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                _embedder = _build()
    return _embedder


def embed_texts(texts: Sequence[str]) -> np.ndarray:
    return get_embedder().embed(texts)


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
