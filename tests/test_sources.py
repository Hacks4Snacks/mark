"""Source adapters: the ChatGPT importer end-to-end plus registry sanity."""

from __future__ import annotations

import json

from mark.sources import IMPORT_SOURCES, WATCHED_SOURCES
from mark.sources.chatgpt import ChatGptSource


def _sample_export() -> bytes:
    convo = {
        "title": "Auth help",
        "conversation_id": "abc",
        "create_time": 1700000000,
        "update_time": 1700000100,
        "current_node": "n2",
        "mapping": {
            "root": {"id": "root", "message": None, "parent": None, "children": ["n1"]},
            "n1": {
                "id": "n1",
                "parent": "root",
                "children": ["n2"],
                "message": {
                    "author": {"role": "user"},
                    "create_time": 1700000000,
                    "content": {"content_type": "text", "parts": ["How do I refresh a token?"]},
                },
            },
            "n2": {
                "id": "n2",
                "parent": "n1",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "create_time": 1700000050,
                    "content": {
                        "content_type": "text",
                        "parts": ["Call the refresh endpoint:\n```bash\ncurl /refresh\n```"],
                    },
                },
            },
        },
    }
    return json.dumps([convo]).encode()


def test_chatgpt_detect():
    src = ChatGptSource()
    assert src.detect("conversations.json", _sample_export()) is True
    assert src.detect("random.json", b'{"hello": "world"}') is False
    assert src.detect("notjson.txt", b"not json at all") is False


def test_chatgpt_parse_export():
    src = ChatGptSource()
    sessions = list(src.parse_export(_sample_export()))
    assert len(sessions) == 1
    s = sessions[0]
    assert s["id"] == "chatgpt-abc"
    assert s["source"] == "chatgpt"
    assert s["title"] == "Auth help"
    assert len(s["turns"]) == 1
    turn = s["turns"][0]
    assert turn["user_message"] == "How do I refresh a token?"
    assert "refresh endpoint" in turn["assistant_response"]
    # The fenced block is extracted as a code block.
    assert turn["code_blocks"] == [{"language": "bash", "content": "curl /refresh"}]


def test_chatgpt_import_into_db(persist_session):
    """The importer's output persists and becomes searchable."""
    from mark import db, persist, search

    src = ChatGptSource()
    with db.connect() as conn:
        cur = conn.cursor()
        for session in src.parse_export(_sample_export()):
            persist.write_session(cur, session, light=True)
        conn.commit()
    res = search.search("refresh token", mode="keyword")
    assert any(r["id"] == "chatgpt-abc" for r in res)


def test_source_registry_is_well_formed():
    # Every watched source exposes a stable key and a default config.
    keys = [s.key for s in WATCHED_SOURCES]
    assert {"vscode", "copilot_cli", "cline", "cursor"} <= set(keys)
    assert len(keys) == len(set(keys)), "watched source keys must be unique"
    for s in WATCHED_SOURCES:
        cfg = s.default_config()
        assert cfg.key == s.key
    assert any(i.key == "chatgpt" for i in IMPORT_SOURCES)
