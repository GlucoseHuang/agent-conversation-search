from agent_conversation_search.compact import compact
from agent_conversation_search.db import connect, get_event, get_session, init_db, replace_session
from agent_conversation_search.handoff import build_handoff
from agent_conversation_search.models import Event, RawRef, Session, ToolCall
from agent_conversation_search.search import search


def _session(text: str = "安排每日简报", secret: str = "") -> Session:
    session = Session(
        agent="test",
        native_id="s1",
        title="每日简报设置",
        cwd="/tmp/demo",
        raw_path="/tmp/demo.jsonl",
        events=[
            Event(
                0,
                "2026-10-31T10:00:00Z",
                "user",
                "message",
                text + secret,
                RawRef(kind="file", path="/tmp/demo.jsonl", line=1, hash="h1"),
            ),
            Event(
                1,
                "2026-10-31T10:00:01Z",
                "assistant",
                "message",
                "我会先确认来源和时区。",
                RawRef(kind="file", path="/tmp/demo.jsonl", line=2, hash="h2"),
            ),
        ],
    )
    session.finalize()
    return session


def test_search_get_and_handoff_keep_raw_pointer(tmp_path):
    conn = connect(tmp_path / "index.db")
    init_db(conn)
    replace_session(conn, _session())

    results = search(conn, "每日简报", client=None, limit=5)
    assert len(results) == 1
    assert results[0]["session_id"] == "test:s1"
    assert results[0]["match"]["raw"]["line"] == 1

    value = get_session(conn, "test:s1")
    assert value is not None
    assert len(value["events"]) == 2
    assert value["events"][0]["raw"]["path"] == "/tmp/demo.jsonl"

    package = build_handoff(value, recent=5)
    assert package["source"]["session_id"] == "test:s1"
    assert package["raw_refs"]
    assert package["recent_messages"][0]["text"].startswith("安排每日简报")


def test_reindex_replaces_fts_rows(tmp_path):
    conn = connect(tmp_path / "index.db")
    init_db(conn)
    replace_session(conn, _session("旧内容"))
    replace_session(conn, _session("新内容"))
    assert search(conn, "旧内容", client=None, limit=5) == []
    assert search(conn, "新内容", client=None, limit=5)[0]["session_id"] == "test:s1"


def test_tool_calls_are_enriched_from_events_and_fts_skips_results(tmp_path):
    session = Session(
        agent="test",
        native_id="tools",
        raw_path="/tmp/tools.jsonl",
        events=[
            Event(
                0,
                "2026-10-31T10:00:00Z",
                "user",
                "message",
                "alpha unique",
                RawRef(kind="file", path="/tmp/tools.jsonl", line=1),
            ),
            Event(
                1,
                "2026-10-31T10:00:01Z",
                "assistant",
                "tool_call",
                "Bash ls",
                RawRef(kind="file", path="/tmp/tools.jsonl", line=2),
                tool_call_id="c1",
            ),
            Event(
                2,
                "2026-10-31T10:00:02Z",
                "tool",
                "tool_result",
                "beta unique",
                RawRef(kind="file", path="/tmp/tools.jsonl", line=3),
                tool_call_id="c1",
            ),
        ],
        tool_calls=[
            ToolCall(id="tc1", name="Bash", call_id="c1", input_text="Bash ls", output_text="beta unique")
        ],
    )
    session.finalize()
    conn = connect(tmp_path / "index.db")
    init_db(conn)
    replace_session(conn, session)

    assert search(conn, "alpha", client=None, limit=5)
    assert search(conn, "beta", client=None, limit=5) == []

    value = get_session(conn, "test:tools")
    assert value is not None
    call = value["tool_calls"][0]
    assert call["input_text"] == "Bash ls"
    assert call["output_text"] == "beta unique"
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
    assert "input_text" not in columns
    assert "output_text" not in columns


def test_compact_rebuilds_external_fts(tmp_path):
    conn = connect(tmp_path / "index.db")
    init_db(conn)
    replace_session(conn, _session("compact me"))
    result = compact(conn, vacuum=False)
    assert result["after_bytes"] > 0
    assert search(conn, "compact", client=None, limit=5)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events_fts'"
    ).fetchone()[0]
    assert "content='events'" in sql


def test_redaction_applies_before_storage(tmp_path):
    conn = connect(tmp_path / "index.db")
    init_db(conn)
    replace_session(conn, _session(secret=" key sk-ws-abc1234567890"))
    value = get_session(conn, "test:s1")
    assert value is not None
    event = get_event(conn, value["events"][0]["id"])
    assert event is not None
    assert "sk-ws-abc1234567890" not in event["text"]
    assert "[REDACTED:" in event["text"]