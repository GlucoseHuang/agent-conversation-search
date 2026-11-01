import pytest

from agent_conversation_search.db import connect, init_db, upsert_session
from agent_conversation_search.models import Session
from agent_conversation_search.resolve import SessionResolutionError, resolve_session


def _add(conn, agent: str, native_id: str, title: str = "demo") -> Session:
    session = Session(agent=agent, native_id=native_id, title=title, raw_path="/tmp/demo")
    session.finalize()
    upsert_session(conn, session)
    return session


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "index.db")
    init_db(conn)
    uuid = "11111111-2222-3333-4444-555555555555"
    _add(conn, "codex", uuid, "codex session")
    _add(conn, "claude-desktop-code", uuid, "claude session")
    _add(conn, "kimi", "session_aaaa-bbbb", "kimi session")
    _add(conn, "dsh", "session-dddd-eeee", "dsh session")
    _add(conn, "opencode", "ses_ffffGGGG", "opencode session")
    _add(conn, "workbuddy", "0123456789abcdef0123456789abcdef", "workbuddy session")
    _add(conn, "hana", "fedcba9876543210fedcba9876543210", "hana session")
    return conn


def test_full_and_bare_ids(db):
    assert resolve_session(db, "codex:11111111-2222-3333-4444-555555555555")["agent"] == "codex"
    assert resolve_session(db, "session_aaaa-bbbb")["id"] == "kimi:session_aaaa-bbbb"
    assert resolve_session(db, "ses_ffffGGGG")["id"] == "opencode:ses_ffffGGGG"


def test_deep_links(db):
    uuid = "11111111-2222-3333-4444-555555555555"
    assert resolve_session(db, f"codex://threads/{uuid}")["agent"] == "codex"
    assert resolve_session(db, f"codex://shared-thread/{uuid}")["agent"] == "codex"
    assert resolve_session(db, f"claude://chat/{uuid}")["agent"] == "claude-desktop-code"
    assert resolve_session(db, "kimi-code://session/session_aaaa-bbbb")["agent"] == "kimi"
    assert resolve_session(db, "dsh://session/session-dddd-eeee")["agent"] == "dsh"
    assert resolve_session(db, "opencode://session/ses_ffffGGGG")["agent"] == "opencode"
    assert resolve_session(
        db, "workbuddy://0123456789abcdef0123456789abcdef"
    )["agent"] == "workbuddy"


def test_ambiguous_bare_id(db):
    with pytest.raises(SessionResolutionError) as error:
        resolve_session(db, "11111111-2222-3333-4444-555555555555")
    assert len(error.value.candidates) == 2