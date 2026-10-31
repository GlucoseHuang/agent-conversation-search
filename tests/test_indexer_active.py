import json

from agent_conversation_search.adapters.base import Source
from agent_conversation_search.config import load_config
from agent_conversation_search.db import connect, init_db
from agent_conversation_search.indexer import index_source


def test_active_source_is_skipped_until_grace_expires(tmp_path):
    session = tmp_path / "projects" / "demo" / "s1.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "s1",
                "timestamp": "2026-10-31T10:00:00Z",
                "message": {"role": "user", "content": "active"},
            }
        )
        + "\n"
    )
    config = load_config()
    config["db_path"] = tmp_path / "index.db"
    conn = connect(config["db_path"])
    init_db(conn)
    source = Source(agent="claude-code", path=session)

    skipped = index_source(conn, source, config, active_grace_seconds=3600)
    assert skipped["status"] == "active"

    indexed = index_source(conn, source, config, active_grace_seconds=0)
    assert indexed["status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1