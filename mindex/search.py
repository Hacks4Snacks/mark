"""Hybrid search over the knowledge base.

Combines two complementary signals and fuses them with Reciprocal Rank Fusion:

* **keyword** — SQLite FTS5 (BM25) over chunk text. Precise term matching.
* **semantic** — cosine similarity of the query embedding against chunk vectors.
  Finds conversations by *meaning*, even when the words differ.

Results are returned grouped by session (the unit a user actually wants to
re-open), each with the best-matching snippet.
"""

from __future__ import annotations

import html
import re
import threading
from typing import Any

import numpy as np

from . import db, embeddings

_RRF_K = 60
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

# Cached embedding matrix: rebuilt only when row count or model changes.
_vec_lock = threading.Lock()
_vec_cache: dict[str, Any] = {
    "key": None,
    "ids": None,
    "sessions": None,
    "matrix": None,
}


# --- query helpers -----------------------------------------------------------


def _fts_query(q: str) -> str | None:
    tokens = [t for t in _TOKEN_RE.findall(q.lower()) if len(t) > 1]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"*' for t in tokens)


def _keyword_search(query: str, limit: int) -> list[dict[str, Any]]:
    match = _fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT chunk_id, session_id, turn_index, "
        "  snippet(search_index, 0, '\x02', '\x03', '…', 14) AS snip, "
        "  bm25(search_index) AS score "
        "FROM search_index WHERE search_index MATCH ? ORDER BY score LIMIT ?"
    )
    with db.cursor() as cur:
        rows = cur.execute(sql, (match, limit)).fetchall()
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
    model = embeddings.get_embedder().name
    with db.cursor() as cur:
        count = cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE model = ?", (model,)
        ).fetchone()[0]
    key = f"{model}:{count}"
    if _vec_cache["key"] == key:
        return _vec_cache["ids"], _vec_cache["sessions"], _vec_cache["matrix"]
    with _vec_lock:
        if _vec_cache["key"] == key:
            return _vec_cache["ids"], _vec_cache["sessions"], _vec_cache["matrix"]
        ids: list[int] = []
        sessions: list[str] = []
        vectors: list[np.ndarray] = []
        with db.cursor() as cur:
            for r in cur.execute(
                "SELECT chunk_id, session_id, vector FROM embeddings WHERE model = ?",
                (model,),
            ):
                ids.append(r["chunk_id"])
                sessions.append(r["session_id"])
                vectors.append(embeddings.from_blob(r["vector"]))
        matrix = (
            np.vstack(vectors)
            if vectors
            else np.zeros((0, embeddings.get_embedder().dim), dtype=np.float32)
        )
        _vec_cache.update(key=key, ids=ids, sessions=sessions, matrix=matrix)
        return ids, sessions, matrix


def _semantic_search(query: str, limit: int) -> list[dict[str, Any]]:
    ids, sessions, matrix = _vector_matrix()
    if matrix.shape[0] == 0:
        return []
    qvec = embeddings.embed_texts([query])[0]
    sims = matrix @ qvec
    k = min(limit, len(ids))
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top])]
    return [
        {
            "chunk_id": ids[i],
            "session_id": sessions[i],
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
        snippet = ("…" if start else "") + content[start : start + width]
    out = html.escape(snippet)
    for t in sorted(set(tokens), key=len, reverse=True):
        out = re.sub(
            f"({re.escape(html.escape(t))})",
            r"<mark>\1</mark>",
            out,
            flags=re.IGNORECASE,
        )
    return out + ("…" if len(content) > width else "")


def _render_fts_snippet(raw: str) -> str:
    return html.escape(raw).replace("\x02", "<mark>").replace("\x03", "</mark>")


# --- filtering ---------------------------------------------------------------


def _allowed_sessions(
    repo, source, tags, date_from, date_to, include_automation=False
) -> set[str] | None:
    clauses, params = [], []
    if repo:
        clauses.append("s.repository = ?")
        params.append(repo)
    if source:
        clauses.append("s.source = ?")
        params.append(source)
    elif not include_automation:
        clauses.append("s.source != 'automation'")
    if date_from:
        clauses.append("COALESCE(s.updated_at, s.created_at) >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("COALESCE(s.updated_at, s.created_at) <= ?")
        params.append(date_to + "T23:59:59Z")
    sql = "SELECT s.id FROM sessions s"
    if tags:
        sql += " JOIN tags t ON t.session_id = s.id AND t.tag IN (%s)" % ",".join(
            "?" * len(tags)
        )
        params = list(tags) + params
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if not tags and not clauses:
        return None  # no filtering
    with db.cursor() as cur:
        return {r["id"] for r in cur.execute(sql, params).fetchall()}


# --- public API --------------------------------------------------------------


def search(
    query: str,
    *,
    mode: str = "hybrid",
    repo: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_automation: bool = False,
    sort: str = "recent",
    limit: int = 30,
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
            include_automation=include_automation,
            limit=limit,
        )

    allowed = _allowed_sessions(
        repo, source, tags, date_from, date_to, include_automation
    )

    kw = _keyword_search(query, limit * 6) if mode in ("hybrid", "keyword") else []
    sem = _semantic_search(query, limit * 6) if mode in ("hybrid", "semantic") else []

    # Reciprocal Rank Fusion at the chunk level.
    fused: dict[int, float] = {}
    meta: dict[int, dict[str, Any]] = {}
    for ranked in (kw, sem):
        for rank, item in enumerate(ranked):
            cid = item["chunk_id"]
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
            meta.setdefault(cid, item)

    # Best chunk per session.
    by_session: dict[str, tuple[float, int]] = {}
    for cid, score in fused.items():
        sid = meta[cid]["session_id"]
        if allowed is not None and sid not in allowed:
            continue
        if sid not in by_session or score > by_session[sid][0]:
            by_session[sid] = (score, cid)

    ordered = sorted(by_session.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]
    if not ordered:
        return []

    sids = [sid for sid, _ in ordered]
    sessions = _load_sessions(sids)
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
        s["match_turn"] = item.get("turn_index")
        results.append(s)
    return _sort_results(results, sort)


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
    include_automation: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    allowed = _allowed_sessions(
        repo, source, tags, date_from, date_to, include_automation
    )
    order = {
        "recent": "COALESCE(updated_at, created_at) DESC",
        # nulls last so undated sessions don't masquerade as the "oldest".
        "oldest": "COALESCE(updated_at, created_at) IS NULL, COALESCE(updated_at, created_at) ASC",
        "turns": "turn_count DESC",
        "title": "title COLLATE NOCASE ASC",
    }.get(sort, "COALESCE(updated_at, created_at) DESC")

    sql = "SELECT * FROM sessions"
    params: list[Any] = []
    if allowed is not None:
        if not allowed:
            return []
        sql += " WHERE id IN (%s)" % ",".join("?" * len(allowed))
        params = list(allowed)
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(limit)

    with db.cursor() as cur:
        rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    _attach_tags(rows)
    for r in rows:
        r["score"] = None
        r["snippet"] = html.escape((r.get("summary") or "")[:240])
    return rows


def _load_sessions(ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    with db.cursor() as cur:
        rows = [
            dict(r)
            for r in cur.execute(
                "SELECT * FROM sessions WHERE id IN (%s)" % ",".join("?" * len(ids)),
                ids,
            ).fetchall()
        ]
    _attach_tags(rows)
    return {r["id"]: r for r in rows}


def _attach_tags(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    ids = [r["id"] for r in rows]
    with db.cursor() as cur:
        tag_rows = cur.execute(
            "SELECT session_id, tag FROM tags WHERE session_id IN (%s) ORDER BY score DESC"
            % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
    by_sid: dict[str, list[str]] = {}
    for tr in tag_rows:
        by_sid.setdefault(tr["session_id"], []).append(tr["tag"])
    for r in rows:
        r["tags"] = by_sid.get(r["id"], [])


def _load_chunk_text(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT id, content FROM chunks WHERE id IN (%s)"
            % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
    return {r["id"]: r["content"] for r in rows}


def facets() -> dict[str, Any]:
    with db.cursor() as cur:
        repos = [
            {"name": r["repository"], "count": r["n"]}
            for r in cur.execute(
                "SELECT repository, COUNT(*) n FROM sessions "
                "WHERE repository IS NOT NULL AND source != 'automation' "
                "GROUP BY repository ORDER BY n DESC"
            ).fetchall()
        ]
        tags = [
            {"tag": r["tag"], "count": r["n"]}
            for r in cur.execute(
                "SELECT t.tag, COUNT(*) n FROM tags t "
                "JOIN sessions s ON s.id = t.session_id AND s.source != 'automation' "
                "GROUP BY t.tag ORDER BY n DESC LIMIT 40"
            ).fetchall()
        ]
        sources = [
            {"source": r["source"], "count": r["n"]}
            for r in cur.execute(
                "SELECT source, COUNT(*) n FROM sessions GROUP BY source"
            ).fetchall()
        ]
        rng = cur.execute(
            "SELECT MIN(COALESCE(created_at, updated_at)) mn, MAX(COALESCE(updated_at, created_at)) mx FROM sessions"
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
                "SELECT turn_index, user_message, assistant_response, tools, timestamp "
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
                "SELECT filename, stored_path, mime, size_bytes, content FROM documents "
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
    ids, sessions, matrix = _vector_matrix()
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

    with db.cursor() as cur:
        rows = {
            r["id"]: r
            for r in cur.execute(
                "SELECT id, title, source, repository, created_at, updated_at "
                "FROM sessions WHERE id IN (%s) AND source != 'automation'"
                % ",".join("?" * len(order)),
                order,
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
