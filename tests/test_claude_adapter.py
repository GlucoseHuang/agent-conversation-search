import json

from agent_conversation_search.adapters.base import Source
from agent_conversation_search.adapters.claude_code import load


def test_claude_code_parses_messages_tools_and_raw_lines(tmp_path):
    session_dir = tmp_path / "projects" / "demo"
    session_dir.mkdir(parents=True)
    path = session_dir / "s1.jsonl"
    rows = [
        {"type": "custom-title", "customTitle": "Demo Session", "sessionId": "s1"},
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-10-31T10:00:00Z",
            "cwd": "/tmp/demo",
            "message": {"role": "user", "content": "找一下每日简报"},
        },
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-10-31T10:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "我来找"},
                    {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "sessionId": "s1",
            "timestamp": "2026-10-31T10:00:02Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")

    sessions = load(Source(agent="claude-code", path=path))
    assert len(sessions) == 1
    session = sessions[0]
    assert session.title == "Demo Session"
    assert session.cwd == "/tmp/demo"
    assert [event.kind for event in session.events] == [
        "message",
        "message",
        "tool_call",
        "tool_result",
    ]
    assert session.events[0].raw.line == 2
    assert session.events[2].tool_call_id == "call_1"
    assert any(call.name == "Bash" for call in session.tool_calls)