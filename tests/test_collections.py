"""Collection membership math: (rule ∪ includes) − excludes."""

from __future__ import annotations

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
        make_session(sid="b", title="Pandas", user="group a dataframe", asst="use groupby")
    )
    cid = coll.create("Auth", rule={"q": "auth token"})
    ids = coll.resolve_member_ids(coll.get_collection(cid))
    assert "a" in ids
    assert "b" not in ids


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
