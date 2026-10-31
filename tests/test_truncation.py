from agent_conversation_search.db import connect, get_session, init_db, replace_session
from agent_conversation_search.models import Event, RawRef, Session


def test_tool_output_and_message_truncation(tmp_path):
    session = Session(
        agent="test",
        native_id="long",
        raw_path="/tmp/long.jsonl",
        events=[
            Event(
                0,
                "2026-10-31T10:00:00Z",
                "tool",
                "tool_result",
                "x" * 30000,
                RawRef(kind="file", path="/tmp/long.jsonl", line=1),
            ),
            Event(
                1,
                "2026-10-31T10:00:01Z",
                "user",
                "message",
                "y" * 30000,
                RawRef(kind="file", path="/tmp/long.jsonl", line=2),
            ),
        ],
    )
    session.finalize()
    conn = connect(tmp_path / "index.db")
    init_db(conn)
    replace_session(conn, session, max_event_chars=20000)
    value = get_session(conn, "test:long")
    assert value is not None
    tool_event, message_event = value["events"]
    assert len(tool_event["text"]) == 8000
    assert len(message_event["text"]) == 20000
    assert tool_event["metadata"]["truncated"] is True
    assert message_event["metadata"]["truncated"] is True