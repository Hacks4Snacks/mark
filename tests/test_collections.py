from __future__ import annotations

import sqlite3

from mark import collections as coll


def test_manual_only_membership(make_session, persist_session):
    persist_session(make_session(sid="a"))
    cid = coll.create("Manual")
    assert coll.resolve_member_ids(coll.get_collection(cid)) == set()

    coll.set_member(cid, "a", "include")
    assert "a" in coll.resolve_member_ids(coll.get_collection(cid))

    coll.set_member(cid, "a", "exclude")
    assert "a" not in coll.resolve_member_ids(coll.get_collection(cid))


def test_rule_membership(make_session, persist_session):
    persist_session(make_session(sid="a", user="how do I fix the auth token timeout"))
    persist_session(
        make_session(
            sid="b", title="Pandas", user="group a dataframe", asst="use groupby"
        )
    )
    cid = coll.create("Auth", rule={"q": "auth token"})
    ids = coll.resolve_member_ids(coll.get_collection(cid))
    assert "a" in ids
    assert "b" not in ids


def test_rule_with_multiple_topics_requires_all(make_session, persist_session):
    from mark.repositories import sessions as sessions_repo

    for sid in ("both", "alpha-only"):
        persist_session(make_session(sid=sid, user="collection topic probe"))
    sessions_repo.add_tag("both", "alpha")
    sessions_repo.add_tag("both", "beta")
    sessions_repo.add_tag("alpha-only", "alpha")

    cid = coll.create(
        "Both topics",
        rule={"q": "collection topic probe", "tags": ["alpha", "beta"]},
    )

    assert coll.resolve_member_ids(coll.get_collection(cid)) == {"both"}


def test_large_collection_avoids_sqlite_variable_limit(monkeypatch):
    from mark import ask, db, search
    from mark.db import connection

    session_ids = [f"large-{index}" for index in range(1001)]
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO sessions(id, source, title, hidden) VALUES (?, 'upload', ?, 0)",
            ((sid, sid) for sid in session_ids),
        )
        conn.executemany(
            "INSERT INTO chunks(session_id, source_type, content) "
            "VALUES (?, 'document', 'largecollectionprobe')",
            ((sid,) for sid in session_ids),
        )
        conn.execute(
            "INSERT INTO search_index(content, title, tags, chunk_id, session_id, "
            "source_type, turn_index) "
            "SELECT c.content, s.title, '', c.id, s.id, 'document', NULL "
            "FROM chunks c JOIN sessions s ON s.id = c.session_id"
        )
    cid = coll.create(
        "Large",
        rule={"q": "largecollectionprobe", "mode": "keyword"},
    )

    real_connect = connection.connect

    def limited_connect():
        conn = real_connect()
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return conn

    monkeypatch.setattr(connection, "connect", limited_connect)
    monkeypatch.setattr(db, "connect", limited_connect)
    collection = coll.get_collection(cid)

    assert len(coll.resolve_member_ids(collection)) == 1001
    listed = next(row for row in coll.list_collections() if row["id"] == cid)
    assert listed["membership_policy"] == {
        "kind": "complete",
        "cap": None,
        "truncated": False,
    }
    assert len(coll.members_as_cards(cid)) == 1001
    assert coll.overview(cid)["totals"]["sessions"] == 1001
    passages = search.search_passages(
        "largecollectionprobe",
        limit=1001,
        per_session_cap=1,
        only_ids=set(session_ids),
        candidate_factor=1,
    )
    assert len(passages) == 1001
    assert ask._load_turns_map(passages, radius=1) == {}
    context, sources = ask.build_context(
        "largecollectionprobe",
        char_budget=1000,
        max_sessions=1,
        session_ids=set(session_ids),
    )
    assert context
    assert len(sources) == 1


def test_filter_only_rule_has_no_implicit_500_cap():
    from mark import db

    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO sessions(id, source, repository, hidden) "
            "VALUES (?, 'upload', 'target', 0)",
            ((f"filtered-{index}",) for index in range(601)),
        )
    cid = coll.create("All target", rule={"repo": "target"})

    assert len(coll.resolve_member_ids(coll.get_collection(cid))) == 601


def test_ranked_rule_reports_server_cap(monkeypatch):
    from mark import config, search

    seen = {}

    def ranked(query, *, mode, limit, **kwargs):
        seen.update(query=query, mode=mode, limit=limit)
        return ["one", "two"], True

    monkeypatch.setattr(search, "ranked_session_ids", ranked)
    monkeypatch.setattr(coll.visibility, "filter_visible", lambda ids: set(ids))
    cid = coll.create("Ranked", rule={"q": "meaning", "mode": "semantic"})

    listed = next(row for row in coll.list_collections() if row["id"] == cid)
    assert seen == {
        "query": "meaning",
        "mode": "semantic",
        "limit": config.COLLECTION_RANKED_LIMIT,
    }
    assert listed["count"] == 2
    assert listed["membership_policy"] == {
        "kind": "ranked",
        "cap": config.COLLECTION_RANKED_LIMIT,
        "truncated": True,
    }


def test_ranked_rule_counts_sessions_below_chunk_heavy_matches(
    persist_session, monkeypatch
):
    import hashlib

    from mark import config

    def many_turns(sid, count):
        turns = [
            {
                "turn_index": index,
                "user_message": f"rankedprobe session {sid} turn {index}",
                "assistant_response": "ok",
                "tools": [],
                "timestamp": "2026-01-01T00:00:00Z",
                "files": [],
                "urls": [],
                "code_blocks": [],
            }
            for index in range(count)
        ]
        return {
            "id": sid,
            "source": "vscode",
            "title": sid,
            "workspace_id": None,
            "repository": "repo",
            "repo_path": None,
            "requester": None,
            "responder": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "source_path": None,
            "content_hash": hashlib.sha256(repr(turns).encode()).hexdigest(),
            "turns": turns,
            "metrics": {},
        }

    monkeypatch.setattr(config, "COLLECTION_RANKED_LIMIT", 2)
    persist_session(many_turns("a", 20))
    persist_session(many_turns("b", 20))
    persist_session(many_turns("c", 1))
    cid = coll.create("Ranked", rule={"q": "rankedprobe", "mode": "hybrid"})

    listed = next(row for row in coll.list_collections() if row["id"] == cid)
    assert listed["count"] == 2
    assert listed["membership_policy"]["truncated"] is True


def test_rule_sort_changes_presentation_not_membership(make_session, persist_session):
    persist_session(make_session(sid="a", user="sortmembership"))
    persist_session(make_session(sid="b", user="sortmembership"))
    recent = coll.create(
        "Recent", rule={"q": "sortmembership", "mode": "keyword", "sort": "recent"}
    )
    title = coll.create(
        "Title", rule={"q": "sortmembership", "mode": "keyword", "sort": "title"}
    )

    assert coll.resolve_member_ids(
        coll.get_collection(recent)
    ) == coll.resolve_member_ids(coll.get_collection(title))


def test_legacy_rule_limit_is_ignored(make_session, persist_session):
    from mark import db

    persist_session(make_session(sid="a", user="legacylimitprobe"))
    cid = coll.create("Legacy")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE collections SET rule = ? WHERE id = ?",
            ('{"q":"legacylimitprobe","mode":"keyword","limit":"bad"}', cid),
        )

    collection = coll.get_collection(cid)
    assert collection["rule_error"] is None
    assert "limit" not in collection["rule"]
    assert coll.resolve_member_ids(collection) == {"a"}


def test_invalid_stored_rule_is_reported():
    from mark import db

    cid = coll.create("Broken")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE collections SET rule = ? WHERE id = ?",
            ('{"mode":"invalid","unknown":true}', cid),
        )

    collection = coll.get_collection(cid)
    listed = next(row for row in coll.list_collections() if row["id"] == cid)
    assert collection["rule"] is None
    assert "invalid collection rule" in collection["rule_error"]
    assert listed["membership_policy"]["kind"] == "invalid"


def test_removing_a_rule_match_sticks_as_exclude(make_session, persist_session):
    persist_session(make_session(sid="a", user="auth token timeout"))
    cid = coll.create("Auth", rule={"q": "auth token"})
    assert "a" in coll.resolve_member_ids(coll.get_collection(cid))

    # Removing a rule-matched session records an explicit exclude that survives
    # re-resolution (otherwise the rule would silently re-add it).
    coll.remove_member(cid, "a")
    assert "a" not in coll.resolve_member_ids(coll.get_collection(cid))


def test_collections_for_session_reports_membership(make_session, persist_session):
    persist_session(make_session(sid="a"))
    cid = coll.create("C")
    coll.set_member(cid, "a", "include")
    rows = coll.collections_for_session("a")
    mine = next(r for r in rows if r["id"] == cid)
    assert mine["member"] is True
    assert mine["manual_include"] is True


def test_manual_membership_survives_session_reingest(make_session, persist_session):
    persist_session(make_session(sid="a", asst="first answer"))
    cid = coll.create("Manual")
    coll.set_member(cid, "a", "include")

    persist_session(make_session(sid="a", asst="changed answer"))

    assert "a" in coll.resolve_member_ids(coll.get_collection(cid))
    mine = next(r for r in coll.collections_for_session("a") if r["id"] == cid)
    assert mine["manual_include"] is True


def test_rule_exclusion_survives_session_reingest(make_session, persist_session):
    persist_session(make_session(sid="a", user="auth token timeout"))
    cid = coll.create("Auth", rule={"q": "auth token"})
    coll.remove_member(cid, "a")
    assert "a" not in coll.resolve_member_ids(coll.get_collection(cid))

    persist_session(
        make_session(sid="a", user="auth token timeout", asst="changed answer")
    )

    assert "a" not in coll.resolve_member_ids(coll.get_collection(cid))
    mine = next(r for r in coll.collections_for_session("a") if r["id"] == cid)
    assert mine["manual_exclude"] is True
