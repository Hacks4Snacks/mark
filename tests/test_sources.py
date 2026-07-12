from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from mark.repositories import sessions as sessions_repo
from mark.sources import IMPORT_SOURCES, WATCHED_SOURCES, claude_code, vscode
from mark.sources.chatgpt import ChatGptSource
from mark.sources.grok import GrokSource


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


def _sample_grok_export() -> bytes:
    """An Enhanced Grok Export v2 payload: one Human + one Grok message."""
    export = {
        "exportDate": "2026-07-01T21:27:21.657Z",
        "exportVersion": "2.4.1",
        "platform": "grok",
        "messageCount": 2,
        "url": "https://grok.com/c/9a8a350b-45b4-4847-9c14-20acba8d2faa?rid=5debbe06",
        "conversation": [
            {
                "id": "msg_0",
                "speaker": "Human",
                "content": "How do I refresh a token? See https://example.com/docs",
                "mode": "deepsearch",
                "timestamp": "2026-07-01T21:27:21.655Z",
            },
            {
                "id": "msg_1",
                "speaker": "Grok",
                "content": "Call the refresh endpoint:\n```bash\ncurl /refresh\n```",
                "mode": "standard",
                "timestamp": "2026-07-01T21:27:21.656Z",
            },
        ],
        "statistics": {"humanMessages": 1, "grokMessages": 1},
    }
    return json.dumps(export).encode()


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


def test_grok_detect():
    src = GrokSource()
    assert src.detect("grok-export.json", _sample_grok_export()) is True
    # A ChatGPT export (a list of conversations) must not be claimed by Grok.
    assert src.detect("conversations.json", _sample_export()) is False
    assert src.detect("other.json", b'{"platform": "chatgpt"}') is False
    assert src.detect("notjson.txt", b"not json at all") is False


def test_grok_parse_export():
    src = GrokSource()
    sessions = list(src.parse_export(_sample_grok_export()))
    assert len(sessions) == 1
    s = sessions[0]
    # The stable id comes from the /c/<uuid> in the share URL.
    assert s["id"] == "grok-9a8a350b-45b4-4847-9c14-20acba8d2faa"
    assert s["source"] == "grok"
    assert s["responder"] == "Grok"
    assert s["source_path"].startswith("https://grok.com/c/")
    assert len(s["turns"]) == 1
    turn = s["turns"][0]
    # Human -> user, Grok -> assistant.
    assert turn["user_message"].startswith("How do I refresh a token?")
    assert "refresh endpoint" in turn["assistant_response"]
    # The deliberate mode is captured; the default "standard" is dropped.
    assert turn["thinking"] == "Mode: deepsearch"
    # Fenced blocks and URLs are extracted like the other adapters.
    assert turn["code_blocks"] == [{"language": "bash", "content": "curl /refresh"}]
    assert "https://example.com/docs" in turn["urls"]


def test_grok_import_into_db(persist_session):
    """The Grok importer's output persists and becomes searchable."""
    from mark import db, persist, search

    src = GrokSource()
    with db.connect() as conn:
        cur = conn.cursor()
        for session in src.parse_export(_sample_grok_export()):
            persist.write_session(cur, session)
        conn.commit()
    res = search.search("refresh token", mode="keyword")
    assert any(r["id"] == "grok-9a8a350b-45b4-4847-9c14-20acba8d2faa" for r in res)


def test_source_registry_is_well_formed():
    # Every watched source exposes a stable key and a default config.
    keys = [s.key for s in WATCHED_SOURCES]
    assert {
        "vscode",
        "copilot_cli",
        "copilot_memory",
        "cline",
        "cursor",
        "claude_code",
    } <= set(keys)
    assert len(keys) == len(set(keys)), "watched source keys must be unique"
    for s in WATCHED_SOURCES:
        cfg = s.default_config()
        assert cfg.key == s.key
    assert any(i.key == "chatgpt" for i in IMPORT_SOURCES)
    assert any(i.key == "grok" for i in IMPORT_SOURCES)


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
            "/home/dev/projects/myrepo",
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


def test_copilot_cli_events_inline_agentic_trace(tmp_path):
    """A mostly-autonomous run (one prompt, many tool turns) is reconstructed from
    events.jsonl as a full interleaved trace, not just its first and last prose."""
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
        ("sess1", "/repo", None, None, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
    )
    con.commit()
    con.close()

    edited = "/repo/src/app.py"
    long_cmd = (
        "set -euo pipefail; TOKEN=$(az account get-access-token --resource "
        "aaaaaa-1234-4567-bb17-11111111111 --query accessToken -o tsv); "
        'curl -sS -H "Authorization: Bearer $TOKEN" https://example/api | jq .'
    )
    multiline_cmd = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "print(Path('.').resolve())\n"
        "PY"
    )
    events = [
        {"type": "session.start", "data": {"selectedModel": "gpt-5.5"}},
        {"type": "user.message", "data": {"content": "fix the routing bug"}},
        {"type": "assistant.turn_start", "data": {"turnId": 0}},
        # The CLI emits an empty user.message between turn_start and the reply.
        {"type": "user.message", "data": {"content": ""}},
        {"type": "assistant.message", "data": {"content": "Let me investigate."}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "c1",
                "toolName": "view",
                "arguments": {"path": edited},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "c1", "success": True},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "c4",
                "toolName": "bash",
                "arguments": {"command": long_cmd},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "c4", "success": True},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "c5",
                "toolName": "bash",
                "arguments": {"command": multiline_cmd},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "c5", "success": True},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "c2",
                "toolName": "rg",
                "arguments": {"pattern": "resolve_route", "paths": "/repo"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "c2", "success": False},
        },
        {"type": "assistant.message", "data": {"content": "Found the cause."}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "c3",
                "toolName": "apply_patch",
                "arguments": f"*** Begin Patch\n*** Update File: {edited}\n@@\n-x\n+y\n",
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "c3", "success": True},
        },
        {"type": "assistant.message", "data": {"content": "Done, fix applied."}},
        {
            "type": "session.shutdown",
            "data": {"codeChanges": {"filesModified": [edited]}},
        },
    ]
    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    (sdir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )

    cfg = config.SourceConfig(
        key="copilot_cli", roots=[store], options={"state_dir": str(state_dir)}
    )
    with db.connect() as conn:
        cur = conn.cursor()
        counts = CopilotCliSource().ingest(cur, {}, cfg, rebuild=False)
        conn.commit()

    assert counts == {"added": 1, "updated": 0, "skipped": 0}
    s = search.get_session("sess1")
    assert s is not None
    assert len(s["turns"]) == 1  # one prompt -> one turn (the autonomous run)
    ar = s["turns"][0]["assistant_response"]
    # All three prose blocks survive, in order (not just first + last).
    i_a, i_b, i_c = (
        ar.find("Let me investigate."),
        ar.find("Found the cause."),
        ar.find("Done, fix applied."),
    )
    assert -1 < i_a < i_b < i_c
    # The tool calls are inlined in order with useful labels.
    assert "`▷ view` " + edited in ar
    assert "`▷ rg` resolve_route" in ar
    assert "`▷ apply_patch` " + edited in ar
    assert (
        ar.index("`▷ view`")
        < ar.index("Found the cause.")
        < ar.index("`▷ apply_patch`")
    )
    # A failed execution is annotated.
    assert "`▷ rg` resolve_route — failed" in ar
    # Long free-text arguments (a full shell command) are kept verbatim — there is
    # no truncation marker anywhere in the reconstructed trace.
    assert "`▷ bash` " + long_cmd in ar
    assert "…" not in ar
    # A multi-line command is preserved verbatim inside a fenced code block.
    assert "`▷ bash`\n```\n" + multiline_cmd + "\n```" in ar
    # Tool names and the git-diff-edited file are captured for the aside/search.
    tool_names = set(json.loads(s["turns"][0]["tools"]))
    assert {"view", "rg", "apply_patch", "bash"} <= tool_names
    assert any(f["file_path"] == edited for f in s["files"])


def test_copilot_cli_records_only_successful_write_paths(tmp_path):
    """Requested and failed writes stay in the trace but never become files."""
    from mark.sources.copilot_cli import _events_to_turns

    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    requested = "/repo/requested-only.txt"
    failed = "/repo/failed.txt"
    succeeded = "/repo/succeeded.txt"
    events = [
        {"type": "user.message", "data": {"content": "edit files"}},
        {
            "type": "assistant.message",
            "data": {
                "content": "Working on it.",
                "toolRequests": [
                    {
                        "name": "create_file",
                        "arguments": {"filePath": requested},
                    }
                ],
            },
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "failed",
                "toolName": "create_file",
                "arguments": {"filePath": failed},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "failed", "success": False},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "succeeded",
                "toolName": "edit_file",
                "arguments": {"filePath": succeeded},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "succeeded", "success": True},
        },
    ]
    (sdir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    parsed = _events_to_turns("sess1", state_dir)
    assert parsed is not None
    turns, files_modified = parsed
    assert files_modified == []
    assert turns[0]["files"] == [succeeded]
    assert requested not in turns[0]["files"]
    assert failed not in turns[0]["files"]
    assert "— failed" in turns[0]["assistant_response"]


def test_copilot_cli_duplicate_call_id_never_promotes_path(tmp_path):
    from mark.sources.copilot_cli import _events_to_turns

    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    events = [
        {"type": "user.message", "data": {"content": "edit"}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "duplicate",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/first.txt"},
            },
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "duplicate",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/never-completed.txt"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "duplicate", "success": True},
        },
    ]
    (sdir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    parsed = _events_to_turns("sess1", state_dir)
    assert parsed is not None
    turns, _ = parsed
    assert turns[0]["files"] == []


def test_copilot_cli_conflicting_completions_never_promote_path(tmp_path):
    from mark.sources.copilot_cli import _events_to_turns

    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    events = [
        {"type": "user.message", "data": {"content": "edit"}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "write",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/file.txt"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "write", "success": True},
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "write", "success": False},
        },
    ]
    (sdir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    parsed = _events_to_turns("sess1", state_dir)
    assert parsed is not None
    assert parsed[0][0]["files"] == []


def test_copilot_cli_call_id_reuse_across_turns_never_promotes(tmp_path):
    from mark.sources.copilot_cli import _events_to_turns

    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    events = [
        {"type": "user.message", "data": {"content": "first"}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "reused",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/first.txt"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "reused", "success": True},
        },
        {"type": "user.message", "data": {"content": "second"}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "reused",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/second.txt"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "reused", "success": True},
        },
    ]
    (sdir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    parsed = _events_to_turns("sess1", state_dir)
    assert parsed is not None
    assert all(turn["files"] == [] for turn in parsed[0])


def test_copilot_cli_invalid_terminal_order_and_type_never_promote(tmp_path):
    from mark.sources.copilot_cli import _events_to_turns

    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    events = [
        {"type": "user.message", "data": {"content": "edit"}},
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "before", "success": True},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "before",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/before.txt"},
            },
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "numeric",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/numeric.txt"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "numeric", "success": 1},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "paths",
                "toolName": "create_file",
                "arguments": {
                    "filePath": "/repo/one.txt",
                    "path": "/repo/two.txt",
                },
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "paths", "success": True},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "   ",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/blank-id.txt"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "   ", "success": True},
        },
    ]
    (sdir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    parsed = _events_to_turns("sess1", state_dir)
    assert parsed is not None
    assert parsed[0][0]["files"] == []


def test_copilot_cli_completion_changes_session_hash(tmp_path):
    from mark.sources.copilot_cli import _events_to_turns, _hash_cli_session

    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    path = sdir / "events.jsonl"
    events = [
        {"type": "user.message", "data": {"content": "edit"}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "write",
                "toolName": "create_file",
                "arguments": {"filePath": "/repo/delayed.txt"},
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    before = _events_to_turns("sess1", state_dir)
    assert before is not None
    before_hash = _hash_cli_session("same", before[0], before[1])

    events.append(
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "write", "success": True},
        }
    )
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    after = _events_to_turns("sess1", state_dir)
    assert after is not None
    after_hash = _hash_cli_session("same", after[0], after[1])

    assert before[0][0]["files"] == []
    assert after[0][0]["files"] == ["/repo/delayed.txt"]
    assert before_hash != after_hash


def test_copilot_cli_fingerprint_changes_with_event_log(tmp_path):
    from mark import config
    from mark.sources.copilot_cli import CopilotCliSource

    store = tmp_path / "session-store.db"
    store.write_bytes(b"store")
    state_dir = tmp_path / "session-state"
    events = state_dir / "sess1" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text('{"type":"user.message"}\n')
    cfg = config.SourceConfig(
        key="copilot_cli",
        roots=[store],
        options={"state_dir": str(state_dir)},
    )
    source = CopilotCliSource()
    before = source.fingerprint(cfg)
    events.write_text(
        '{"type":"user.message"}\n'
        '{"type":"tool.execution_complete","data":{"success":true}}\n'
    )
    after = source.fingerprint(cfg)

    assert before != after


def test_copilot_cli_reingests_delayed_completion(tmp_path):
    from mark import config, db
    from mark.sources.copilot_cli import CopilotCliSource

    workspace = tmp_path / "repo"
    workspace.mkdir()
    created = workspace / "created.txt"
    created.write_text("captured after completion")
    store = tmp_path / "session-store.db"
    con = sqlite3.connect(store)
    con.execute(
        "CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, summary TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (
            "sess1",
            str(workspace),
            None,
            None,
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
        ),
    )
    con.commit()
    con.close()

    state_dir = tmp_path / "session-state"
    events_path = state_dir / "sess1" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    events = [
        {"type": "user.message", "data": {"content": "create file"}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "write",
                "toolName": "create_file",
                "arguments": {"filePath": str(created)},
            },
        },
    ]
    events_path.write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )
    cfg = config.SourceConfig(
        key="copilot_cli",
        roots=[store],
        options={"state_dir": str(state_dir)},
    )
    source = CopilotCliSource()
    with db.connect() as conn:
        cur = conn.cursor()
        first = source.ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        existing = {
            row["id"]: row["content_hash"]
            for row in cur.execute("SELECT id, content_hash FROM sessions")
        }
        assert first["added"] == 1
        assert (
            cur.execute(
                "SELECT COUNT(*) FROM documents WHERE session_id = 'sess1'"
            ).fetchone()[0]
            == 0
        )

        events.append(
            {
                "type": "tool.execution_complete",
                "data": {"toolCallId": "write", "success": True},
            }
        )
        events_path.write_text(
            "\n".join(json.dumps(event) for event in events), encoding="utf-8"
        )
        second = source.ingest(cur, existing, cfg, rebuild=False)
        conn.commit()
        attachment = cur.execute(
            "SELECT storage_kind, sha256 FROM documents "
            "WHERE session_id = 'sess1' AND kind = 'attachment'"
        ).fetchone()

    assert second["updated"] == 1
    assert attachment["storage_kind"] == "managed"
    assert attachment["sha256"]


def test_copilot_cli_ingest_snapshots_only_workspace_files(tmp_path):
    from mark import attachments, config, db
    from mark.sources.copilot_cli import CopilotCliSource

    workspace = tmp_path / "repo"
    workspace.mkdir()
    inside = workspace / "inside.bin"
    inside_bytes = b"\x00inside snapshot\xff"
    inside.write_bytes(inside_bytes)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret")

    store = tmp_path / "session-store.db"
    con = sqlite3.connect(store)
    con.execute(
        "CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, summary TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (
            "sess1",
            str(workspace),
            None,
            None,
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
        ),
    )
    con.commit()
    con.close()

    events = [
        {"type": "user.message", "data": {"content": "write files"}},
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "inside",
                "toolName": "create_file",
                "arguments": {"filePath": str(inside)},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "inside", "success": True},
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "outside",
                "toolName": "create_file",
                "arguments": {"filePath": str(outside)},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "outside", "success": True},
        },
    ]
    state_dir = tmp_path / "session-state"
    sdir = state_dir / "sess1"
    sdir.mkdir(parents=True)
    (sdir / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events), encoding="utf-8"
    )

    cfg = config.SourceConfig(
        key="copilot_cli",
        roots=[store],
        options={"state_dir": str(state_dir)},
    )
    with db.connect() as conn:
        result = CopilotCliSource().ingest(conn.cursor(), {}, cfg, rebuild=False)
        conn.commit()
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT filename, stored_path, content, sha256, size_bytes "
                "FROM documents "
                "WHERE session_id = 'sess1' AND kind = 'attachment'"
            )
        ]

    assert result["added"] == 1
    assert [row["filename"] for row in rows] == ["inside.bin"]
    snapshot = attachments.managed_snapshot(
        rows[0]["stored_path"],
        sha256=rows[0]["sha256"],
        size_bytes=rows[0]["size_bytes"],
    )
    assert snapshot is not None
    assert snapshot.read_bytes() == inside_bytes
    assert rows[0]["content"] is None
    assert str(outside) not in {row["stored_path"] for row in rows}


def test_attachment_snapshot_is_workspace_contained_and_immutable(tmp_path):
    from mark import attachments

    workspace = tmp_path / "repo"
    workspace.mkdir()
    original = workspace / "artifact.bin"
    original_bytes = b"\x00original binary\xff"
    original.write_bytes(original_bytes)
    outside = tmp_path / "outside.txt"
    outside.write_text("private outside data")
    escape = workspace / "escape.txt"
    escape.symlink_to(outside)

    captured = attachments.snapshot_file(
        "artifact.bin", workspace=str(workspace), session_id="session-1"
    )
    assert captured is not None
    assert captured["content"] is None
    snapshot = attachments.managed_snapshot(
        captured["stored_path"],
        sha256=captured["sha256"],
        size_bytes=captured["size_bytes"],
    )
    assert snapshot is not None
    assert snapshot.read_bytes() == original_bytes
    assert snapshot.stat().st_mode & 0o777 == 0o600

    original.write_bytes(b"changed after ingest")
    assert snapshot.read_bytes() == original_bytes
    assert (
        attachments.snapshot_file(
            str(outside), workspace=str(workspace), session_id="session-1"
        )
        is None
    )
    assert (
        attachments.snapshot_file(
            str(escape), workspace=str(workspace), session_id="session-1"
        )
        is None
    )
    assert attachments.managed_snapshot(str(outside)) is None


def test_attachment_snapshot_rejects_broad_workspace(tmp_path, monkeypatch):
    from mark import attachments

    home = tmp_path / "home"
    home.mkdir()
    secret = home / "secret.txt"
    secret.write_text("secret")
    monkeypatch.setattr(attachments.Path, "home", lambda: home)

    assert (
        attachments.snapshot_file(
            str(secret), workspace=str(home), session_id="session-1"
        )
        is None
    )
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(attachments.tempfile, "gettempdir", lambda: str(shared))
    assert (
        attachments.snapshot_file(
            str(secret), workspace=str(shared), session_id="session-1"
        )
        is None
    )
    for broad in ("/System", "/private", "/Volumes"):
        if Path(broad).is_dir():
            assert attachments._trusted_root(broad, reject_broad=True) is None


def test_attachment_snapshot_rejects_macos_firmlink_alias(tmp_path):
    from mark import attachments

    alias_root = Path("/System/Volumes/Data/private/tmp")
    canonical_root = Path("/private/tmp")
    if not alias_root.is_dir() or not canonical_root.is_dir():
        return
    try:
        same_root = os.path.samefile(alias_root, canonical_root)
    except OSError:
        return
    if not same_root:
        return

    fixture = canonical_root / f"mark-f01-{tmp_path.name}.txt"
    fixture.write_text("fixture only")
    try:
        alias_fixture = alias_root / fixture.name
        assert (
            attachments.snapshot_file(
                str(alias_fixture),
                workspace=str(alias_root),
                session_id="session-1",
            )
            is None
        )
    finally:
        fixture.unlink(missing_ok=True)


def test_attachment_snapshot_caps_content(tmp_path, monkeypatch):
    from mark import attachments, config

    workspace = tmp_path / "repo"
    workspace.mkdir()
    large = workspace / "large.txt"
    large.write_text("too large")
    monkeypatch.setattr(config, "MAX_ATTACHMENT_BYTES", 4)

    captured = attachments.snapshot_file(
        str(large), workspace=str(workspace), session_id="session-1"
    )
    assert captured is not None
    assert captured["size_bytes"] == len("too large")
    assert captured["content"] is None
    assert captured["stored_path"] is None


def test_attachment_snapshot_accepts_exact_cap(tmp_path, monkeypatch):
    from mark import attachments, config

    workspace = tmp_path / "repo"
    workspace.mkdir()
    exact = workspace / "exact.bin"
    exact.write_bytes(b"1234")
    monkeypatch.setattr(config, "MAX_ATTACHMENT_BYTES", 4)

    captured = attachments.snapshot_file(
        str(exact), workspace=str(workspace), session_id="session-1"
    )
    assert captured is not None
    assert captured["storage_kind"] == "managed"
    assert attachments.attachment_bytes(captured) == b"1234"


def test_attachment_reingest_removes_replaced_managed_blob(
    tmp_path, make_session, persist_session
):
    from mark import attachments

    workspace = tmp_path / "repo"
    workspace.mkdir()
    original = workspace / "artifact.bin"
    original.write_bytes(b"first version")
    first = attachments.snapshot_file(
        str(original), workspace=str(workspace), session_id="session-1"
    )
    assert first is not None
    session = make_session(sid="session-1", asst="first answer")
    session["attachments"] = [first]
    persist_session(session)
    first_path = Path(first["stored_path"])
    assert first_path.exists()

    original.write_bytes(b"second version")
    second = attachments.snapshot_file(
        str(original), workspace=str(workspace), session_id="session-1"
    )
    assert second is not None
    changed = make_session(sid="session-1", asst="changed answer")
    changed["attachments"] = [second]
    persist_session(changed)
    attachments.cleanup_unreferenced()

    assert not first_path.exists()
    assert Path(second["stored_path"]).exists()


def test_attachment_cleanup_preserves_shared_managed_blob(
    tmp_path, make_session, persist_session
):
    from mark import attachments

    workspace = tmp_path / "repo"
    workspace.mkdir()
    original = workspace / "shared.bin"
    original.write_bytes(b"shared bytes")
    captured = attachments.snapshot_file(
        str(original), workspace=str(workspace), session_id="shared"
    )
    assert captured is not None
    for sid in ("one", "two"):
        session = make_session(sid=sid)
        session["attachments"] = [captured]
        persist_session(session)

    shared_path = Path(captured["stored_path"])
    assert sessions_repo.purge("one") is True
    assert shared_path.exists()
    assert sessions_repo.purge("two") is True
    assert not shared_path.exists()


def test_attachment_cleanup_removes_unreferenced_owned_files(tmp_path):
    from mark import attachments, config

    config.ensure_dirs()
    upload_orphan = config.UPLOADS_DIR / "orphan.txt"
    upload_orphan.write_text("orphan")
    attachment_orphan = attachments.snapshot_root() / "orphan" / "blob"
    attachment_orphan.parent.mkdir(parents=True)
    attachment_orphan.write_bytes(b"orphan")

    assert attachments.cleanup_unreferenced() == 2
    assert not upload_orphan.exists()
    assert not attachment_orphan.exists()


def test_upload_failure_removes_staged_file(monkeypatch):
    from mark import config, uploads

    def fail_index(**kwargs):
        raise RuntimeError("index failed")

    monkeypatch.setattr(uploads, "_index_document_locked", fail_index)
    with pytest.raises(RuntimeError, match="index failed"):
        uploads.add_file("failed.txt", b"partial bytes", "text/plain")

    assert not list(config.UPLOADS_DIR.glob("*"))


def test_inline_attachment_requires_supported_exact_provenance(monkeypatch):
    import hashlib

    from mark import attachments, config

    raw = b"trusted"
    base = {
        "capture_version": attachments.CAPTURE_VERSION,
        "storage_kind": "inline",
        "content": raw.decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    assert attachments.attachment_bytes(base) == raw
    assert attachments.attachment_bytes({**base, "capture_version": 999}) is None
    assert attachments.attachment_bytes({**base, "size_bytes": 1}) is None
    assert attachments.attachment_bytes({**base, "sha256": "0" * 64}) is None
    monkeypatch.setattr(config, "MAX_ATTACHMENT_BYTES", len(raw) - 1)
    assert attachments.attachment_bytes(base) is None


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


def _make_memory_workspace(tmp_path):
    """A workspaceStorage root with a workspace.json (repo mapping) plus two
    Copilot memory notes: one repo-scoped, one session-scoped."""
    import base64

    ws = tmp_path / "workspaceStorage"
    (ws / "ws1").mkdir(parents=True)
    (ws / "ws1" / "workspace.json").write_text(
        json.dumps({"folder": "file:///home/me/code/myrepo"})
    )
    mem = ws / "ws1" / "GitHub.copilot-chat" / "memory-tool" / "memories"
    (mem / "repo").mkdir(parents=True)
    (mem / "repo" / "conventions.md").write_text(
        "# Conventions\n\nUse refresh tokens for auth.\n```bash\ncurl /refresh\n```\n"
    )
    sid = "f0bd00cf-c81d-4435-aa27-52373371f0f5"
    b64 = base64.urlsafe_b64encode(sid.encode()).decode().rstrip("=")
    (mem / b64).mkdir(parents=True)
    (mem / b64 / "plan.md").write_text("# Plan\n\nStep one: ship the feature.\n")
    return ws


def test_copilot_memory_indexes_repo_scope_not_session(tmp_path):
    """Repo-scoped notes index as their own `copilot_memory` session (no LLM cost);
    session-scoped notes are left to the VS Code source, not indexed here."""
    from mark import config, db, search
    from mark.sources.copilot_memory import CopilotMemorySource

    ws = _make_memory_workspace(tmp_path)
    cfg = config.SourceConfig(key="copilot_memory", roots=[ws])
    src = CopilotMemorySource()
    with db.connect() as conn:
        cur = conn.cursor()
        res = src.ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        rows = {
            r["title"]: dict(r)
            for r in cur.execute(
                "SELECT id, title, source, repository, workspace_id, "
                "est_cost_usd, input_tokens FROM sessions"
            )
        }
    assert res == {"added": 1, "updated": 0, "skipped": 0}
    assert set(rows) == {"Repo memory · conventions"}  # session note not indexed here
    repo_row = rows["Repo memory · conventions"]
    assert repo_row["source"] == "copilot_memory"
    assert repo_row["repository"] == "myrepo"
    assert repo_row["workspace_id"] == "ws1"
    assert repo_row["est_cost_usd"] == 0.0  # memory is knowledge, not spend
    assert repo_row["input_tokens"] == 0
    # The note body is searchable and traces back to the memory session.
    hits = search.search("refresh", mode="keyword")
    assert any(h["id"] == repo_row["id"] for h in hits)


def test_copilot_memory_reingest_skips_unchanged(tmp_path):
    """A second scan of unchanged memory notes is skipped from the stat cache."""
    from mark import config, db
    from mark.sources.copilot_memory import CopilotMemorySource

    ws = _make_memory_workspace(tmp_path)
    cfg = config.SourceConfig(key="copilot_memory", roots=[ws])
    src = CopilotMemorySource()
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
    assert first == {"added": 1, "updated": 0, "skipped": 0}
    assert second == {"added": 0, "updated": 0, "skipped": 1}


def test_copilot_memory_multiroot_attributes_repo_per_file(tmp_path):
    """In a multi-root workspace (workspace.json -> .code-workspace descriptor),
    each repo note is attributed to the folder whose name best matches the file,
    never the literal 'workspace.json' descriptor."""
    from mark import config, db
    from mark.sources.copilot_memory import CopilotMemorySource

    ws = tmp_path / "workspaceStorage"
    (ws / "ws1").mkdir(parents=True)
    desc = tmp_path / "Workspaces" / "123" / "workspace.json"
    desc.parent.mkdir(parents=True)
    desc.write_text(
        json.dumps(
            {"folders": [{"path": "/code/frontend-app"}, {"path": "/code/backend-api"}]}
        )
    )
    (ws / "ws1" / "workspace.json").write_text(json.dumps({"workspace": desc.as_uri()}))
    mem = ws / "ws1" / "GitHub.copilot-chat" / "memory-tool" / "memories" / "repo"
    mem.mkdir(parents=True)
    (mem / "frontend-app.md").write_text("# FE\n\nRoutes and views.\n")
    (mem / "backend-api-notes.md").write_text("# BE\n\nEndpoints and auth.\n")

    cfg = config.SourceConfig(key="copilot_memory", roots=[ws])
    with db.connect() as conn:
        cur = conn.cursor()
        CopilotMemorySource().ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        repos = {
            r["title"]: r["repository"]
            for r in cur.execute("SELECT title, repository FROM sessions")
        }
    assert repos["Repo memory · frontend-app"] == "frontend-app"
    assert repos["Repo memory · backend-api-notes"] == "backend-api"


def test_vscode_attaches_session_memory(tmp_path):
    """A session-scoped memory note is attached to the chat that produced it (an
    agent attachment on the VS Code session), not indexed as a separate session."""
    import base64

    from mark import config, db
    from mark.sources.vscode import VSCodeSource

    ws = tmp_path / "workspaceStorage"
    sid = "11111111-2222-3333-4444-555555555555"
    chat = ws / "wsA" / "chatSessions" / f"{sid}.json"
    chat.parent.mkdir(parents=True)
    chat.write_text(
        json.dumps(
            {
                "sessionId": sid,
                "creationDate": 1,
                "requests": [
                    {"message": {"text": "hello"}, "response": [{"value": "hi"}]}
                ],
            }
        )
    )
    b64 = base64.urlsafe_b64encode(sid.encode()).decode().rstrip("=")
    memdir = ws / "wsA" / "GitHub.copilot-chat" / "memory-tool" / "memories" / b64
    memdir.mkdir(parents=True)
    (memdir / "plan.md").write_text("# Plan\n\nRemember: use refresh tokens.\n")

    cfg = config.SourceConfig(key="vscode", roots=[ws])
    with db.connect() as conn:
        cur = conn.cursor()
        res = VSCodeSource().ingest(cur, {}, cfg, rebuild=False)
        conn.commit()
        atts = [
            dict(r)
            for r in cur.execute(
                "SELECT kind, filename, content FROM documents WHERE session_id = ?",
                (sid,),
            )
        ]
    assert res["added"] == 1
    assert len(atts) == 1
    assert atts[0]["kind"] == "attachment"
    assert atts[0]["filename"] == "plan.md"
    assert "refresh tokens" in atts[0]["content"]


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


def test_ingest_all_skips_unchanged_source(monkeypatch):
    """ingest_all skips a source whose own fingerprint is unchanged, so a pass
    triggered by another (active) source doesn't re-scan idle ones."""
    from mark import ingest
    from mark.sources.base import WatchedSource

    calls = {"n": 0}

    class FakeSource(WatchedSource):
        key = "fake"
        row_sources = ("fake",)

        def fingerprint(self, cfg) -> str:
            return "constant-fp"

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            calls["n"] += 1
            return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest, "WATCHED_SOURCES", [FakeSource()])
    ingest.ingest_all(do_embed=False)
    ingest.ingest_all(do_embed=False)
    assert calls["n"] == 1  # second pass skipped: fingerprint unchanged


def test_sources_fingerprint_isolates_broken_adapter(monkeypatch):
    from mark import ingest
    from mark.sources.base import WatchedSource

    healthy_called = False

    class BrokenSource(WatchedSource):
        key = "broken"

        def fingerprint(self, cfg) -> str:
            raise RuntimeError("boom")

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            return {"added": 0, "updated": 0, "skipped": 0}

    class HealthySource(WatchedSource):
        key = "healthy"

        def fingerprint(self, cfg) -> str:
            nonlocal healthy_called
            healthy_called = True
            return "changed"

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            return {"added": 0, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest, "WATCHED_SOURCES", [BrokenSource(), HealthySource()])
    snapshot = ingest.sources_fingerprint_snapshot()

    assert healthy_called is True
    assert snapshot.value == "broken=!error|healthy=changed"
    assert snapshot.errors == {"broken": "boom"}


def test_ingest_all_isolates_failed_source_and_continues(monkeypatch, make_session):
    """A broken adapter rolls back its partial writes without blocking later ones."""
    from mark import db, ingest
    from mark.sources.base import WatchedSource

    class FailingSource(WatchedSource):
        key = "failing"
        row_sources = ("failing",)

        def fingerprint(self, cfg) -> str:
            return "failing-fp"

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            ingest.persist.write_session(
                cur,
                make_session(
                    sid="rolled-back",
                    user="partial source content",
                ),
            )
            raise RuntimeError("broken adapter")

    class HealthySource(WatchedSource):
        key = "healthy"
        row_sources = ("healthy",)

        def fingerprint(self, cfg) -> str:
            return "healthy-fp"

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            cur.execute(
                "INSERT INTO source_file_stat(path, signature) VALUES (?, ?)",
                ("healthy-write", "committed"),
            )
            return {"added": 1, "updated": 0, "skipped": 0}

    monkeypatch.setattr(ingest, "WATCHED_SOURCES", [FailingSource(), HealthySource()])
    result = ingest.ingest_all(do_embed=False)

    assert result["added"] == 1
    assert result["errors"] == {"failing": "broken adapter"}
    assert result["sources"]["failing"]["status"] == "error"
    assert result["sources"]["healthy"]["status"] == "ok"
    with db.cursor() as cur:
        for table, id_column in (
            ("sessions", "id"),
            ("chunks", "session_id"),
            ("search_index", "session_id"),
        ):
            assert (
                cur.execute(
                    f"SELECT 1 FROM {table} WHERE {id_column} = 'rolled-back'"
                ).fetchone()
                is None
            ), table
        assert (
            cur.execute(
                "SELECT 1 FROM source_file_stat WHERE path = 'srcfp:failing'"
            ).fetchone()
            is None
        )
        assert (
            cur.execute(
                "SELECT 1 FROM source_file_stat WHERE path = 'healthy-write'"
            ).fetchone()
            is not None
        )
        assert (
            cur.execute(
                "SELECT signature FROM source_file_stat WHERE path = 'srcfp:healthy'"
            ).fetchone()[0]
            == "healthy-fp"
        )


def test_ingest_all_does_not_count_rolled_back_source(monkeypatch):
    from mark import db, ingest
    from mark.sources.base import WatchedSource

    class Source(WatchedSource):
        key = "late-failure"
        row_sources = ("late-failure",)

        def fingerprint(self, cfg) -> str:
            return "source-fp"

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            cur.execute(
                "INSERT INTO source_file_stat(path, signature) VALUES (?, ?)",
                ("source-write", "must-rollback"),
            )
            return {"added": 1, "updated": 0, "skipped": 0}

    def fail_fingerprint(cur, path, signature):
        raise RuntimeError("cannot persist fingerprint")

    monkeypatch.setattr(ingest, "WATCHED_SOURCES", [Source()])
    monkeypatch.setattr(ingest.persist, "record_file_signature", fail_fingerprint)
    result = ingest.ingest_all(do_embed=False)

    assert result["added"] == 0
    assert result["errors"] == {"late-failure": "cannot persist fingerprint"}
    with db.cursor() as cur:
        assert (
            cur.execute(
                "SELECT 1 FROM source_file_stat WHERE path = 'source-write'"
            ).fetchone()
            is None
        )


def test_ingest_all_retries_source_after_recovery(monkeypatch):
    from mark import db, ingest
    from mark.sources.base import WatchedSource

    calls = 0

    class Source(WatchedSource):
        key = "recovering"
        row_sources = ("recovering",)

        def fingerprint(self, cfg) -> str:
            return "recovering-fp"

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary failure")
            return {"added": 0, "updated": 1, "skipped": 0}

    monkeypatch.setattr(ingest, "WATCHED_SOURCES", [Source()])
    first = ingest.ingest_all(do_embed=False)
    second = ingest.ingest_all(do_embed=False)

    assert first["errors"] == {"recovering": "temporary failure"}
    assert second["errors"] == {}
    assert second["updated"] == 1
    assert calls == 2
    with db.cursor() as cur:
        assert (
            cur.execute(
                "SELECT signature FROM source_file_stat "
                "WHERE path = 'srcfp:recovering'"
            ).fetchone()[0]
            == "recovering-fp"
        )


def test_ingest_all_invalidates_prior_fingerprint_after_failure(monkeypatch):
    from mark import db, ingest
    from mark.sources.base import WatchedSource

    class Source(WatchedSource):
        key = "rebuild-failure"
        row_sources = ("rebuild-failure",)

        def fingerprint(self, cfg) -> str:
            return "same-fp"

        def ingest(self, cur, existing, cfg, *, rebuild, progress=None):
            raise RuntimeError("rebuild failed")

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO source_file_stat(path, signature) VALUES (?, ?)",
            ("srcfp:rebuild-failure", "same-fp"),
        )
    monkeypatch.setattr(ingest, "WATCHED_SOURCES", [Source()])

    result = ingest.ingest_all(rebuild=True, do_embed=False)
    assert result["errors"] == {"rebuild-failure": "rebuild failed"}
    with db.cursor() as cur:
        assert (
            cur.execute(
                "SELECT 1 FROM source_file_stat " "WHERE path = 'srcfp:rebuild-failure'"
            ).fetchone()
            is None
        )


def test_ingest_entry_points_share_one_process_gate(monkeypatch):
    from mark import ingest, uploads

    class TrackingGate:
        def __init__(self):
            self.lock = threading.Lock()
            self.condition = threading.Condition()
            self.attempts = 0

        def __enter__(self):
            with self.condition:
                self.attempts += 1
                self.condition.notify_all()
            self.lock.acquire()

        def __exit__(self, exc_type, exc, tb):
            self.lock.release()

    gate = TrackingGate()
    entered = []

    def fake_ingest_all(**kwargs):
        entered.append("watched")
        return {"added": 0, "updated": 0, "skipped": 0}

    def fake_import_export(*args, **kwargs):
        entered.append("import")
        return {"matched": None, "imported": 0}

    def fake_index_document(**kwargs):
        entered.append("document")
        return "note-id"

    monkeypatch.setattr(ingest, "_ingest_gate", gate)
    monkeypatch.setattr(ingest, "_ingest_all", fake_ingest_all)
    monkeypatch.setattr(ingest, "_import_export", fake_import_export)
    monkeypatch.setattr(uploads, "_index_document_locked", fake_index_document)

    callers = []
    gate.lock.acquire()
    try:
        callers = [
            threading.Thread(target=lambda: ingest.ingest_all(do_embed=False)),
            threading.Thread(target=lambda: ingest.import_export("x", b"x")),
            threading.Thread(
                target=lambda: uploads._index_document(
                    title="note",
                    kind="note",
                    content="body",
                )
            ),
        ]
        for caller in callers:
            caller.start()
        with gate.condition:
            assert gate.condition.wait_for(lambda: gate.attempts == 3, timeout=2)
        assert entered == []
    finally:
        gate.lock.release()
    for caller in callers:
        caller.join(timeout=2)
        assert not caller.is_alive()
    assert sorted(entered) == ["document", "import", "watched"]


def test_snapshot_sqlite_falls_back_to_filecopy(tmp_path, monkeypatch):
    """When the source can't be opened read-only (a WAL DB on a read-only mount),
    snapshot_sqlite falls back to a filesystem copy that is still readable."""
    import sqlite3 as _sqlite3

    from mark import config
    from mark.sources import base

    src = tmp_path / "store.vscdb"
    con = _sqlite3.connect(src)
    con.execute("CREATE TABLE t (k TEXT, v TEXT)")
    con.execute("INSERT INTO t VALUES ('a', '1')")
    con.commit()
    con.close()

    real_connect = _sqlite3.connect

    def fake_connect(target, *args, **kwargs):
        if isinstance(target, str) and "mode=ro" in target:
            raise _sqlite3.OperationalError("unable to open database file")
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(base.sqlite3, "connect", fake_connect)

    dest = config.DATA_DIR / "_snap_test.db"
    base.snapshot_sqlite(src, dest)
    con = real_connect(dest)
    try:
        assert con.execute("SELECT v FROM t WHERE k = 'a'").fetchone()[0] == "1"
    finally:
        con.close()
    base.cleanup_snapshot(dest)
    assert not dest.exists()


def _claude_transcript() -> list[dict]:
    """A realistic Claude Code ``<session>.jsonl`` transcript (enveloped events).

    One prompt drives a multi-step agent reply (thinking + code + a Write tool +
    its tool_result + a follow-up); a second prompt starts a second turn. A
    summary line supplies the title and an ``isMeta`` system-reminder must not
    become its own turn.
    """
    cwd = "/home/dev/projects/myrepo"
    return [
        {"type": "summary", "summary": "Refresh token help", "leafUuid": "u-last"},
        {
            "type": "user",
            "isMeta": True,
            "timestamp": "2026-06-23T10:00:00.000Z",
            "cwd": cwd,
            "gitBranch": "main",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<system-reminder>be concise</system-reminder>",
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-06-23T10:00:01.000Z",
            "cwd": cwd,
            "gitBranch": "main",
            "message": {"role": "user", "content": "How do I refresh a token?"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-23T10:00:05.000Z",
            "cwd": cwd,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4.8",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 100,
                },
                "content": [
                    {"type": "thinking", "thinking": "The user wants a token refresh."},
                    {
                        "type": "text",
                        "text": "Call the refresh endpoint:\n```bash\ncurl /refresh\n```",
                    },
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": f"{cwd}/auth.py", "content": "..."},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-06-23T10:00:06.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "File written",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-23T10:00:07.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4.8",
                "usage": {
                    "input_tokens": 1200,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 300,
                },
                "content": [{"type": "text", "text": "Done. The token now refreshes."}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-06-23T10:00:10.000Z",
            "cwd": cwd,
            "message": {"role": "user", "content": "Thanks, now add a test"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-06-23T10:00:12.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4.8",
                "usage": {"input_tokens": 1500, "output_tokens": 30},
                "content": [{"type": "text", "text": "Added tests."}],
            },
        },
    ]


def test_claude_code_parses_transcript(tmp_path):
    """A Claude Code JSONL transcript parses to costed, multi-step turns."""
    path = tmp_path / "projects" / "-home-dev-projects-myrepo" / "sess1.jsonl"
    _write_jsonl(path, _claude_transcript())

    s = claude_code.parse_transcript(path)
    assert s is not None
    assert s["id"] == "claude-code-sess1"
    assert s["source"] == "claude-code"
    assert s["responder"] == "Claude Code"
    # Title comes from the transcript's own summary line.
    assert s["title"] == "Refresh token help"
    # cwd is read from the events, not decoded from the (lossy) directory name.
    assert s["repository"] == "myrepo"
    assert s["repo_path"] == "/home/dev/projects/myrepo"

    # The isMeta system-reminder is not its own turn; one prompt + its agent run
    # is a single turn, so there are two turns total.
    assert len(s["turns"]) == 2
    t0 = s["turns"][0]
    assert t0["user_message"] == "How do I refresh a token?"
    assert "refresh endpoint" in t0["assistant_response"]
    assert "Done. The token now refreshes." in t0["assistant_response"]
    assert t0["code_blocks"] == [{"language": "bash", "content": "curl /refresh"}]
    assert t0["thinking"] == "The user wants a token refresh."
    assert "Write" in t0["tools"]
    assert "/home/dev/projects/myrepo/auth.py" in t0["files"]
    assert s["turns"][1]["user_message"] == "Thanks, now add a test"
    assert "Added tests." in s["turns"][1]["assistant_response"]

    # Real per-message usage is summed across the whole session (not estimated).
    m = s["metrics"]
    assert m["model"] == "claude-opus-4.8"
    assert m["input_tokens"] == 3700
    assert m["output_tokens"] == 100
    assert m["tokens_estimated"] == 0
    assert m["est_cost_usd"] > 0
    assert s["created_at"] <= s["updated_at"]


def test_claude_code_indexes_transcripts(tmp_path):
    """The adapter discovers projects/*/<session>.jsonl and indexes them."""
    from mark import config, db, search
    from mark.sources.claude_code import ClaudeCodeSource

    root = tmp_path / "projects"
    path = root / "-home-dev-projects-myrepo" / "sess1.jsonl"
    _write_jsonl(path, _claude_transcript())
    # A subagent sidecar one level deeper must NOT be indexed as its own session.
    _write_jsonl(
        path.parent / "sess1" / "subagents" / "sub.jsonl", _claude_transcript()
    )

    cfg = config.SourceConfig(key="claude_code", roots=[root])
    with db.connect() as conn:
        cur = conn.cursor()
        counts = ClaudeCodeSource().ingest(cur, {}, cfg, rebuild=False)
        conn.commit()

    assert counts["added"] == 1  # only the top-level transcript, not the subagent
    s = search.get_session("claude-code-sess1")
    assert s is not None
    assert s["repository"] == "myrepo"
    # get_session returns the flattened sessions row, so metrics are top-level.
    assert s["model"] == "claude-opus-4.8"
    assert s["input_tokens"] == 3700
    res = search.search("refresh token", mode="keyword")
    assert any(r["id"] == "claude-code-sess1" for r in res)


def test_claude_code_reingest_skips_unchanged(tmp_path):
    """A second scan of an unchanged transcript is skipped from its stat alone."""
    from mark import config, db
    from mark.sources.claude_code import ClaudeCodeSource

    root = tmp_path / "projects"
    path = root / "-home-dev-projects-myrepo" / "sess1.jsonl"
    _write_jsonl(path, _claude_transcript())

    cfg = config.SourceConfig(key="claude_code", roots=[root])
    src = ClaudeCodeSource()
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
        cached = cur.execute(
            "SELECT signature FROM source_file_stat WHERE path = ?",
            (f"cc:{path}",),
        ).fetchone()
    assert first["added"] == 1
    assert second == {"added": 0, "updated": 0, "skipped": 1}
    assert cached is not None
