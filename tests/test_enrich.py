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


def test_candidates_drop_path_boilerplate_but_keep_dotted_technologies():
    cands = dict(
        enrich._candidates(
            "Session File Path directory node.js node.js socket.io socket.io "
            "three.js three.js react.js react.js d3.js d3.js"
        )
    )

    assert "path" not in cands
    assert "directory" not in cands
    assert "node.js" in cands
    assert "socket.io" in cands
    assert "three.js" in cands
    assert "react.js" in cands
    assert "d3.js" in cands


def test_weighted_topic_segments_do_not_create_cross_field_bigrams():
    tags = enrich._keywords_freq_segments([("alpha beta", 3.0), ("gamma", 2.0)])
    terms = {term for term, _score in tags}

    assert "beta alpha" not in terms
    assert "beta gamma" not in terms


def test_dotted_topic_requires_authored_repetition():
    weighted_once = enrich._candidate_segments([("config.js", 3.0)])
    authored_twice = enrich._candidate_segments([("three.js three.js", 1.0)])

    assert "config.js" not in dict(weighted_once)
    assert "three.js" in dict(authored_twice)


def test_topic_plaintext_preserves_slash_concepts_and_strips_relative_paths():
    cleaned = enrich._topic_plaintext(
        "TCP/IP client/server apps/v1 src/private/certificates.md "
        r"src\private\secrets.yaml"
    )

    assert "TCP/IP" in cleaned
    assert "client/server" in cleaned
    assert "apps/v1" in cleaned
    assert "certificates.md" not in cleaned
    assert "secrets.yaml" not in cleaned


def test_topic_plaintext_strips_generic_injected_wrappers():
    cleaned = enrich._topic_plaintext(
        "<availableDeferredTools>src/private/node.js</availableDeferredTools> "
        "<modeInstructions>ignore this</modeInstructions> certificate rotation"
    )

    assert cleaned == "certificate rotation"


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


def test_enrich_session_prioritizes_concepts_over_paths_and_urls():
    turns = [
        {
            "user_message": (
                "I am designing Kubernetes cluster CA rotation and need help "
                "understanding CAPI and kubeadm certificate behavior."
            ),
            "assistant_response": (
                "I am validating cluster reachability. "
                "See https://raw.githubusercontent.com/kubernetes-sigs/cluster-api/main/x.go "
                "and /Users/m/microsoft/nc-platform-operator/hack/cluster-ca-rotation-poc.sh."
            ),
        },
        {
            "user_message": (
                '<skill-context name="cluster">/Users/m/.copilot/skills/cluster '
                "https://raw.githubusercontent.com/example/repo</skill-context>"
            ),
            "assistant_response": "Checking certificate secrets and CAPI controllers.",
        },
        {
            "user_message": "What is the supported certificate rotation boundary?",
            "assistant_response": (
                "CAPI supports automatic leaf certificate renewal through kubeadm. "
                "Cluster root CA rotation requires a staged manual trust migration."
            ),
        },
    ]

    summary, tags = enrich.enrich_session(
        "Kubernetes cluster CA rotation with CAPI", turns
    )
    terms = {term for term, _score in tags}

    assert "root CA rotation".lower() in summary.lower()
    assert any("rotation" in term for term in terms)
    assert any(
        concept in term
        for term in terms
        for concept in ("certificate", "kubernetes", "capi", "kubeadm")
    )
    assert not any(
        noise in term
        for term in terms
        for noise in (
            "raw.githubusercontent.com",
            "users microsoft",
            "cluster-ca-rotation-poc.sh",
            "https",
        )
    )


def test_summarize_fast_prefers_final_finding_over_startup_narration():
    turns = [
        {
            "user_message": "Investigate Kubernetes certificate rotation support.",
            "assistant_response": (
                "I am validating cluster reachability before inspecting objects. "
                "Next I will map the deployed controllers."
            ),
        },
        {
            "user_message": "What did you conclude?",
            "assistant_response": (
                "CAPI renews leaf certificates automatically. Root CA rotation "
                "requires a staged manual trust migration."
            ),
        },
    ]

    summary = enrich._summarize_fast(turns)

    assert "Root CA rotation" in summary
    assert "validating cluster reachability" not in summary


def test_summarize_fast_strips_paths_urls_and_injected_context():
    turns = [
        {
            "user_message": (
                "Investigate certificate rotation at /Users/me/project and "
                "https://example.com/runbook."
            ),
            "assistant_response": "Initial investigation is in progress.",
        },
        {
            "user_message": (
                "<workspace_info>src/private/certificates.md</workspace_info> "
                '<skill-context name="cluster">/Users/me/secret/path</skill-context>'
            ),
            "assistant_response": (
                "Root CA rotation requires a staged trust migration. "
                "See file:///Users/me/private/notes.txt for details."
            ),
        },
    ]

    summary = enrich._summarize_fast(turns)

    assert "Root CA rotation" in summary
    assert "/Users/" not in summary
    assert "example.com" not in summary
    assert "skill-context" not in summary
    assert "workspace_info" not in summary
    assert "src/private" not in summary


def test_summarize_fast_skips_closing_pleasantry():
    turns = [
        {
            "user_message": "Investigate certificate rotation support.",
            "assistant_response": (
                "Root CA rotation requires a staged trust migration. "
                "Leaf certificates renew automatically."
            ),
        },
        {
            "user_message": "Thanks.",
            "assistant_response": (
                "You're welcome. Let me know if you need anything else."
            ),
        },
    ]

    summary = enrich._summarize_fast(turns)

    assert "Root CA rotation" in summary
    assert "You're welcome" not in summary


def test_summarize_fast_removes_closing_sentence_after_substantive_answer():
    turns = [
        {
            "user_message": "Investigate certificate rotation support.",
            "assistant_response": (
                "Root CA rotation requires a staged trust migration. "
                "Let me know if you need anything else."
            ),
        }
    ]

    summary = enrich._summarize_fast(turns)

    assert "Root CA rotation" in summary
    assert "Let me know" not in summary


def test_summarize_fast_keeps_substantive_let_me_know_sentence():
    turns = [
        {
            "user_message": "Why is TLS failing?",
            "assistant_response": (
                "Let me know if TLS 1.3 is disabled on the proxy because that "
                "would explain the handshake failure."
            ),
        }
    ]

    summary = enrich._summarize_fast(turns)

    assert "TLS 1.3" in summary


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
