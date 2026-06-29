from __future__ import annotations

import hashlib

from mark import ask, config, embeddings, search


def _session(sid, turns_text, *, title="S", source="vscode", repository="repo"):
    """Build a multi-turn session dict matching persist.write_session's contract.

    ``turns_text`` is a list of ``(user_message, assistant_response)`` tuples.
    """
    turns = []
    for i, (user, asst) in enumerate(turns_text):
        turns.append(
            {
                "turn_index": i,
                "user_message": user,
                "assistant_response": asst,
                "tools": [],
                "timestamp": "2026-01-01T00:00:00+00:00",
                "files": [],
                "urls": [],
                "code_blocks": [],
            }
        )
    raw = (sid + title + repr(turns_text)).encode()
    return {
        "id": sid,
        "source": source,
        "title": title,
        "workspace_id": None,
        "repository": repository,
        "repo_path": None,
        "requester": None,
        "responder": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
        "source_path": None,
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "turns": turns,
        "metrics": {},
    }


def test_search_passages_caps_per_session(persist_session):
    # One session whose every turn matches a rare token, so several chunks hit.
    persist_session(
        _session(
            "a",
            [(f"zphloga retry tuning note {i}", "noted") for i in range(5)],
        )
    )
    persist_session(_session("b", [("zphloga appears once here", "ok")]))

    passages = search.search_passages("zphloga", per_session_cap=2)
    from_a = [p for p in passages if p["session_id"] == "a"]
    assert len(from_a) == 2  # capped, even though 5 turns matched
    assert any(p["session_id"] == "b" for p in passages)
    # Passages carry their own chunk text + turn index, not a session summary.
    assert all("content" in p and "turn_index" in p for p in passages)


def test_search_passages_only_ids_scopes(persist_session):
    persist_session(_session("a", [("zphloga one", "x")]))
    persist_session(_session("b", [("zphloga two", "y")]))

    passages = search.search_passages("zphloga", only_ids={"a"})
    assert passages
    assert {p["session_id"] for p in passages} == {"a"}


def test_build_context_uses_matched_turn_not_first_turns(persist_session, monkeypatch):
    # Only the two genuinely-matching chunks (the late turn) should be packed,
    # not whichever chunks merely share the session's auto tags.
    monkeypatch.setattr(config, "ASK_PER_SESSION_PASSAGES", 2)
    # The answer lives well past the old first-8-turns window; early turns are
    # unrelated filler the previous excerpt approach would have dumped verbatim.
    turns = [("alphaearly greeting", "hello there")]
    turns += [(f"unrelated chatter {i}", "noted") for i in range(12)]
    turns += [
        (
            "how did we configure the retry backoff for the kafka consumer",
            "we set backoff.ms to 5000 and capped it at eight retries",
        )
    ]
    persist_session(_session("a", turns))  # matched turn is index 13

    context, sources = ask.build_context(
        "kafka consumer retry backoff configure",
        char_budget=40000,
        max_sessions=5,
    )
    # The matched late turn (beyond the old 8-turn cutoff) is present...
    assert "5000" in context
    assert "consumer" in context
    # ...while the unrelated first turn is nowhere near the match and excluded.
    assert "alphaearly" not in context
    assert sources and sources[0]["id"] == "a"


def test_build_context_respects_char_budget(persist_session, monkeypatch):
    monkeypatch.setattr(config, "ASK_NEIGHBOR_TURNS", 0)
    # A single user-only turn => exactly one matching chunk, longer than the
    # budget, so packing must trim it rather than emit the whole turn.
    long_user = "kafka retry backoff zphmark " + ("detail " * 60)
    persist_session(_session("a", [(long_user, "")]))

    context, _ = ask.build_context(
        "kafka retry backoff zphmark", char_budget=300, max_sessions=5
    )
    assert context  # at least the single best passage is always included
    assert len(context) < 500  # trimmed to roughly the budget, not the full turn
    assert "zphmark" in context  # the relevant slice (start of turn) survives


def test_build_context_citations_are_per_session(persist_session):
    persist_session(_session("a", [("zphloga from session a", "x")], title="Alpha"))
    persist_session(_session("b", [("zphloga from session b", "y")], title="Beta"))

    context, sources = ask.build_context("zphloga", char_budget=40000, max_sessions=5)
    assert len(sources) == 2
    assert [s["n"] for s in sources] == [1, 2]
    assert "[1]" in context and "[2]" in context

    # max_sessions caps breadth.
    _, capped = ask.build_context("zphloga", char_budget=40000, max_sessions=1)
    assert len(capped) == 1


def test_rerank_reorders_by_cross_encoder(monkeypatch):
    passages = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
    # Force ascending scores so the last passage becomes the most relevant.
    monkeypatch.setattr(embeddings, "rerank", lambda q, docs: [0.1, 0.2, 0.9])
    out = ask._rerank("q", passages)
    assert [p["content"] for p in out] == ["c", "b", "a"]


def test_rerank_falls_back_when_unavailable(monkeypatch):
    passages = [{"content": "a"}, {"content": "b"}]
    monkeypatch.setattr(embeddings, "rerank", lambda q, docs: None)
    out = ask._rerank("q", passages)
    assert [p["content"] for p in out] == ["a", "b"]  # unchanged


def test_rerank_disabled_returns_none():
    # conftest forces the reranker off; rerank() must degrade to None, not crash.
    assert embeddings.rerank("q", ["doc"]) is None


def test_api_ask_accepts_body_without_limit(client, monkeypatch):
    # The "sources" dropdown was removed, so the endpoint must accept a
    # limit-less body and fall back to the backend default (limit=None).
    seen = {}

    def fake_stream(question, limit=None, session_ids=None):
        seen["question"] = question
        seen["limit"] = limit
        yield {"type": "done", "model": "stub"}

    monkeypatch.setattr(ask, "stream_answer", fake_stream)
    resp = client.post("/api/ask", json={"question": "anything"})
    assert resp.status_code == 200  # body validated (not a 422)
    assert seen == {"question": "anything", "limit": None}
