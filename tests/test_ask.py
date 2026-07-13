from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

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


def test_search_passages_repository_scope_precedes_candidate_truncation(
    persist_session,
):
    target = _session(
        "target",
        [("filler " * 300 + "passagescopeprobe", "ok")],
        repository="target-repo",
    )
    persist_session(target)
    for index in range(8):
        persist_session(
            _session(
                f"distractor-{index}",
                [("passagescopeprobe passagescopeprobe passagescopeprobe", "ok")],
                repository="other-repo",
            )
        )

    passages = search.search_passages(
        "passagescopeprobe", limit=1, candidate_factor=1, repo="target-repo"
    )

    assert [passage["session_id"] for passage in passages] == ["target"]


def test_plan_resolves_repository_case_insensitively():
    plan = ask.plan_query(
        "What was fixed in AFOI-NC-Security?",
        repositories=["afoi-nc-security", "other"],
    )

    assert plan.repository == "afoi-nc-security"


def test_plan_does_not_guess_unknown_repository():
    plan = ask.plan_query("What happened in missing-repo?", repositories=["known-repo"])

    assert plan.repository is None


def test_plan_does_not_guess_between_multiple_repositories():
    plan = ask.plan_query("Compare alpha and beta", repositories=["alpha", "beta"])

    assert plan.repository is None


def test_plan_parses_past_week_with_injected_clock():
    plan = ask.plan_query(
        "What changed in the past week?",
        now=datetime(2026, 7, 13, 12, tzinfo=timezone.utc),
        repositories=[],
    )

    assert plan.date_from == "2026-07-07"
    assert plan.date_to == "2026-07-13"


def test_plan_marks_most_recent_as_latest():
    plan = ask.plan_query("What was the most recent issue?", repositories=[])

    assert plan.recency == "latest"


def test_latest_intent_survives_cross_encoder_reranking(monkeypatch):
    passages = [
        {"content": "old", "timestamp": "2026-01-01T00:00:00Z"},
        {"content": "new", "timestamp": "2026-02-01T00:00:00Z"},
    ]
    monkeypatch.setattr(embeddings, "rerank", lambda query, docs: [100.0, 0.0])

    reranked = ask._rerank("latest", passages, recency="latest")

    assert [passage["content"] for passage in reranked] == ["new", "old"]


def test_recent_intent_blends_retrieval_and_cross_encoder_ranks(monkeypatch):
    passages = [
        {"content": "recent lane"},
        {"content": "older relevance"},
    ]
    monkeypatch.setattr(embeddings, "rerank", lambda query, docs: [0.0, 100.0])

    reranked = ask._rerank("recent", passages, recency="boost")

    assert [passage["content"] for passage in reranked] == [
        "recent lane",
        "older relevance",
    ]


def test_utc_date_scope_normalizes_timestamp_offsets(persist_session):
    first = _session("utc-in", [("timezoneprobe", "ok")])
    first["updated_at"] = "2026-07-14T00:30:00+02:00"
    second = _session("utc-out", [("timezoneprobe", "ok")])
    second["updated_at"] = "2026-07-13T23:30:00-07:00"
    persist_session(first)
    persist_session(second)

    passages = search.search_passages(
        "timezoneprobe", date_from="2026-07-13", date_to="2026-07-13"
    )

    assert {passage["session_id"] for passage in passages} == {"utc-in"}


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


def test_rejected_context_does_not_add_source(monkeypatch):
    monkeypatch.setattr(
        search,
        "search_passages",
        lambda *args, **kwargs: [
            {
                "chunk_id": 1,
                "session_id": "accepted",
                "turn_index": None,
                "source_type": "document",
                "timestamp": "2026-01-01T00:00:00Z",
                "content": "accepted evidence",
                "score": 1.0,
                "title": "Accepted",
                "source": "upload",
                "repository": "repo",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {
                "chunk_id": 2,
                "session_id": "rejected",
                "turn_index": None,
                "source_type": "document",
                "timestamp": "2026-01-02T00:00:00Z",
                "content": "rejected " * 100,
                "score": 0.5,
                "title": "Rejected",
                "source": "upload",
                "repository": "repo",
                "updated_at": "2026-01-02T00:00:00Z",
            },
        ],
    )
    monkeypatch.setattr(ask, "plan_query", lambda question: ask.AskQueryPlan(question))
    monkeypatch.setattr(embeddings, "rerank", lambda query, docs: None)

    context, sources = ask.build_context("q", char_budget=100)

    assert "accepted evidence" in context
    assert "Rejected" not in context
    assert [source["id"] for source in sources] == ["accepted"]


def test_sources_contain_only_accepted_bounded_passages(persist_session, monkeypatch):
    monkeypatch.setattr(config, "ASK_SOURCE_EXCERPT_CHARS", 80)
    persist_session(
        _session(
            "a",
            [("evidenceprobe " + "detail " * 100, "answer")],
            title="Evidence",
        )
    )

    context, sources = ask.build_context("evidenceprobe", char_budget=4000)

    assert context
    assert len(sources) == 1
    passages = sources[0]["passages"]
    assert passages
    assert all(len(passage["excerpt"]) <= 82 for passage in passages)
    assert {
        "chunk_id",
        "turn_index",
        "source_type",
        "timestamp",
        "score",
        "excerpt",
    } <= passages[0].keys()


def test_context_never_exceeds_budget_with_oversized_metadata(monkeypatch):
    monkeypatch.setattr(
        search,
        "search_passages",
        lambda *args, **kwargs: [
            {
                "chunk_id": 1,
                "session_id": "a",
                "turn_index": None,
                "source_type": "document",
                "timestamp": None,
                "content": "body " * 1000,
                "score": 1.0,
                "title": "t" * 5000,
                "source": "s" * 5000,
                "repository": "r" * 5000,
                "updated_at": None,
            }
        ],
    )
    monkeypatch.setattr(ask, "plan_query", lambda question: ask.AskQueryPlan(question))

    context, sources = ask.build_context("q", char_budget=2000)

    assert len(context) <= 2000
    assert sources


def test_first_block_keeps_matched_evidence_before_neighbors(monkeypatch):
    monkeypatch.setattr(config, "ASK_NEIGHBOR_TURNS", 4)
    monkeypatch.setattr(config, "ASK_NEIGHBOR_CHARS", 800)
    monkeypatch.setattr(
        search,
        "search_passages",
        lambda *args, **kwargs: [
            {
                "chunk_id": 1,
                "session_id": "a",
                "turn_index": 4,
                "source_type": "turn",
                "timestamp": "2026-01-01T00:00:00Z",
                "content": "MATCHED-EVIDENCE",
                "score": 1.0,
                "title": "T",
                "source": "vscode",
                "repository": "repo",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        ask,
        "_load_turns_map",
        lambda passages, radius: {
            "a": {
                index: {
                    "user_message": f"neighbor-{index} " * 200,
                    "assistant_response": "",
                }
                for index in range(4)
            }
        },
    )
    monkeypatch.setattr(ask, "plan_query", lambda question: ask.AskQueryPlan(question))

    context, sources = ask.build_context("q", char_budget=2000)

    assert "MATCHED-EVIDENCE" in context
    assert sources[0]["passages"][0]["excerpt"] == "MATCHED-EVIDENCE"


def test_latest_rerank_normalizes_timestamp_offsets(monkeypatch):
    passages = [
        {"content": "older", "timestamp": "2026-07-14T00:30:00+02:00"},
        {"content": "newer", "timestamp": "2026-07-13T23:30:00Z"},
    ]
    monkeypatch.setattr(embeddings, "rerank", lambda query, docs: [100.0, 0.0])

    reranked = ask._rerank("latest", passages, recency="latest")

    assert [passage["content"] for passage in reranked] == ["newer", "older"]


def test_latest_rerank_treats_naive_timestamp_as_utc(monkeypatch):
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        passages = [
            {"content": "naive older", "timestamp": "2026-07-13T22:00:00"},
            {"content": "aware newer", "timestamp": "2026-07-13T22:30:00Z"},
        ]
        monkeypatch.setattr(embeddings, "rerank", lambda query, docs: [100.0, 0.0])

        reranked = ask._rerank("latest", passages, recency="latest")

        assert [passage["content"] for passage in reranked] == [
            "aware newer",
            "naive older",
        ]
    finally:
        if previous_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous_tz)
        if hasattr(time, "tzset"):
            time.tzset()


def test_stream_answer_bounds_complete_model_prompt(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            yield b'{"done": true}'

    monkeypatch.setattr(
        ask, "status", lambda: {"available": True, "model": "test-model"}
    )
    monkeypatch.setattr(ask, "_effective_num_ctx", lambda model: 2048)

    def build_context(question, *, char_budget, **kwargs):
        captured["char_budget"] = char_budget
        return "x" * char_budget, []

    def urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(ask, "build_context", build_context)
    monkeypatch.setattr(ask.urllib.request, "urlopen", urlopen)
    question = "q" * config.MAX_ASK_QUESTION_CHARS

    list(ask.stream_answer(question))

    messages = captured["payload"]["messages"]
    complete_chars = sum(len(message["content"]) for message in messages)
    input_chars = int((2048 - config.ASK_RESERVE_OUTPUT_TOKENS) * 0.9 * 3.5)
    assert captured["char_budget"] > 0
    assert complete_chars <= input_chars


def test_turn_loader_fetches_only_needed_neighbors(persist_session):
    persist_session(
        _session(
            "a",
            [(f"unrelated {index}", "x" * 1000) for index in range(100)],
        )
    )
    passages = [
        {"session_id": "a", "turn_index": 50},
        {"session_id": "a", "turn_index": 80},
    ]

    turns = ask._load_turns_map(passages, radius=1)

    assert set(turns["a"]) == {49, 50, 51, 79, 80, 81}
    assert all(
        len(turn["assistant_response"]) <= config.ASK_NEIGHBOR_CHARS + 2
        for turn in turns["a"].values()
    )


def test_build_context_unbounded_sessions_by_default(persist_session):
    # No max_sessions: breadth is bounded only by the character budget, not a
    # session count. Ten matching sessions all fit a generous budget (the old
    # default would have capped this at 8).
    for i in range(10):
        persist_session(_session(f"s{i}", [(f"zphloga note number {i}", "ok")]))

    _, sources = ask.build_context("zphloga", char_budget=100000)
    assert len(sources) == 10


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
