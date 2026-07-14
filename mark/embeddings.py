from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path

import numpy as np

from . import config

INDEX_SCHEMA_VERSION = 1
HASH_ALGORITHM_VERSION = 1
TRANSFORMER_ALGORITHM_VERSION = 1
FINGERPRINT_META_KEY = "embed_fingerprint"
TARGET_FINGERPRINT_META_KEY = "embed_target_fingerprint"
GENERATION_META_KEY = "embed_generation"

_lock = threading.Lock()
_writer_lock = threading.RLock()
_writer_local = threading.local()
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
    backend: str = "builtin-hash"

    def embed(self, texts: Sequence[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    @property
    def algorithm_version(self) -> int:
        return (
            HASH_ALGORITHM_VERSION
            if self.kind == "builtin"
            else TRANSFORMER_ALGORITHM_VERSION
        )

    @property
    def fingerprint(self) -> str:
        """Stable identity for vectors produced by this embedder contract."""
        return json.dumps(
            {
                "schema": INDEX_SCHEMA_VERSION,
                "kind": self.kind,
                "backend": self.backend,
                "model": self.name,
                "dim": self.dim,
                "algorithm": self.algorithm_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class _FastEmbed(Embedder):
    kind = "transformer"
    backend = "fastembed"

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
        probe = next(
            iter(
                self._model.embed(
                    ["dimension probe"], batch_size=config.EMBED_BATCH_SIZE
                )
            )
        )
        self.dim = int(np.asarray(probe).shape[-1])

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = np.asarray(
            list(self._model.embed(list(texts), batch_size=config.EMBED_BATCH_SIZE)),
            dtype=np.float32,
        )
        return _normalize(vecs)


class _Model2Vec(Embedder):
    kind = "transformer"
    backend = "model2vec"

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
    backend = "builtin-hash"

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


def index_state(cur) -> tuple[str | None, int]:
    rows = {
        row["key"]: row["value"]
        for row in cur.execute(
            "SELECT key, value FROM meta WHERE key IN (?, ?)",
            (FINGERPRINT_META_KEY, GENERATION_META_KEY),
        )
    }
    try:
        generation = int(rows.get(GENERATION_META_KEY, "0"))
    except (TypeError, ValueError):
        generation = 0
    return rows.get(FINGERPRINT_META_KEY), generation


def set_index_fingerprint(cur, embedder: Embedder) -> None:
    cur.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (FINGERPRINT_META_KEY, embedder.fingerprint),
    )
    # Keep the legacy display/status field until API consumers migrate.
    cur.execute(
        "INSERT INTO meta(key, value) VALUES('embed_model', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (embedder.name,),
    )


def _set_meta(cur, key: str, value: str) -> None:
    cur.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def target_fingerprint(cur) -> str | None:
    row = cur.execute(
        "SELECT value FROM meta WHERE key = ?", (TARGET_FINGERPRINT_META_KEY,)
    ).fetchone()
    return row["value"] if row else None


def index_is_active(cur, embedder: Embedder) -> bool:
    current, _generation = index_state(cur)
    return current == embedder.fingerprint


def mark_index_dirty(cur) -> None:
    """Deactivate a complete index in the same transaction as chunk mutation."""
    current, _generation = index_state(cur)
    target = target_fingerprint(cur)
    if current is not None:
        if target is None:
            _set_meta(cur, TARGET_FINGERPRINT_META_KEY, current)
        cur.execute("DELETE FROM meta WHERE key = ?", (FINGERPRINT_META_KEY,))
        _set_meta(cur, "embed_model", "")
        bump_generation(cur)
    _set_meta(cur, "embed_pending", "1")


def prepare_index(cur, embedder: Embedder) -> bool:
    """Prepare an incompatible rebuild without exposing partial new vectors."""
    current, _generation = index_state(cur)
    if current == embedder.fingerprint:
        cur.execute("DELETE FROM meta WHERE key = ?", (TARGET_FINGERPRINT_META_KEY,))
        set_index_fingerprint(cur, embedder)
        return False
    if target_fingerprint(cur) == embedder.fingerprint:
        stale = cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE fingerprint IS NOT ?",
            (embedder.fingerprint,),
        ).fetchone()[0]
        if stale:
            cur.execute(
                "DELETE FROM embeddings WHERE fingerprint IS NOT ?",
                (embedder.fingerprint,),
            )
            bump_generation(cur)
        return False
    cur.execute("DELETE FROM embeddings")
    cur.execute("DELETE FROM meta WHERE key = ?", (FINGERPRINT_META_KEY,))
    _set_meta(cur, TARGET_FINGERPRINT_META_KEY, embedder.fingerprint)
    _set_meta(cur, "embed_pending", "1")
    cur.execute(
        "INSERT INTO meta(key, value) VALUES('embed_model', '') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    )
    bump_generation(cur)
    return True


def activate_index(cur, embedder: Embedder) -> int:
    """Publish a fully rebuilt target fingerprint and invalidate readers."""
    target = target_fingerprint(cur)
    if target != embedder.fingerprint:
        raise RuntimeError("semantic target changed during rebuild")
    incompatible = cur.execute(
        "SELECT 1 FROM embeddings WHERE fingerprint IS NOT ? OR model != ? "
        "OR dim != ? OR length(vector) != ? LIMIT 1",
        (
            embedder.fingerprint,
            embedder.name,
            embedder.dim,
            embedder.dim * 4,
        ),
    ).fetchone()
    if incompatible is not None:
        raise RuntimeError("semantic rebuild contains incompatible vectors")
    missing = cur.execute(
        "SELECT 1 FROM ("
        "  SELECT c.id, ROW_NUMBER() OVER "
        "         (PARTITION BY c.session_id ORDER BY c.id) AS rn "
        "  FROM chunks c"
        ") r LEFT JOIN embeddings e ON e.chunk_id = r.id "
        "AND e.fingerprint = ? AND e.model = ? AND e.dim = ? "
        "AND length(e.vector) = ? "
        "WHERE e.chunk_id IS NULL AND r.rn <= ? LIMIT 1",
        (
            embedder.fingerprint,
            embedder.name,
            embedder.dim,
            embedder.dim * 4,
            config.MAX_EMBED_CHUNKS_PER_SESSION,
        ),
    ).fetchone()
    if missing is not None:
        raise RuntimeError("semantic rebuild is incomplete")
    set_index_fingerprint(cur, embedder)
    cur.execute("DELETE FROM meta WHERE key = ?", (TARGET_FINGERPRINT_META_KEY,))
    _set_meta(cur, "embed_pending", "0")
    return bump_generation(cur)


def bump_generation(cur) -> int:
    _fingerprint, generation = index_state(cur)
    generation += 1
    cur.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (GENERATION_META_KEY, str(generation)),
    )
    return generation


@contextmanager
def writer_lock() -> Iterator[None]:
    """Serialize semantic producers across threads and Mark processes."""
    with _writer_lock:
        depth = getattr(_writer_local, "depth", 0)
        if depth:
            _writer_local.depth = depth + 1
            try:
                yield
            finally:
                _writer_local.depth -= 1
            return
        lock_path = Path(f"{config.DB_PATH}.semantic.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except ImportError as exc:  # pragma: no cover - Windows fallback
                raise RuntimeError(
                    "cross-process semantic locking unavailable"
                ) from exc
            _writer_local.depth = 1
            yield
        finally:
            _writer_local.depth = 0
            try:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class _CrossEncoderReranker:
    """Cross-encoder relevance scorer (fastembed).

    A cross-encoder reads the query and a candidate *together*, so it ranks a
    small set of retrieved passages far more accurately than the bi-encoder
    cosine similarity used for first-stage recall.
    """

    def __init__(self) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # type: ignore

        self.name = config.RERANK_MODEL
        # Reuse the embedding CPU cap so a first-time rerank (model download +
        # ONNX init) doesn't peg every core.
        self._model = TextCrossEncoder(
            model_name=config.RERANK_MODEL,
            threads=config.EMBED_THREADS or None,
        )

    def scores(self, query: str, documents: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, list(documents))]


_rerank_lock = threading.Lock()
_reranker: _CrossEncoderReranker | None = None
_reranker_ready = False


def get_reranker() -> _CrossEncoderReranker | None:
    """Lazily construct the cross-encoder reranker, or ``None`` when unavailable.

    The outcome (including failure) is cached so the expensive model download and
    ONNX session init are only attempted once per process.
    """
    global _reranker, _reranker_ready
    if _reranker_ready:
        return _reranker
    with _rerank_lock:
        if _reranker_ready:
            return _reranker
        if config.ASK_RERANK:
            try:
                _reranker = _CrossEncoderReranker()
            except Exception:  # missing dep, download failure, runtime issue
                _reranker = None
        _reranker_ready = True
        return _reranker


def rerank(query: str, documents: Sequence[str]) -> list[float] | None:
    """Relevance scores aligned with ``documents`` (higher = better).

    Returns ``None`` when no reranker backend is available so callers can fall
    back to their existing ordering.
    """
    if not documents:
        return []
    rr = get_reranker()
    if rr is None:
        return None
    try:
        scores = rr.scores(query, documents)
    except Exception:
        return None
    return scores if len(scores) == len(documents) else None
