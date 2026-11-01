from __future__ import annotations

import glob
import sqlite3
from pathlib import Path
from typing import Any

from .adapters import claude_code, codex, dsh, hana, kimi, opencode, workbuddy
from .adapters.base import Source
from .db import replace_session
from .resolve import SessionResolutionError, resolve_session, session_tokens

_MODULES = {
    "claude-code": claude_code,
    "claude-desktop-code": claude_code,
    "codex": codex,
    "kimi": kimi,
    "opencode": opencode,
    "dsh": dsh,
    "hana": hana,
    "workbuddy": workbuddy,
}


def _paths(pattern: str) -> list[Path]:
    return [Path(path) for path in glob.glob(pattern, recursive=True)]


def find_raw_sources(value: str, config: dict[str, Any]) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    roots = config["sources"]
    tokens = [
        token
        for token in session_tokens(value)
        if len(token) >= 8 and token not in {"claude", "threads", "session", "chat"}
    ]

    def add(agent: str, path: Path) -> None:
        key = (agent, str(path))
        if path.exists() and key not in seen:
            seen.add(key)
            sources.append((agent, path))

    for token in tokens:
        for path in _paths(str(roots["claude_projects"] / "**" / f"*{token}*.jsonl")):
            add("claude-code", path)
        for path in _paths(str(Path(str(roots["claude_desktop_glob"])) / "**" / f"*{token}*.jsonl")):
            add("claude-desktop-code", path)
        for path in _paths(str(roots["codex_dir"] / "sessions" / "**" / f"*{token}*.jsonl")):
            add("codex", path)
        for path in _paths(str(roots["codex_dir"] / "archived_sessions" / f"*{token}*.zst")):
            add("codex", path)
        for session_name in {f"session_{token}", token}:
            for path in _paths(
                str(roots["kimi_sessions"] / "*" / session_name / "agents" / "*" / "wire.jsonl")
            ):
                add("kimi", path)
        for session_name in {f"session-{token}", token}:
            for path in _paths(
                str(
                    roots["dsh_sessions"]
                    / "**"
                    / session_name
                    / "session.v4.jsonl.zstd"
                )
            ):
                add("dsh", path)
        for path in _paths(str(roots["hana_agents"] / "*" / "sessions" / "**" / f"*{token}*.jsonl")):
            add("hana", path)
        for path in _paths(
            str(roots["hana_agents"] / "*" / "subagent-sessions" / "**" / f"*{token}*.jsonl")
        ):
            add("hana", path)
        for path in _paths(str(roots["workbuddy_dir"] / "projects" / "**" / f"{token}.jsonl")):
            add("workbuddy", path)
        for path in _paths(
            str(roots["workbuddy_dir"] / "projects" / "**" / f"*{token}*.jsonl")
        ):
            add("workbuddy", path)
        if token.startswith("ses_"):
            add("opencode", roots["opencode_db"])
    return sources


def ensure_session(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    value: str,
) -> dict[str, Any]:
    try:
        return resolve_session(conn, value)
    except SessionResolutionError as error:
        original = error

    tokens = set(session_tokens(value))
    for agent, path in find_raw_sources(value, config):
        module = _MODULES.get(agent)
        if not module:
            continue
        compressed = "zstd" if path.suffix in {".zst", ".zstd"} else None
        source = Source(agent=agent, path=path, compressed=compressed)
        try:
            sessions = module.load(source)
        except Exception:
            continue
        for session in sessions:
            if (
                session.native_id in tokens
                or session.id in tokens
                or session.native_id == value
                or session.id == value
            ):
                replace_session(
                    conn,
                    session,
                    config["index"]["max_event_chars"],
                    config["index"]["event_caps"],
                )
                conn.commit()
                return resolve_session(conn, session.id)
    raise original