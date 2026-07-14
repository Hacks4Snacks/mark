from __future__ import annotations

from mark import exporting


def _turn(**kw):
    base = {
        "turn_index": 0,
        "user_message": "",
        "assistant_response": "",
        "thinking": "",
        "tools": [],
    }
    base.update(kw)
    return base


def test_session_to_markdown_full_session():
    s = {
        "title": "Fixing auth",
        "summary": "How to fix the token timeout.",
        "source": "vscode",
        "repository": "kbank",
        "model": "gpt-4o",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T09:30:00+00:00",
        "turn_count": 1,
        "est_cost_usd": 0.1234,
        "turns": [
            _turn(
                turn_index=0,
                user_message="How do I fix the timeout?",
                thinking="consider refresh tokens",
                assistant_response="Use a refresh token.",
                tools=["read_file", "edit_file"],
            )
        ],
    }
    md = exporting.session_to_markdown(s)
    assert md.startswith("# Fixing auth")
    assert "> How to fix the token timeout." in md
    assert "- **Source:** vscode" in md
    assert "- **Repository:** kbank" in md
    assert "- **Model:** gpt-4o" in md
    # updated_at is preferred over created_at for the rendered date.
    assert "- **Date:** 2026-01-02T09:30:00+00:00" in md
    assert "- **Turns:** 1" in md
    assert "- **Est. cost:** ~$0.12" in md
    assert "## Turn 1" in md
    assert "**You:**" in md
    assert "How do I fix the timeout?" in md
    assert "**Reasoning:**" in md
    assert "> consider refresh tokens" in md
    assert "**Assistant:**" in md
    assert "_tools: read_file, edit_file_" in md
    assert "Use a refresh token." in md
    assert md.endswith("\n")


def test_untitled_when_no_title():
    md = exporting.session_to_markdown({"turns": []})
    assert md.startswith("# Untitled conversation")


def test_turn_index_is_offset_by_one():
    md = exporting.session_to_markdown(
        {"title": "t", "turns": [_turn(turn_index=4, user_message="hi")]}
    )
    assert "## Turn 5" in md


def test_zero_cost_is_still_rendered():
    md = exporting.session_to_markdown({"title": "t", "est_cost_usd": 0, "turns": []})
    assert "- **Est. cost:** ~$0.00" in md


def test_reasoning_blockquote_preserves_multiline():
    md = exporting.session_to_markdown(
        {"title": "t", "turns": [_turn(user_message="q", thinking="line one\nline two")]}
    )
    assert "> line one\n> line two" in md


def test_tools_parsed_from_json_string():
    md = exporting.session_to_markdown(
        {
            "title": "t",
            "turns": [
                _turn(user_message="q", assistant_response="a", tools='["grep", "read"]')
            ],
        }
    )
    assert "_tools: grep, read_" in md


def test_invalid_tools_json_is_ignored():
    md = exporting.session_to_markdown(
        {
            "title": "t",
            "turns": [
                _turn(user_message="q", assistant_response="answer", tools="not json")
            ],
        }
    )
    assert "_tools:" not in md
    assert "answer" in md


def test_document_fallback_when_no_turns():
    md = exporting.session_to_markdown(
        {"title": "Doc", "turns": [], "document": {"content": "Some note body."}}
    )
    assert "Some note body." in md


def test_slug_basic():
    assert exporting.slug("Hello, World!", "fallback") == "hello-world"


def test_slug_empty_uses_fallback():
    assert exporting.slug("", "fallback") == "fallback"
    assert exporting.slug("!!!", "fb") == "fb"


def test_slug_truncates_to_60_chars():
    assert exporting.slug("a" * 100, "fb") == "a" * 60


def test_slug_collapses_and_strips_separators():
    assert exporting.slug("  --Multiple   Spaces-- ", "fb") == "multiple-spaces"
