import json
import time

from agent_conversation_search.current import (
    claude_from_pids,
    detect_current_session,
    kimi_recent,
)


def test_explicit_current_session_wins():
    current = detect_current_session(explicit="kimi:session_123")
    assert current is not None
    assert current.session_id == "kimi:session_123"
    assert current.confidence == "exact"


def test_claude_pid_mapping(tmp_path):
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "123.json").write_text(
        json.dumps(
            {
                "pid": 123,
                "sessionId": "abc",
                "entrypoint": "claude-desktop-3p",
                "cwd": "/tmp/demo",
            }
        )
    )
    current = claude_from_pids([123], home=tmp_path)
    assert current is not None
    assert current.session_id == "claude-desktop-code:abc"
    assert current.confidence == "exact"


def test_kimi_recent_same_cwd(tmp_path):
    state_dir = tmp_path / ".kimi-code" / "sessions" / "wd" / "session_x"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "session_x",
                "cwd": str(tmp_path),
                "updatedAt": int(time.time() * 1000),
                "title": "current",
            }
        )
    )
    current = kimi_recent(home=tmp_path, cwd=str(tmp_path))
    assert current is not None
    assert current.session_id == "kimi:session_x"