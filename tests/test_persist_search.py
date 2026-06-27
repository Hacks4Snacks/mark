from __future__ import annotations

from mark import search, uploads


def test_write_session_round_trip(make_session, persist_session):
    persist_session(
        make_session(code_blocks=[{"language": "py", "content": "print('hi')"}])
    )
    got = search.get_session("s1")
    assert got is not None
    assert got["title"] == "Test session"
    assert got["repository"] == "kbank"
    assert len(got["turns"]) == 1
    assert got["turns"][0]["user_message"].startswith("how do I fix")


def test_browse_lists_persisted_session(make_session, persist_session):
    persist_session(make_session(sid="a"))
    persist_session(make_session(sid="b", title="Second"))
    ids = {r["id"] for r in search.browse()}
    assert {"a", "b"} <= ids


def test_keyword_search_finds_session(make_session, persist_session):
    persist_session(make_session(sid="a", user="how do I fix the auth token timeout"))
    persist_session(
        make_session(
            sid="b", title="Pandas", user="group a dataframe", asst="use groupby"
        )
    )
    res = search.search("auth token", mode="keyword")
    found = {r["id"] for r in res}
    assert "a" in found
    assert "b" not in found


def test_semantic_search_over_embedded_note():
    # Notes are embedded on write, so semantic search has vectors to match.
    sid = uploads.add_note(
        "Auth refactor", "fixing the authentication token timeout bug in the API"
    )
    res = search.search("token timeout", mode="semantic")
    assert any(r["id"] == sid for r in res)


def test_reindex_preserves_embeddings_for_unchanged_chunks(make_session):
    """Re-indexing a session carries existing chunk vectors over instead of
    deleting them and forcing a re-embed of identical content."""
    from mark import db, persist

    session = make_session(sid="grow")
    with db.connect() as conn:
        cur = conn.cursor()
        persist.write_session(cur, session)
        chunks = cur.execute(
            "SELECT id, content FROM chunks WHERE session_id = 'grow'"
        ).fetchall()
        assert chunks  # sanity: the session produced chunks
        for c in chunks:
            cur.execute(
                "INSERT INTO embeddings(chunk_id, session_id, model, dim, vector) "
                "VALUES (?,?,?,?,?)",
                (c["id"], "grow", "test-model", 3, b"\x00\x00\x80?" * 3),
            )
        conn.commit()
        embedded = {c["content"] for c in chunks}

        # Re-index the same session (a churn pass) with identical content.
        persist.write_session(cur, session)
        conn.commit()
        kept = {
            r["content"]
            for r in cur.execute(
                "SELECT c.content FROM chunks c "
                "JOIN embeddings e ON e.chunk_id = c.id "
                "WHERE c.session_id = 'grow'"
            )
        }
    assert kept == embedded  # every unchanged chunk kept its vector
