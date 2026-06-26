from __future__ import annotations

import json
import sqlite3

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
            persist.write_session(cur, session)
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


def test_vscode_inline_reference_parts_are_spliced(tmp_path):
    """Agent turns interleave prose and inlineReference parts for symbol links."""
    path = tmp_path / "workspaceStorage" / "ws1" / "chatSessions" / "agent.jsonl"
    _write_jsonl(
        path,
        [
            {
                "kind": 0,
                "v": {"sessionId": "agent-inline", "creationDate": 1, "requests": []},
            },
            {
                "kind": 2,
                "v": [
                    {
                        "requestId": "r1",
                        "message": {"text": "fix it"},
                        "response": [
                            {"value": "The status payload now carries "},
                            {
                                "kind": "inlineReference",
                                "inlineReference": {
                                    "name": "resume_cmd",
                                    "location": {
                                        "uri": {
                                            "path": "/repo/mark/schemas.py",
                                            "scheme": "file",
                                        }
                                    },
                                },
                            },
                            {"value": " in the response."},
                        ],
                    }
                ],
            },
        ],
    )
    turn = vscode.parse_session(path, {})["turns"][0]
    assert turn["assistant_response"] == (
        "The status payload now carries `resume_cmd` in the response."
    )
    assert "/repo/mark/schemas.py" in turn["files"]


def test_vscode_tool_invocation_messages_are_captured(tmp_path):
    path = tmp_path / "workspaceStorage" / "ws1" / "chatSessions" / "tools.jsonl"
    _write_jsonl(
        path,
        [
            {
                "kind": 0,
                "v": {"sessionId": "agent-tools", "creationDate": 1, "requests": []},
            },
            {
                "kind": 2,
                "v": [
                    {
                        "requestId": "r1",
                        "message": {"text": "read the file"},
                        "response": [
                            {"value": "I'll inspect the module."},
                            {
                                "kind": "toolInvocationSerialized",
                                "toolId": "copilot_readFile",
                                "isComplete": True,
                                "invocationMessage": {
                                    "value": "Reading [](file:///repo/mark/persist.py)",
                                    "uris": {
                                        "file:///repo/mark/persist.py": {
                                            "path": "/repo/mark/persist.py",
                                            "scheme": "file",
                                        }
                                    },
                                },
                                "pastTenseMessage": {
                                    "value": "Read [](file:///repo/mark/persist.py)",
                                    "uris": {
                                        "file:///repo/mark/persist.py": {
                                            "path": "/repo/mark/persist.py",
                                            "scheme": "file",
                                        }
                                    },
                                },
                            },
                        ],
                    }
                ],
            },
        ],
    )
    turn = vscode.parse_session(path, {})["turns"][0]
    assert "I'll inspect the module." in turn["assistant_response"]
    assert "Read `persist.py`" in turn["assistant_response"]
    assert turn["tools"] == ["copilot_readFile"]
    assert "/repo/mark/persist.py" in turn["files"]


def test_vscode_text_edit_groups_become_code_fences(tmp_path):
    path = tmp_path / "workspaceStorage" / "ws1" / "chatSessions" / "edits.jsonl"
    _write_jsonl(
        path,
        [
            {
                "kind": 0,
                "v": {"sessionId": "agent-edits", "creationDate": 1, "requests": []},
            },
            {
                "kind": 2,
                "v": [
                    {
                        "requestId": "r1",
                        "message": {"text": "apply fix"},
                        "response": [
                            {"value": "Applying the change."},
                            {"value": "\n```\n\n```\n"},
                            {
                                "kind": "codeblockUri",
                                "uri": {
                                    "path": "/repo/mark/persist.py",
                                    "scheme": "file",
                                },
                            },
                            {
                                "kind": "textEditGroup",
                                "uri": {
                                    "path": "/repo/mark/persist.py",
                                    "scheme": "file",
                                },
                                "edits": [
                                    [{"text": "def write_session():\n    pass\n"}]
                                ],
                            },
                            {"value": "\n```\n\n```\n"},
                        ],
                    }
                ],
            },
        ],
    )
    turn = vscode.parse_session(path, {})["turns"][0]
    assert "Applying the change." in turn["assistant_response"]
    assert "```py\ndef write_session():\n    pass\n```" in turn["assistant_response"]
    assert "```\n\n```" not in turn["assistant_response"]
    assert turn["code_blocks"] == [
        {"language": "py", "content": "def write_session():\n    pass"}
    ]


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


_ASSISTANT_REPLY = "Use a refresh token.\n```bash\ncurl /refresh\n```"


def test_cline_parses_task_history(tmp_path):
    """A Cline task dir (api_conversation_history.json) parses to one costed turn."""
    from mark.sources.cline import _parse_cline_task

    task_dir = tmp_path / "1700000000000"  # digit name => recoverable timestamp
    task_dir.mkdir()
    messages = [
        {
            "role": "user",
            "content": "how do I fix the auth token timeout",
            "ts": 1700000000000,
        },
        {"role": "assistant", "content": _ASSISTANT_REPLY, "ts": 1700000001000},
    ]
    (task_dir / "api_conversation_history.json").write_text(json.dumps(messages))

    s = _parse_cline_task(task_dir, "cline")
    assert s is not None
    assert s["id"] == "cline-1700000000000"
    assert s["source"] == "cline"
    assert len(s["turns"]) == 1
    turn = s["turns"][0]
    assert turn["user_message"].startswith("how do I fix")
    assert "refresh token" in turn["assistant_response"]
    assert turn["code_blocks"] == [{"language": "bash", "content": "curl /refresh"}]
    assert turn["thinking"] == ""  # all adapters emit the thinking key
    assert s["metrics"]["tokens_estimated"] == 1
    assert s["created_at"] <= s["updated_at"]


def test_copilot_cli_indexes_store_sessions(tmp_path):
    """The Copilot CLI store (sessions+turns tables) is snapshotted and indexed."""
    from mark import config, db, search
    from mark.sources.copilot_cli import CopilotCliSource

    store = tmp_path / "session-store.db"
    con = sqlite3.connect(store)
    con.execute(
        "CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, summary TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    con.execute(
        "CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, "
        "assistant_response TEXT, timestamp TEXT)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (
            "sess1",
            "/Users/m/microsoft/myrepo",
            None,
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        ),
    )
    con.execute(
        "INSERT INTO turns VALUES (?,?,?,?,?)",
        (
            "sess1",
            0,
            "how do I fix the auth token timeout",
            _ASSISTANT_REPLY,
            "2026-01-01T00:00:00+00:00",
        ),
    )
    con.commit()
    con.close()

    cfg = config.SourceConfig(
        key="copilot_cli",
        roots=[store],
        options={"state_dir": str(tmp_path / "no-state")},  # no events.jsonl
    )
    with db.connect() as conn:
        cur = conn.cursor()
        counts = CopilotCliSource().ingest(cur, {}, cfg, rebuild=False)
        conn.commit()

    assert counts == {"added": 1, "updated": 0, "skipped": 0}
    s = search.get_session("sess1")
    assert s is not None
    assert s["repository"] == "myrepo"  # derived from the cwd
    assert s["turns"][0]["user_message"].startswith("how do I fix")
    assert "curl /refresh" in s["turns"][0]["assistant_response"]


def test_cursor_indexes_composer_from_vscdb(tmp_path):
    """A Cursor composer (inline conversation in state.vscdb) is parsed + indexed."""
    from mark import config, db, search
    from mark.sources.cursor import CursorSource

    store = tmp_path / "state.vscdb"
    con = sqlite3.connect(store)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    data = {
        "composerId": "abc",
        "name": "Auth help",
        "createdAt": 1700000000000,
        "lastUpdatedAt": 1700000100000,
        "conversation": [
            {"type": 1, "text": "how do I fix the auth token timeout"},
            {"type": 2, "text": _ASSISTANT_REPLY},
        ],
    }
    con.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)", ("composerData:abc", json.dumps(data))
    )
    con.commit()
    con.close()

    cfg = config.SourceConfig(
        key="cursor",
        roots=[store],
        options={"workspace_roots": [str(tmp_path / "ws")]},  # empty => no repo map
    )
    with db.connect() as conn:
        cur = conn.cursor()
        counts = CursorSource().ingest(cur, {}, cfg, rebuild=False)
        conn.commit()

    assert counts["added"] == 1
    s = search.get_session("cursor-abc")
    assert s is not None
    assert s["title"] == "Auth help"
    assert s["turns"][0]["user_message"].startswith("how do I fix")
    assert "refresh token" in s["turns"][0]["assistant_response"]
    assert "curl /refresh" in s["turns"][0]["assistant_response"]


def test_vscode_reingest_skips_unchanged_via_stat_cache(tmp_path):
    """A second scan of an unchanged file is skipped from its cheap stat alone,
    without re-parsing — and the signature is cached for next time."""
    from mark import config, db
    from mark.sources.vscode import VSCodeSource

    root = tmp_path / "workspaceStorage"
    path = root / "ws1" / "chatSessions" / "s.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "sessionId": "vs-1",
                "creationDate": 1,
                "requests": [
                    {"message": {"text": "hello"}, "response": [{"value": "hi"}]}
                ],
            }
        )
    )
    cfg = config.SourceConfig(key="vscode", roots=[root])
    src = VSCodeSource()
    with db.connect() as conn:
        cur = conn.cursor()
        first = src.ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        second = src.ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        cached = cur.execute(
            "SELECT signature FROM source_file_stat WHERE path = ?", (str(path),)
        ).fetchone()
    assert first == {"added": 1, "updated": 0, "skipped": 0}
    assert second == {"added": 0, "updated": 0, "skipped": 1}
    assert cached is not None


def test_copilot_cli_reingest_skips_snapshot_when_unchanged(tmp_path):
    """With no store change, a re-scan skips the whole-store backup + parse and
    reports the session as skipped via the read-only pre-check."""
    from mark import config, db
    from mark.sources.copilot_cli import CopilotCliSource

    store = tmp_path / "session-store.db"
    con = sqlite3.connect(store)
    con.execute(
        "CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, summary TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    con.execute(
        "CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, "
        "assistant_response TEXT, timestamp TEXT)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (
            "sess1",
            "/repo",
            None,
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
        ),
    )
    con.execute(
        "INSERT INTO turns VALUES (?,?,?,?,?)",
        ("sess1", 0, "hello", _ASSISTANT_REPLY, "2026-01-01T00:00:00+00:00"),
    )
    con.commit()
    con.close()

    cfg = config.SourceConfig(
        key="copilot_cli",
        roots=[store],
        options={"state_dir": str(tmp_path / "no-state")},
    )
    src = CopilotCliSource()
    with db.connect() as conn:
        cur = conn.cursor()
        first = src.ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        existing = {
            r["id"]: r["content_hash"]
            for r in cur.execute("SELECT id, content_hash FROM sessions")
        }
        second = src.ingest(cur, existing, cfg, rebuild=False)
        conn.commit()
    assert first["added"] == 1
    assert second == {"added": 0, "updated": 0, "skipped": 1}


def test_cursor_reingest_skips_unchanged_store(tmp_path):
    """A second scan of an unchanged Cursor store is skipped at the store level,
    without opening it or re-parsing any composer blob."""
    from mark import config, db
    from mark.sources.cursor import CursorSource

    store = tmp_path / "state.vscdb"
    con = sqlite3.connect(store)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    data = {
        "composerId": "abc",
        "name": "Auth help",
        "createdAt": 1700000000000,
        "lastUpdatedAt": 1700000100000,
        "conversation": [
            {"type": 1, "text": "how do I fix the auth token timeout"},
            {"type": 2, "text": _ASSISTANT_REPLY},
        ],
    }
    con.execute(
        "INSERT INTO cursorDiskKV VALUES (?,?)", ("composerData:abc", json.dumps(data))
    )
    con.commit()
    con.close()

    cfg = config.SourceConfig(
        key="cursor",
        roots=[store],
        options={"workspace_roots": [str(tmp_path / "ws")]},
    )
    src = CursorSource()
    with db.connect() as conn:
        cur = conn.cursor()
        first = src.ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        second = src.ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        cached = cur.execute(
            "SELECT signature FROM source_file_stat WHERE path = ?",
            (f"cursor:{store}",),
        ).fetchone()
    assert first["added"] == 1
    assert second == {"added": 0, "updated": 0, "skipped": 0}
    assert cached is not None
