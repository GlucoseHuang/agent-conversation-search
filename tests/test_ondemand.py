import json
from pathlib import Path

from agent_conversation_search.config import load_config
from agent_conversation_search.db import connect, init_db
from agent_conversation_search.ondemand import ensure_session


def test_ensure_session_indexes_missing_kimi_session(tmp_path):
    session_dir = tmp_path / "kimi" / "wd_demo" / "session_session_test"
    main = session_dir / "agents" / "main"
    main.mkdir(parents=True)
    (session_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "session_test",
                "cwd": "/tmp/demo",
                "title": "on demand",
                "createdAt": 1_790_000_000_000,
                "updatedAt": 1_790_000_001_000,
            }
        )
    )
    (main / "wire.jsonl").write_text(
        json.dumps(
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "hello on demand"}],
                "time": 1_790_000_002_000,
            }
        )
        + "\n"
    )

    config = load_config()
    config["db_path"] = tmp_path / "index.db"
    for key in config["sources"]:
        if key != "kimi_sessions":
            config["sources"][key] = tmp_path / "empty"
    config["sources"]["claude_desktop_glob"] = str(tmp_path / "empty" / "*")
    config["sources"]["kimi_sessions"] = tmp_path / "kimi"

    conn = connect(config["db_path"])
    init_db(conn)
    result = ensure_session(conn, config, "session_test")
    assert result["id"] == "kimi:session_test"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1