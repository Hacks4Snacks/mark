from __future__ import annotations

import pytest

# The MCP server surface is an optional install (`pip install markive[mcp]`).
# Skip cleanly when the `mcp` package is not present in the environment.
pytest.importorskip("mcp")

from mark import mcp_server


def test_clean_strips_tags_and_unescapes():
    assert mcp_server._clean("<mark>auth</mark> &amp; token") == "auth & token"
    assert mcp_server._clean("") == ""
    assert mcp_server._clean(None) == ""


def test_format_hit_full():
    s = {
        "id": "abc",
        "title": "Fixing auth",
        "source": "vscode",
        "repository": "kbank",
        "updated_at": "2026-01-02T09:30:00+00:00",
        "score": 0.87,
        "snippet": "<mark>token</mark> timeout",
    }
    out = mcp_server._format_hit(s)
    assert out.startswith("- [abc] Fixing auth")
    assert "vscode" in out
    assert "kbank" in out
    assert "2026-01-02" in out  # date clipped to 10 chars
    assert "score 0.87" in out
    assert "token timeout" in out  # cleaned snippet on the second line


def test_format_hit_untitled_and_no_facts():
    assert mcp_server._format_hit({"id": "x"}) == "- [x] Untitled"


def test_search_history_finds_persisted_session(make_session, persist_session):
    persist_session(make_session(sid="a", user="how do I fix the auth token timeout"))
    persist_session(
        make_session(sid="b", title="Pandas", user="group a dataframe", asst="groupby")
    )
    out = mcp_server.search_history("auth token", mode="keyword")
    assert "[a]" in out
    assert "[b]" not in out
    assert "matching" in out


def test_search_history_no_results():
    out = mcp_server.search_history("nonexistent-zzz-term", mode="keyword")
    assert out.startswith("No conversations found for:")


def test_search_history_tolerates_bad_mode_and_high_limit(
    make_session, persist_session
):
    persist_session(make_session(sid="a"))
    out = mcp_server.search_history("auth", mode="weird", limit=999)
    assert isinstance(out, str) and out


def test_get_session_returns_markdown(make_session, persist_session):
    persist_session(make_session(sid="a", title="Fixing auth"))
    out = mcp_server.get_session("a")
    assert out.startswith("# Fixing auth")


def test_get_session_missing():
    assert mcp_server.get_session("nope") == "No conversation found with id: nope"


def test_list_recent_lists_sessions(make_session, persist_session):
    persist_session(make_session(sid="a", title="First"))
    persist_session(make_session(sid="b", title="Second"))
    out = mcp_server.list_recent()
    assert "[a]" in out
    assert "[b]" in out


def test_list_recent_empty():
    assert mcp_server.list_recent() == "No conversations indexed yet."


def test_main_initialises_db_then_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server.db, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(
        mcp_server.ingest,
        "ensure_index_ready",
        lambda **kwargs: calls.append("ready"),
    )
    monkeypatch.setattr(mcp_server.mcp, "run", lambda: calls.append("run"))
    mcp_server.main()
    assert calls == ["init", "ready", "run"]
