import json
from pathlib import Path

from agent_conversation_search.adapters.base import Source
from agent_conversation_search.adapters.workbuddy import load


def test_workbuddy_loads_session_and_task_file(tmp_path):
    workbuddy = tmp_path / "workbuddy"
    project = workbuddy / "projects" / "demo"
    project.mkdir(parents=True)
    path = project / "s1.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "custom-title",
                        "customTitle": "Demo",
                        "sessionId": "s1",
                        "timestamp": 1_790_000_000_000,
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "role": "user",
                        "sessionId": "s1",
                        "timestamp": 1_790_000_001_000,
                        "content": [{"type": "input_text", "text": "做每日简报"}],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n"
    )
    task_dir = workbuddy / "tasks" / "s1"
    task_dir.mkdir(parents=True)
    (task_dir / "1.json").write_text(
        json.dumps(
            {
                "id": "1",
                "subject": "确认来源",
                "description": "问用户要哪些来源",
                "status": "pending",
            },
            ensure_ascii=False,
        )
    )

    sessions = load(
        Source(agent="workbuddy", path=path, metadata={"workbuddy_dir": str(workbuddy)})
    )
    assert len(sessions) == 1
    assert sessions[0].title == "Demo"
    assert sessions[0].todos[0].content == "确认来源"
    assert sessions[0].todos[0].status == "pending"