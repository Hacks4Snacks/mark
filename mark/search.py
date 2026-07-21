from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np

from . import config, db, embeddings, ingest, visibility

_RRF_K = 60
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

# Cached embedding matrix: rebuilt whenever the persisted semantic generation changes.
_vec_lock = threading.Lock()
_vec_cache: dict[str, Any] = {
    "key": None,
    "ids": None,
    "sessions": None,
    "matrix": None,
}


@dataclass(frozen=True)
class _SessionScope:
    repo: str | None
    source: str | None
    tags: tuple[str, ...]
    date_from: str | None
    date_to: str | None
    only_ids: frozenset[str] | None
    only_hidden: bool


def _session_scope(
    *,
    repo: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_ids: set[str] | None = None,
    only_hidden: bool = False,
) -> _SessionScope:
    normalized_tags = tuple(
        dict.fromkeys(tag.strip() for tag in (tags or []) if tag.strip())
    )
    return _SessionScope(
        repo=repo,
        source=source,
        tags=normalized_tags,
        date_from=date_from,
        date_to=date_to,
        only_ids=None if only_ids is None else frozenset(only_ids),
        only_hidden=only_hidden,
    )


def _scope_where(
    scope: _SessionScope, alias: str = "s", id_table: str | None = None
) -> tuple[str, list[Any]]:
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[Any] = []

    vclause, vparams = visibility.sql_where(alias, only_hidden=scope.only_hidden)
    clauses.append(vclause)
    params.extend(vparams)
    if scope.repo:
        clauses.append(f"{prefix}repository = ?")
        params.append(scope.repo)
    if scope.source:
        clauses.append(f"{prefix}source = ?")
        params.append(scope.source)
    if scope.date_from:
        clauses.append(
            f"julianday(COALESCE({prefix}updated_at, {prefix}created_at)) "
            ">= julianday(?)"
        )
        params.append(scope.date_from)
    if scope.date_to:
        next_day = (date.fromisoformat(scope.date_to) + timedelta(days=1)).isoformat()
        clauses.append(
            f"julianday(COALESCE({prefix}updated_at, {prefix}created_at)) "
            "< julianday(?)"
        )
        params.append(next_day)
    if scope.tags:
        placeholders = ",".join("?" * len(scope.tags))
        clauses.append(
            f"{prefix}id IN ("
            "SELECT scoped_tags.session_id FROM tags scoped_tags "
            f"WHERE scoped_tags.tag IN ({placeholders}) "
            "GROUP BY scoped_tags.session_id "
            "HAVING COUNT(DISTINCT scoped_tags.tag) = ?"
            ")"
        )
        params.extend(scope.tags)
        params.append(len(scope.tags))
    if scope.only_ids is not None:
        if id_table is None:
            raise ValueError("an ID scope requires a temporary table")
        clauses.append(f"{prefix}id IN (SELECT id FROM {id_table})")
    return " AND ".join(clauses), params


def _scoped_session_ids(scope: _SessionScope) -> set[str]:
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, scope.only_ids) as id_table,
    ):
        where, params = _scope_where(scope, id_table=id_table)
        return {
            row["id"]
            for row in cur.execute(
                f"SELECT s.id FROM sessions s WHERE {where}", params
            ).fetchall()
        }


def _recent_session_ids(scope: _SessionScope, limit: int) -> set[str]:
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, scope.only_ids) as id_table,
    ):
        where, params = _scope_where(scope, id_table=id_table)
        rows = cur.execute(
            f"SELECT s.id FROM sessions s WHERE {where} "
            "ORDER BY julianday(COALESCE(s.updated_at, s.created_at)) DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return {row["id"] for row in rows}


def _timestamp_value(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return float("-inf")


def _fts_query(q: str, *, column: str | None = None) -> str | None:
    tokens = [t for t in _TOKEN_RE.findall(q.lower()) if len(t) > 1]
    if not tokens:
        return None
    expression = " OR ".join(f'"{t}"*' for t in tokens)
    return f"{column} : ({expression})" if column else expression


def _keyword_search(
    query: str,
    limit: int,
    scope: _SessionScope,
    *,
    content_only: bool = False,
) -> list[dict[str, Any]]:
    match = _fts_query(query, column="content" if content_only else None)
    if not match:
        return []
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, scope.only_ids) as id_table,
    ):
        where, scope_params = _scope_where(scope, id_table=id_table)
        sql = (
            "SELECT search_index.chunk_id, search_index.session_id, "
            "  search_index.turn_index, "
            "  snippet(search_index, 0, '\x02', '\x03', '...', 14) AS snip, "
            "  bm25(search_index) AS score "
            "FROM search_index "
            "JOIN sessions s ON s.id = search_index.session_id "
            f"WHERE search_index MATCH ? AND {where} "
            "ORDER BY score LIMIT ?"
        )
        rows = cur.execute(sql, [match, *scope_params, limit]).fetchall()
    # bm25 returns more-negative = better; rank ascending.
    return [
        {
            "chunk_id": r["chunk_id"],
            "session_id": r["session_id"],
            "turn_index": r["turn_index"],
            "snippet": r["snip"],
        }
        for r in rows
    ]


def scoped_session_ids(
    *,
    repo: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_ids: set[str] | None = None,
) -> set[str]:
    """Return the complete visible set for structured filters."""
    return _scoped_session_ids(
        _session_scope(
            repo=repo,
            source=source,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            only_ids=only_ids,
        )
    )


def keyword_session_ids(
    query: str,
    *,
    repo: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_ids: set[str] | None = None,
) -> set[str]:
    """Return every visible session containing an FTS match."""
    match = _fts_query(query)
    if not match:
        return set()
    scope = _session_scope(
        repo=repo,
        source=source,
        tags=tags,
        date_from=date_from,
        date_to=date_to,
        only_ids=only_ids,
    )
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, scope.only_ids) as id_table,
    ):
        where, params = _scope_where(scope, id_table=id_table)
        return {
            row["session_id"]
            for row in cur.execute(
                "SELECT DISTINCT search_index.session_id FROM search_index "
                "JOIN sessions s ON s.id = search_index.session_id "
                f"WHERE search_index MATCH ? AND {where}",
                [match, *params],
            ).fetchall()
        }


def ranked_session_ids(
    query: str,
    *,
    mode: str,
    limit: int,
    repo: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_ids: set[str] | None = None,
) -> tuple[list[str], bool]:
    """Return server-bounded relevance-ranked IDs and whether more were eligible."""
    scope = _session_scope(
        repo=repo,
        source=source,
        tags=tags,
        date_from=date_from,
        date_to=date_to,
        only_ids=only_ids,
    )
    keyword_ranked = _keyword_session_ranking(query, scope) if mode == "hybrid" else []
    semantic_ranked = _semantic_session_ranking(query, _scoped_session_ids(scope))
    scores: dict[str, float] = {}
    for ranked in (keyword_ranked, semantic_ranked):
        for rank, session_id in enumerate(ranked):
            scores[session_id] = scores.get(session_id, 0.0) + 1.0 / (_RRF_K + rank)
    ordered = sorted(scores, key=lambda session_id: scores[session_id], reverse=True)
    return ordered[:limit], len(ordered) > limit


def _keyword_session_ranking(query: str, scope: _SessionScope) -> list[str]:
    """Rank every matching session by its best BM25 chunk."""
    match = _fts_query(query)
    if not match:
        return []
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, scope.only_ids) as id_table,
    ):
        where, params = _scope_where(scope, id_table=id_table)
        rows = cur.execute(
            "SELECT search_index.session_id, bm25(search_index) score "
            "FROM search_index JOIN sessions s ON s.id = search_index.session_id "
            f"WHERE search_index MATCH ? AND {where} ORDER BY score",
            [match, *params],
        )
        ranked: list[str] = []
        seen: set[str] = set()
        for row in rows:
            session_id = row["session_id"]
            if session_id not in seen:
                seen.add(session_id)
                ranked.append(session_id)
    return ranked


def _semantic_session_ranking(query: str, allowed_sessions: set[str]) -> list[str]:
    """Rank every vector-backed session by its best chunk similarity."""
    if not allowed_sessions:
        return []
    _ids, sessions, matrix = _vector_matrix()
    if matrix.shape[0] == 0:
        return []
    eligible_indices = np.fromiter(
        (
            index
            for index, session_id in enumerate(sessions)
            if session_id in allowed_sessions
        ),
        dtype=np.int64,
    )
    if eligible_indices.size == 0:
        return []
    similarities = matrix[eligible_indices] @ embeddings.embed_texts([query])[0]
    best: dict[str, float] = {}
    for index, similarity in zip(eligible_indices, similarities, strict=False):
        session_id = sessions[int(index)]
        best[session_id] = max(best.get(session_id, -1.0), float(similarity))
    return sorted(best, key=lambda session_id: best[session_id], reverse=True)


def _vector_matrix():
    with _vec_lock:
        ids: list[int] = []
        sessions: list[str] = []
        vectors: list[np.ndarray] = []
        conn = db.connect()
        try:
            conn.execute("BEGIN")
            cur = conn.cursor()
            fingerprint, generation = embeddings.index_state(cur)
            if not fingerprint or not ingest.semantic_verified():
                key = f"inactive:{generation}"
                matrix = np.zeros((0, 0), dtype=np.float32)
                _vec_cache.update(key=key, ids=ids, sessions=sessions, matrix=matrix)
                return ids, sessions, matrix
            embedder = embeddings.get_embedder()
            key = f"{embedder.fingerprint}:{generation}"
            if _vec_cache["key"] == key:
                return (
                    _vec_cache["ids"],
                    _vec_cache["sessions"],
                    _vec_cache["matrix"],
                )
            if fingerprint == embedder.fingerprint:
                for r in cur.execute(
                    "SELECT chunk_id, session_id, vector FROM embeddings "
                    "WHERE fingerprint = ? AND model = ? AND dim = ? "
                    "AND length(vector) = ?",
                    (
                        embedder.fingerprint,
                        embedder.name,
                        embedder.dim,
                        embedder.dim * 4,
                    ),
                ):
                    ids.append(r["chunk_id"])
                    sessions.append(r["session_id"])
                    vectors.append(embeddings.from_blob(r["vector"]))
        finally:
            conn.close()
        matrix = (
            np.vstack(vectors)
            if vectors
            else np.zeros((0, embedder.dim), dtype=np.float32)
        )
        _vec_cache.update(key=key, ids=ids, sessions=sessions, matrix=matrix)
        return ids, sessions, matrix


def _semantic_search(
    query: str, limit: int, allowed_sessions: set[str]
) -> list[dict[str, Any]]:
    if not allowed_sessions:
        return []
    ids, sessions, matrix = _vector_matrix()
    if matrix.shape[0] == 0:
        return []
    eligible = np.fromiter(
        (session_id in allowed_sessions for session_id in sessions),
        dtype=np.bool_,
        count=len(sessions),
    )
    eligible_indices = np.flatnonzero(eligible)
    if eligible_indices.size == 0:
        return []
    qvec = embeddings.embed_texts([query])[0]
    sims = matrix[eligible_indices] @ qvec
    k = min(limit, len(eligible_indices))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    return [
        {
            "chunk_id": ids[eligible_indices[i]],
            "session_id": sessions[eligible_indices[i]],
            "turn_index": None,
            "snippet": None,
            "sim": float(sims[i]),
        }
        for i in top
    ]


def _make_snippet(content: str, query: str, width: int = 240) -> str:
    tokens = [t for t in _TOKEN_RE.findall(query.lower()) if len(t) > 1]
    low = content.lower()
    pos = -1
    for t in tokens:
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0:
        snippet = content[:width]
    else:
        start = max(0, pos - width // 3)
        snippet = ("..." if start else "") + content[start : start + width]
    out = html.escape(snippet)
    for t in sorted(set(tokens), key=len, reverse=True):
        out = re.sub(
            f"({re.escape(html.escape(t))})",
            r"<mark>\1</mark>",
            out,
            flags=re.IGNORECASE,
        )
    return out + ("..." if len(content) > width else "")


def _render_fts_snippet(raw: str) -> str:
    return html.escape(raw).replace("\x02", "<mark>").replace("\x03", "</mark>")


def _fuse(
    ranked_lists: tuple[list[dict[str, Any]], ...],
) -> tuple[dict[int, float], dict[int, dict[str, Any]]]:
    """Reciprocal Rank Fusion over chunk-level result lists.

    Returns the fused score per ``chunk_id`` plus the first-seen item metadata for
    each chunk, so callers can either collapse to sessions or keep passages.
    """
    fused: dict[int, float] = {}
    meta: dict[int, dict[str, Any]] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            cid = item["chunk_id"]
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
            meta.setdefault(cid, item)
    return fused, meta


def search(
    query: str,
    *,
    mode: str = "hybrid",
    repo: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "recent",
    limit: int = 30,
    only_ids: set[str] | None = None,
    only_hidden: bool = False,
) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return browse(
            repo=repo,
            source=source,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            limit=limit,
            only_ids=only_ids,
            only_hidden=only_hidden,
        )

    scope = _session_scope(
        repo=repo,
        source=source,
        tags=tags,
        date_from=date_from,
        date_to=date_to,
        only_ids=only_ids,
        only_hidden=only_hidden,
    )
    kw = (
        _keyword_search(query, limit * 6, scope)
        if mode in ("hybrid", "keyword")
        else []
    )
    sem = (
        _semantic_search(query, limit * 6, _scoped_session_ids(scope))
        if mode in ("hybrid", "semantic")
        else []
    )

    # Reciprocal Rank Fusion at the chunk level.
    fused, meta = _fuse((kw, sem))

    # Best chunk per session.
    by_session: dict[str, tuple[float, int]] = {}
    for cid, score in fused.items():
        sid = meta[cid]["session_id"]
        if sid not in by_session or score > by_session[sid][0]:
            by_session[sid] = (score, cid)

    ordered = sorted(by_session.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]
    if not ordered:
        return []

    sids = [sid for sid, _ in ordered]
    sessions = _load_sessions(sids, only_hidden=only_hidden)
    chunk_text = _load_chunk_text([cid for _, (_, cid) in ordered])

    results = []
    max_score = ordered[0][1][0] or 1.0
    for sid, (score, cid) in ordered:
        s = sessions.get(sid)
        if not s:
            continue
        item = meta[cid]
        snippet = (
            _render_fts_snippet(item["snippet"])
            if item.get("snippet")
            else _make_snippet(chunk_text.get(cid, ""), query)
        )
        s = dict(s)
        s["score"] = round(score / max_score, 4)
        s["snippet"] = snippet
        results.append(s)
    return _sort_results(results, sort)


def search_passages(
    query: str,
    *,
    limit: int = 40,
    per_session_cap: int = 3,
    only_ids: set[str] | None = None,
    candidate_factor: int = 6,
    repo: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    recency: str = "none",
    recent_session_limit: int = 20,
    mode: str = "hybrid",
) -> list[dict[str, Any]]:
    """Return the top matching *passages* (chunks) for RAG context assembly.

    Unlike :func:`search`, this does not collapse to one row per session: up to
    ``per_session_cap`` passages from the same session may be returned, so a long
    on-topic session can contribute more than a single excerpt. Each result
    carries the chunk's own text and turn index plus its parent session's display
    metadata, ranked by fused keyword+semantic relevance.
    """
    query = (query or "").strip()
    if not query:
        return []

    scope = _session_scope(
        repo=repo,
        date_from=date_from,
        date_to=date_to,
        only_ids=only_ids,
    )
    kw = (
        _keyword_search(
            query,
            limit * candidate_factor,
            scope,
            content_only=True,
        )
        if mode in ("hybrid", "keyword")
        else []
    )
    sem = (
        _semantic_search(query, limit * candidate_factor, _scoped_session_ids(scope))
        if mode in ("hybrid", "semantic")
        else []
    )
    recent: list[dict[str, Any]] = []
    if recency in ("boost", "latest"):
        recent_ids = _recent_session_ids(scope, recent_session_limit)
        recent_scope = _session_scope(
            repo=repo,
            date_from=date_from,
            date_to=date_to,
            only_ids=recent_ids,
        )
        recent_kw = (
            _keyword_search(
                query,
                limit * candidate_factor,
                recent_scope,
                content_only=True,
            )
            if mode in ("hybrid", "keyword")
            else []
        )
        recent_sem = (
            _semantic_search(
                query,
                limit * candidate_factor,
                _scoped_session_ids(recent_scope),
            )
            if mode in ("hybrid", "semantic")
            else []
        )
        recent_scores, recent_meta = _fuse((recent_kw, recent_sem))
        recent = [
            recent_meta[chunk_id]
            for chunk_id in sorted(
                recent_scores,
                key=lambda candidate_id: recent_scores[candidate_id],
                reverse=True,
            )
        ]
    fused, _meta = _fuse((kw, sem, recent))
    if not fused:
        return []

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    chunks = _load_chunks([cid for cid, _ in ordered])
    sessions = _load_sessions(list({chunk["session_id"] for chunk in chunks.values()}))
    if recency == "latest":
        ordered.sort(
            key=lambda item: (
                _timestamp_value(
                    sessions.get(chunks[item[0]]["session_id"], {}).get("updated_at")
                    or sessions.get(chunks[item[0]]["session_id"], {}).get("created_at")
                ),
                item[1],
            ),
            reverse=True,
        )

    picked: list[tuple[int, float]] = []
    per_session: dict[str, int] = {}
    for cid, score in ordered:
        cm = chunks.get(cid)
        if not cm:
            continue
        sid = cm["session_id"]
        if only_ids is not None and sid not in only_ids:
            continue
        if per_session.get(sid, 0) >= per_session_cap:
            continue
        per_session[sid] = per_session.get(sid, 0) + 1
        picked.append((cid, score))
        if len(picked) >= limit:
            break
    if not picked:
        return []

    max_score = picked[0][1] or 1.0
    passages: list[dict[str, Any]] = []
    for cid, score in picked:
        cm = chunks[cid]
        s = sessions.get(cm["session_id"])
        if not s:  # hidden or vanished session — skip
            continue
        passages.append(
            {
                "chunk_id": cid,
                "session_id": cm["session_id"],
                "turn_index": cm["turn_index"],
                "source_type": cm["source_type"],
                "timestamp": cm.get("timestamp")
                or s.get("updated_at")
                or s.get("created_at"),
                "content": cm["content"],
                "score": round(score / max_score, 4),
                "title": s.get("title"),
                "summary": s.get("summary"),
                "source": s.get("source"),
                "repository": s.get("repository"),
                "updated_at": s.get("updated_at") or s.get("created_at"),
            }
        )
    return passages


def _sort_results(results: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Reorder relevance-ranked results when the user picks an explicit sort.

    ``recent`` is treated as "keep relevance order" for keyword/semantic queries
    so the default search experience still surfaces the best matches first.
    """
    if sort == "oldest":
        # "~" sorts after digits so undated sessions land last, mirroring browse.
        return sorted(
            results, key=lambda r: r.get("updated_at") or r.get("created_at") or "~"
        )
    if sort == "turns":
        return sorted(results, key=lambda r: r.get("turn_count") or 0, reverse=True)
    if sort == "duration":

        def duration_key(result: dict[str, Any]) -> tuple[bool, float, int, str, str]:
            raw_duration = result.get("duration_seconds")
            duration = (
                float(raw_duration) if isinstance(raw_duration, (int, float)) else 0.0
            )
            raw_turn_count = result.get("turn_count")
            turn_count = raw_turn_count if isinstance(raw_turn_count, int) else 0
            return (
                raw_duration is None,
                -duration,
                -turn_count,
                result.get("updated_at") or result.get("created_at") or "~",
                result.get("id") or "",
            )

        return sorted(
            results,
            key=duration_key,
        )
    if sort == "title":
        return sorted(results, key=lambda r: (r.get("title") or "").lower())
    return results


def browse(
    *,
    repo: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sort: str = "recent",
    limit: int = 50,
    only_ids: set[str] | None = None,
    only_hidden: bool = False,
) -> list[dict[str, Any]]:
    scope = _session_scope(
        repo=repo,
        source=source,
        tags=tags,
        date_from=date_from,
        date_to=date_to,
        only_ids=only_ids,
        only_hidden=only_hidden,
    )
    order = {
        "recent": "COALESCE(s.updated_at, s.created_at) DESC",
        # nulls last so undated sessions don't masquerade as the "oldest".
        "oldest": (
            "COALESCE(s.updated_at, s.created_at) IS NULL, "
            "COALESCE(s.updated_at, s.created_at) ASC"
        ),
        "turns": "s.turn_count DESC",
        "duration": (
            "s.duration_seconds IS NULL ASC, s.duration_seconds DESC, "
            "s.turn_count DESC, "
            "COALESCE(s.updated_at, s.created_at) ASC, s.id ASC"
        ),
        "title": "s.title COLLATE NOCASE ASC",
    }.get(sort, "COALESCE(s.updated_at, s.created_at) DESC")

    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, scope.only_ids) as id_table,
    ):
        where, params = _scope_where(scope, id_table=id_table)
        sql = f"SELECT s.* FROM sessions s WHERE {where}"
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    attach_tags(rows)
    for r in rows:
        r["score"] = None
        r["snippet"] = html.escape((r.get("summary") or "")[:240])
    return rows


def _load_sessions(
    ids: list[str], *, only_hidden: bool = False
) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    vclause, vparams = visibility.sql_where("s", only_hidden=only_hidden)
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, ids) as id_table,
    ):
        rows = [
            dict(r)
            for r in cur.execute(
                f"SELECT s.* FROM sessions s JOIN {id_table} scope ON scope.id = s.id "
                f"WHERE {vclause}",
                vparams,
            ).fetchall()
        ]
    attach_tags(rows)
    return {r["id"]: r for r in rows}


def attach_tags(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ids = [r["id"] for r in rows]
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, ids) as id_table,
    ):
        tag_rows = cur.execute(
            "SELECT t.session_id, t.tag FROM tags t "
            f"JOIN {id_table} scope ON scope.id = t.session_id "
            "ORDER BY t.score DESC"
        ).fetchall()
    by_sid: dict[str, list[str]] = {}
    for tr in tag_rows:
        by_sid.setdefault(tr["session_id"], []).append(tr["tag"])
    for r in rows:
        r["tags"] = by_sid.get(r["id"], [])


def _load_chunk_text(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, ids, id_type="INTEGER") as id_table,
    ):
        rows = cur.execute(
            "SELECT c.id, c.content FROM chunks c "
            f"JOIN {id_table} scope ON scope.id = c.id"
        ).fetchall()
    return {r["id"]: r["content"] for r in rows}


def _load_chunks(ids: list[int]) -> dict[int, dict[str, Any]]:
    """Load chunk rows (id, session, turn, type, content) keyed by chunk id."""
    if not ids:
        return {}
    with (
        db.cursor() as cur,
        db.temporary_id_table(cur.connection, ids, id_type="INTEGER") as id_table,
    ):
        rows = cur.execute(
            "SELECT c.id, c.session_id, c.turn_index, c.source_type, c.content, "
            "t.timestamp FROM chunks c "
            f"JOIN {id_table} scope ON scope.id = c.id "
            "LEFT JOIN turns t ON t.session_id = c.session_id "
            "AND t.turn_index = c.turn_index"
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def facets() -> dict[str, Any]:
    vclause, vparams = visibility.sql_where()
    sclause, sparams = visibility.sql_where("s")
    with db.cursor() as cur:
        repos = [
            {"name": r["repository"], "count": r["n"]}
            for r in cur.execute(
                "SELECT repository, COUNT(*) n FROM sessions "
                f"WHERE repository IS NOT NULL AND {vclause} "
                "GROUP BY repository ORDER BY n DESC",
                vparams,
            ).fetchall()
        ]
        tags = [
            {"tag": r["tag"], "count": r["n"]}
            for r in cur.execute(
                "SELECT t.tag, COUNT(*) n FROM tags t "
                f"JOIN sessions s ON s.id = t.session_id AND {sclause} "
                "GROUP BY t.tag ORDER BY n DESC LIMIT 40",
                sparams,
            ).fetchall()
        ]
        sources = [
            {"source": r["source"], "count": r["n"]}
            for r in cur.execute(
                f"SELECT source, COUNT(*) n FROM sessions WHERE {vclause} "
                "GROUP BY source",
                vparams,
            ).fetchall()
        ]
        rng = cur.execute(
            "SELECT MIN(COALESCE(created_at, updated_at)) mn, "
            "MAX(COALESCE(updated_at, created_at)) mx FROM sessions "
            f"WHERE {vclause}",
            vparams,
        ).fetchone()
    return {
        "repositories": repos,
        "tags": tags,
        "sources": sources,
        "date_min": rng["mn"],
        "date_max": rng["mx"],
    }


def _session_turns(
    cur,
    session_id: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    defer_above: int | None = None,
) -> list[dict[str, Any]]:
    if defer_above is None:
        fields = "user_message, assistant_response, thinking"
        params: list[Any] = [session_id]
    else:
        length_expr = (
            "length(CAST(COALESCE(user_message, '') AS BLOB)) + "
            "length(CAST(COALESCE(assistant_response, '') AS BLOB)) + "
            "length(CAST(COALESCE(thinking, '') AS BLOB))"
        )
        fields = (
            f"CASE WHEN {length_expr} <= ? THEN user_message END AS user_message, "
            f"CASE WHEN {length_expr} <= ? THEN assistant_response END "
            "AS assistant_response, "
            f"CASE WHEN {length_expr} <= ? THEN thinking END AS thinking, "
            f"{length_expr} AS content_chars"
        )
        params = [defer_above, defer_above, defer_above, session_id]
    sql = (
        f"SELECT turn_index, {fields}, tools, timestamp FROM turns "
        "WHERE session_id = ? ORDER BY turn_index"
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend((limit, offset))
    return [dict(row) for row in cur.execute(sql, params).fetchall()]


def get_session_turns(
    session_id: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    defer_above: int | None = None,
) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        return _session_turns(
            cur,
            session_id,
            offset=offset,
            limit=limit,
            defer_above=defer_above,
        )


def get_session_turn(session_id: str, turn_index: int) -> dict[str, Any] | None:
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT turn_index, user_message, assistant_response, thinking, tools, "
            "timestamp FROM turns WHERE session_id = ? AND turn_index = ?",
            (session_id, turn_index),
        ).fetchone()
    return dict(row) if row else None


def get_session_files(
    session_id: str, *, offset: int = 0, limit: int
) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        return [
            dict(row)
            for row in cur.execute(
                "SELECT file_path, tool_name, turn_index FROM session_files "
                "WHERE session_id = ? ORDER BY turn_index, file_path LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        ]


def get_session_refs(
    session_id: str, *, offset: int = 0, limit: int
) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        return [
            dict(row)
            for row in cur.execute(
                "SELECT ref_type, ref_value, turn_index FROM session_refs "
                "WHERE session_id = ? AND ref_type = 'url' "
                "ORDER BY turn_index, ref_value LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        ]


def get_session_attachments(
    session_id: str, *, offset: int = 0, limit: int
) -> list[dict[str, Any]]:
    with db.cursor() as cur:
        return [
            dict(row)
            for row in cur.execute(
                "SELECT id, filename, stored_path, mime, size_bytes, NULL AS content, "
                "storage_kind, sha256, capture_version FROM documents "
                "WHERE session_id = ? AND kind = 'attachment' "
                "ORDER BY filename, id LIMIT ? OFFSET ?",
                (session_id, limit, offset),
            ).fetchall()
        ]


def get_session(
    session_id: str,
    *,
    turns_offset: int = 0,
    turns_limit: int | None = None,
    defer_turns_above: int | None = None,
    defer_document_above: int | None = None,
) -> dict[str, Any] | None:
    with db.cursor() as cur:
        srow = cur.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not srow:
            return None
        session = dict(srow)
        turns = _session_turns(
            cur,
            session_id,
            offset=turns_offset,
            limit=turns_limit,
            defer_above=defer_turns_above,
        )
        files = [
            dict(r)
            for r in cur.execute(
                "SELECT file_path, tool_name, turn_index FROM session_files "
                "WHERE session_id = ? ORDER BY turn_index, file_path LIMIT ?",
                (session_id, config.DETAIL_FILE_LIMIT),
            ).fetchall()
        ]
        refs = [
            dict(r)
            for r in cur.execute(
                "SELECT ref_type, ref_value, turn_index FROM session_refs "
                "WHERE session_id = ? AND ref_type = 'url' "
                "ORDER BY turn_index, ref_value LIMIT ?",
                (session_id, config.DETAIL_LINK_LIMIT),
            ).fetchall()
        ]
        tag_rows = cur.execute(
            "SELECT tag, manual FROM tags WHERE session_id = ? "
            "ORDER BY manual DESC, score DESC",
            (session_id,),
        ).fetchall()
        tags = [r["tag"] for r in tag_rows]
        manual_tags = [r["tag"] for r in tag_rows if r["manual"]]
        if defer_document_above is None:
            doc = cur.execute(
                "SELECT kind, filename, mime, size_bytes, content, "
                "length(CAST(COALESCE(content, '') AS BLOB)) AS content_chars "
                "FROM documents "
                "WHERE session_id = ? AND kind != 'attachment'",
                (session_id,),
            ).fetchone()
        else:
            doc = cur.execute(
                "SELECT kind, filename, mime, size_bytes, "
                "CASE WHEN length(CAST(COALESCE(content, '') AS BLOB)) <= ? "
                "THEN content END AS content, "
                "length(CAST(COALESCE(content, '') AS BLOB)) AS content_chars "
                "FROM documents WHERE session_id = ? AND kind != 'attachment'",
                (defer_document_above, session_id),
            ).fetchone()
        attachments = [
            dict(r)
            for r in cur.execute(
                "SELECT id, filename, stored_path, mime, size_bytes, NULL AS content, "
                "storage_kind, sha256, capture_version FROM documents "
                "WHERE session_id = ? AND kind = 'attachment' "
                "ORDER BY filename, id LIMIT ?",
                (session_id, config.DETAIL_ATTACHMENT_LIMIT),
            ).fetchall()
        ]
        counts = cur.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM session_files WHERE session_id = ?) files_total, "
            "(SELECT COUNT(*) FROM session_refs "
            " WHERE session_id = ? AND ref_type = 'url') refs_total, "
            "(SELECT COUNT(*) FROM documents "
            " WHERE session_id = ? AND kind = 'attachment') attachments_total",
            (session_id, session_id, session_id),
        ).fetchone()
    session["turns"] = turns
    session["files"] = files
    session["refs"] = refs
    session["tags"] = tags
    session["manual_tags"] = manual_tags
    session["document"] = dict(doc) if doc else None
    session["attachments"] = attachments
    session["files_total"] = int(counts["files_total"])
    session["refs_total"] = int(counts["refs_total"])
    session["attachments_total"] = int(counts["attachments_total"])
    return session


def related_sessions(session_id: str, limit: int = 8) -> list[dict[str, Any]]:
    """Semantically nearest other sessions, by best-matching chunk similarity.

    Builds a query vector from the session's own chunk embeddings (their mean),
    then ranks every other session by its single best-matching chunk. Returns
    lightweight cards the detail view can link to.
    """
    _ids, sessions, matrix = _vector_matrix()
    if matrix.shape[0] == 0:
        return []
    own = [i for i, s in enumerate(sessions) if s == session_id]
    if not own:
        return []
    qvec = matrix[own].mean(axis=0)
    norm = float(np.linalg.norm(qvec))
    if norm == 0:
        return []
    sims = matrix @ (qvec / norm)

    best: dict[str, float] = {}
    for i, sid in enumerate(sessions):
        if sid == session_id:
            continue
        v = float(sims[i])
        if v > best.get(sid, -2.0):
            best[sid] = v
    if not best:
        return []
    visible_ids = visibility.filter_visible(best)
    ranked = sorted(
        ((sid, score) for sid, score in best.items() if sid in visible_ids),
        key=lambda kv: kv[1],
        reverse=True,
    )[:limit]
    if not ranked:
        return []
    order = [sid for sid, _ in ranked]

    vclause, vparams = visibility.sql_where()
    with db.cursor() as cur:
        rows = {
            r["id"]: r
            for r in cur.execute(
                "SELECT id, title, source, repository, created_at, updated_at "
                f"FROM sessions WHERE id IN ({','.join('?' * len(order))}) "
                f"AND {vclause}",
                [*order, *vparams],
            ).fetchall()
        }
    out: list[dict[str, Any]] = []
    for sid, score in ranked:
        r = rows.get(sid)
        if not r:
            continue
        out.append(
            {
                "id": sid,
                "title": r["title"],
                "source": r["source"],
                "repository": r["repository"],
                "updated_at": r["updated_at"] or r["created_at"],
                "score": round(score, 3),
            }
        )
    return out
