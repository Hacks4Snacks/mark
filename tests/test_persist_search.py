"""Persistence round-trip and hybrid search behaviour."""

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
    persist_session(make_session(sid="b", title="Pandas", user="group a dataframe", asst="use groupby"))
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
