"""Source adapters: the ChatGPT importer end-to-end plus registry sanity."""

from __future__ import annotations

import json

from mark.sources import IMPORT_SOURCES, WATCHED_SOURCES, vscode
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
                    "content": {
                        "content_type": "text",
                        "parts": ["How do I refresh a token?"],
                    },
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
                        "parts": [
                            "Call the refresh endpoint:\n```bash\ncurl /refresh\n```"
                        ],
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


def _write_jsonl(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_vscode_jsonl_reconstructs_streamed_turns(tmp_path):
    """Newer VS Code chats are an append log: a kind=0 snapshot (empty), kind=2
    request appends, and kind=2 response-part batches streamed afterwards."""
    path = tmp_path / "workspaceStorage" / "ws1" / "chatSessions" / "s.jsonl"
    _write_jsonl(
        path,
        [
            {
                "kind": 0,
                "v": {
                    "version": 3,
                    "sessionId": "sess-xyz",
                    "creationDate": 1772045863205,
                    "responderUsername": "GitHub Copilot",
                    "requests": [],
                },
            },
            {
                "kind": 2,
                "v": [
                    {
                        "requestId": "request_1",
                        "timestamp": 1772045870000,
                        "message": {"text": "how do I refresh a token?"},
                        "response": [
                            {"kind": "mcpServersStarting", "didStartServerIds": []}
                        ],
                    }
                ],
            },
            # Assistant answer streams as a standalone response-part batch.
            {
                "kind": 2,
                "v": [
                    {
                        "kind": None,
                        "id": "p1",
                        "value": "Call the refresh endpoint and handle 401.",
                    }
                ],
            },
            {"kind": 1, "v": "positional scalar we intentionally ignore"},
        ],
    )
    s = vscode.parse_session(path, {})
    assert s is not None
    assert s["id"] == "sess-xyz"
    assert len(s["turns"]) == 1
    turn = s["turns"][0]
    assert turn["user_message"] == "how do I refresh a token?"
    assert "refresh endpoint" in turn["assistant_response"]


def test_vscode_legacy_json_still_parses(tmp_path):
    """The old whole-file ``.json`` format must keep working unchanged."""
    path = tmp_path / "workspaceStorage" / "ws1" / "chatSessions" / "s.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "sessionId": "legacy-1",
                "creationDate": 1772045863205,
                "requests": [
                    {"message": {"text": "hello"}, "response": [{"value": "hi there"}]}
                ],
            }
        )
    )
    s = vscode.parse_session(path, {})
    assert s is not None and s["id"] == "legacy-1"
    assert s["turns"][0]["assistant_response"] == "hi there"


def test_vscode_discovers_jsonl_and_empty_window(tmp_path):
    """Discovery must find ``.json``/``.jsonl`` workspace chats and empty-window chats."""
    ws = tmp_path / "workspaceStorage"
    (ws / "w1" / "chatSessions").mkdir(parents=True)
    (ws / "w1" / "chatSessions" / "a.json").write_text("{}")
    (ws / "w1" / "chatSessions" / "b.jsonl").write_text("{}")
    ew = tmp_path / "globalStorage" / "emptyWindowChatSessions"
    ew.mkdir(parents=True)
    (ew / "c.jsonl").write_text("{}")
    found = {p.name for p in vscode.iter_session_paths([ws])}
    assert {"a.json", "b.jsonl", "c.jsonl"} <= found
