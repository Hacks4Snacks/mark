from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import db, embeddings, visibility

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
        clauses.append(f"COALESCE({prefix}updated_at, {prefix}created_at) >= ?")
        params.append(scope.date_from)
    if scope.date_to:
        clauses.append(f"COALESCE({prefix}updated_at, {prefix}created_at) <= ?")
        params.append(scope.date_to + "T23:59:59Z")
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


def _fts_query(q: str) -> str | None:
    tokens = [t for t in _TOKEN_RE.findall(q.lower()) if len(t) > 1]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"*' for t in tokens)


def _keyword_search(
    query: str, limit: int, scope: _SessionScope
) -> list[dict[str, Any]]:
    match = _fts_query(query)
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


def _vector_matrix():
    embedder = embeddings.get_embedder()
    with _vec_lock:
        ids: list[int] = []
        sessions: list[str] = []
        vectors: list[np.ndarray] = []
        conn = db.connect()
        try:
            conn.execute("BEGIN")
            cur = conn.cursor()
            fingerprint, generation = embeddings.index_state(cur)
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

    scope = _session_scope(only_ids=only_ids)
    kw = _keyword_search(query, limit * candidate_factor, scope)
    sem = _semantic_search(query, limit * candidate_factor, _scoped_session_ids(scope))
    fused, _meta = _fuse((kw, sem))
    if not fused:
        return []

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    chunks = _load_chunks([cid for cid, _ in ordered])

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

    sessions = _load_sessions([chunks[cid]["session_id"] for cid, _ in picked])
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
                "content": cm["content"],
                "score": round(score / max_score, 4),
                "title": s.get("title"),
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
        db.temporary_id_table(
            cur.connection, ids, id_type="INTEGER"
        ) as id_table,
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
        db.temporary_id_table(
            cur.connection, ids, id_type="INTEGER"
        ) as id_table,
    ):
        rows = cur.execute(
            "SELECT c.id, c.session_id, c.turn_index, c.source_type, c.content "
            f"FROM chunks c JOIN {id_table} scope ON scope.id = c.id"
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


def get_session(session_id: str) -> dict[str, Any] | None:
    with db.cursor() as cur:
        srow = cur.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not srow:
            return None
        session = dict(srow)
        turns = [
            dict(r)
            for r in cur.execute(
                "SELECT turn_index, user_message, assistant_response, thinking, tools, timestamp "
                "FROM turns WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            ).fetchall()
        ]
        files = [
            dict(r)
            for r in cur.execute(
                "SELECT file_path, tool_name, turn_index FROM session_files WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            ).fetchall()
        ]
        refs = [
            dict(r)
            for r in cur.execute(
                "SELECT ref_type, ref_value, turn_index FROM session_refs WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        ]
        tag_rows = cur.execute(
            "SELECT tag, manual FROM tags WHERE session_id = ? "
            "ORDER BY manual DESC, score DESC",
            (session_id,),
        ).fetchall()
        tags = [r["tag"] for r in tag_rows]
        manual_tags = [r["tag"] for r in tag_rows if r["manual"]]
        doc = cur.execute(
            "SELECT kind, filename, mime, size_bytes, content FROM documents "
            "WHERE session_id = ? AND kind != 'attachment'",
            (session_id,),
        ).fetchone()
        attachments = [
            dict(r)
            for r in cur.execute(
                "SELECT id, filename, stored_path, mime, size_bytes, content, "
                "storage_kind, sha256, capture_version FROM documents "
                "WHERE session_id = ? AND kind = 'attachment' ORDER BY filename",
                (session_id,),
            ).fetchall()
        ]
    session["turns"] = turns
    session["files"] = files
    session["refs"] = refs
    session["tags"] = tags
    session["manual_tags"] = manual_tags
    session["document"] = dict(doc) if doc else None
    session["attachments"] = attachments
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
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]
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
