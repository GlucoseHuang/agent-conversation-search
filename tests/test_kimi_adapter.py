import json

from agent_conversation_search.adapters.base import Source
from agent_conversation_search.adapters.kimi import load


def test_kimi_session_directory_and_subagent_parent(tmp_path):
    session_dir = tmp_path / "sessions" / "wd_demo" / "session_x"
    main_dir = session_dir / "agents" / "main"
    sub_dir = session_dir / "agents" / "agent-1"
    main_dir.mkdir(parents=True)
    sub_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "session_x",
                "cwd": "/tmp/demo",
                "title": "Demo",
                "createdAt": 1_790_000_000_000,
                "updatedAt": 1_790_000_001_000,
            }
        )
    )
    main_wire = main_dir / "wire.jsonl"
    main_wire.write_text(
        "\n".join(
            [
                json.dumps({"type": "metadata"}),
                json.dumps(
                    {
                        "type": "turn.prompt",
                        "input": [{"type": "text", "text": "hello"}],
                        "time": 1_790_000_002_000,
                    }
                ),
                json.dumps(
                    {
                        "type": "task.started",
                        "info": {
                            "taskId": "t1",
                            "agentId": "agent-1",
                            "description": "sub task",
                            "status": "running",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    (sub_dir / "wire.jsonl").write_text(
        json.dumps(
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "sub hello"}],
                "time": 1_790_000_003_000,
            }
        )
        + "\n"
    )

    main = load(Source(agent="kimi", path=main_wire))[0]
    assert main.id == "kimi:session_x"
    assert main.parent_id is None
    assert main.title == "Demo"

    sub = load(Source(agent="kimi", path=sub_dir / "wire.jsonl"))[0]
    assert sub.id == "kimi:session_x:agent-1"
    assert sub.parent_id == "kimi:session_x"
    assert sub.title == "sub task"