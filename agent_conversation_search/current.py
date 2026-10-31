from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CurrentSession:
    session_id: str
    agent: Optional[str] = None
    confidence: str = "heuristic"
    source: str = ""
    detail: str = ""


def ancestor_pids(start: Optional[int] = None, limit: int = 12) -> list[int]:
    pid = start or os.getpid()
    result: list[int] = []
    for _ in range(limit):
        if pid <= 1:
            break
        result.append(pid)
        try:
            output = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            pid = int(output.stdout.strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return result


def _session_id(value: str) -> tuple[str, Optional[str]]:
    value = value.strip()
    if ":" in value:
        agent, native = value.split(":", 1)
        if agent in {
            "claude-code",
            "claude-desktop-code",
            "codex",
            "kimi",
            "opencode",
            "dsh",
            "hana",
            "workbuddy",
        }:
            return value, agent
    return value, None


def claude_from_pids(
    pids: Optional[list[int]] = None,
    home: Optional[Path] = None,
) -> Optional[CurrentSession]:
    home = home or Path.home()
    for pid in pids or ancestor_pids():
        path = home / ".claude" / "sessions" / f"{pid}.json"
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        session = value.get("sessionId")
        if not session:
            continue
        entrypoint = str(value.get("entrypoint") or "")
        agent = "claude-desktop-code" if "desktop" in entrypoint else "claude-code"
        return CurrentSession(
            session_id=f"{agent}:{session}",
            agent=agent,
            confidence="exact",
            source="claude-pid-session",
            detail=f"pid={pid}, entrypoint={entrypoint}, cwd={value.get('cwd')}",
        )
    return None


def kimi_recent(
    home: Optional[Path] = None,
    cwd: Optional[str] = None,
    max_age_seconds: int = 86400,
) -> Optional[CurrentSession]:
    home = home or Path.home()
    cwd_path = Path(cwd or os.getcwd()).resolve()
    now_ms = int(time.time() * 1000)
    best: Optional[tuple[int, dict, str]] = None
    for path in glob.glob(str(home / ".kimi-code" / "sessions" / "*" / "session_*" / "state.json")):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        session_cwd = value.get("cwd")
        if not session_cwd:
            continue
        try:
            session_path = Path(session_cwd).resolve()
            if not (
                session_path == cwd_path
                or session_path in cwd_path.parents
                or cwd_path in session_path.parents
            ):
                continue
        except OSError:
            continue
        try:
            updated = int(value.get("updatedAt") or 0)
        except (TypeError, ValueError):
            continue
        if updated and now_ms - updated > max_age_seconds * 1000:
            continue
        if not value.get("id"):
            continue
        if best is None or updated > best[0]:
            best = (updated, value, path)
    if not best:
        return None
    _, value, path = best
    return CurrentSession(
        session_id=f"kimi:{value['id']}",
        agent="kimi",
        confidence="heuristic",
        source="kimi-recent-cwd",
        detail=f"updatedAt={value.get('updatedAt')}, cwd={value.get('cwd')}, state={path}",
    )


def detect_current_session(
    explicit: Optional[str] = None,
    agent_filter: Optional[str] = None,
    cwd: Optional[str] = None,
    home: Optional[Path] = None,
) -> Optional[CurrentSession]:
    raw = explicit or os.environ.get("ACS_CURRENT_SESSION_ID")
    if raw:
        session_id, agent = _session_id(raw)
        if agent_filter and agent and agent != agent_filter:
            return None
        if agent_filter and not agent:
            session_id = f"{agent_filter}:{session_id}"
            agent = agent_filter
        return CurrentSession(
            session_id=session_id,
            agent=agent,
            confidence="exact",
            source="explicit" if explicit else "ACS_CURRENT_SESSION_ID",
        )

    claude = claude_from_pids(home=home)
    if claude and (not agent_filter or claude.agent == agent_filter):
        return claude

    if not agent_filter or agent_filter == "kimi":
        kimi = kimi_recent(home=home, cwd=cwd)
        if kimi:
            return kimi
    return None