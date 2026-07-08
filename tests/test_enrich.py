from __future__ import annotations

from mark import enrich


def test_plaintext_strips_markdown_and_code():
    md = (
        "# Heading\n\nUse `func()` and see [the docs](https://x).\n\n"
        "```py\ncode\n```\n> quote"
    )
    out = enrich._plaintext(md)
    assert "func" not in out  # inline code removed
    assert "code" not in out  # fenced code removed
    assert "the docs" in out  # link text kept, url dropped
    assert "https" not in out
    assert "#" not in out and ">" not in out
    assert "Heading" in out


def test_plaintext_empty():
    assert enrich._plaintext("") == ""


def test_sentences_length_filter():
    text = "Too short. This sentence is definitely long enough to be kept here. X."
    sents = enrich._sentences(text)
    assert any("long enough" in s for s in sents)
    assert all(25 <= len(s) <= 320 for s in sents)


def test_overlap_jaccard():
    assert enrich._overlap("alpha beta", "alpha beta") == 1.0
    assert enrich._overlap("alpha beta", "gamma delta") == 0.0
    assert enrich._overlap("", "x") == 0.0
    assert abs(enrich._overlap("a b", "b c") - 1 / 3) < 1e-9


def test_candidates_filters_and_normalizes():
    cands = dict(enrich._candidates("database migration database migration schema"))
    assert "database migration" in cands  # repeated bigram survives
    assert cands["database migration"] == 1.0  # top score is normalized to 1.0
    assert "the" not in cands  # stopwords are dropped
    assert all(0 < v <= 1.0 for v in cands.values())


def test_candidates_empty_for_stopwords_only():
    assert enrich._candidates("the a an of to") == []
    assert enrich._candidates("") == []


def test_keywords_freq_dedups_substrings():
    tags = enrich._keywords_freq("database migration database migration schema")
    terms = [t for t, _ in tags]
    assert terms[0] == "database migration"
    assert "schema" in terms
    # "database" alone is a substring of the chosen bigram, so it is deduped out.
    assert "database" not in terms
    assert all(isinstance(score, float) for _, score in tags)


def test_summarize_fast_combines_intent_and_reply():
    turns = [
        {
            "user_message": "How do I fix the authentication token timeout?",
            "assistant_response": (
                "You should use a refresh token to recover. "
                "Handle the 401 response by retrying the request."
            ),
        }
    ]
    summary = enrich._summarize_fast(turns)
    assert isinstance(summary, str) and summary
    assert "authentication token timeout" in summary
    assert "refresh token" in summary or "401" in summary


def test_enrich_session_returns_summary_and_tags():
    turns = [
        {
            "user_message": "How do I set up database migrations in the project?",
            "assistant_response": (
                "Run the migration tool to create schema changes. "
                "Database migrations should be versioned and reviewed."
            ),
        }
    ]
    summary, tags = enrich.enrich_session("Database migrations", turns)
    assert isinstance(summary, str) and summary
    assert isinstance(tags, list) and tags
    for term, score in tags:
        assert isinstance(term, str) and term
        assert isinstance(score, float)
        assert term == term.lower()  # auto tags are lowercased
    terms = {t for t, _ in tags}
    assert "the" not in terms and "how" not in terms  # stopwords never surface


def test_enrich_text_returns_summary_and_tags():
    summary, tags = enrich.enrich_text(
        "Auth refactor",
        "Fixing the authentication token timeout bug in the API gateway service.",
    )
    assert isinstance(summary, str)
    assert isinstance(tags, list) and tags
    terms = {t for t, _ in tags}
    assert any(
        key in t for t in terms for key in ("auth", "token", "timeout", "gateway")
    )
