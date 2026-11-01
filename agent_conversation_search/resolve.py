from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ID = re.compile(r"(?:session[_-][0-9A-Za-z-]+|ses_[0-9A-Za-z]+|[0-9a-fA-F]{32})")
_SCHEME_AGENTS = {
    "codex": {"codex"},
    "chatgpt": {"codex"},
    "claude": {"claude-code", "claude-desktop-code"},
    "kimi": {"kimi"},
    "kimi-code": {"kimi"},
    "opencode": {"opencode"},
    "dsh": {"dsh"},
    "hana": {"hana"},
    "workbuddy": {"workbuddy"},
}


class SessionResolutionError(LookupError):
    def __init__(self, message: str, candidates: Optional[list[dict[str, Any]]] = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


def _row(conn: sqlite3.Connection, session_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT id, agent, native_id, title, raw_locator FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    value = dict(row)
    try:
        value["raw"] = json.loads(value.pop("raw_locator") or "{}")
    except ValueError:
        value["raw"] = {}
    return value


def _candidate_rows(conn: sqlite3.Connection, value: str) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT id, agent, native_id, title, raw_locator FROM sessions "
            "WHERE id = ? OR native_id = ?",
            (value, value),
        )
    ]
    for row in rows:
        try:
            row["raw"] = json.loads(row.pop("raw_locator") or "{}")
        except ValueError:
            row["raw"] = {}
    return rows


def _tokens(value: str) -> list[str]:
    raw = unquote(value.strip())
    tokens: list[str] = [raw]
    parsed = urlparse(raw)
    if parsed.scheme:
        tokens.extend([parsed.netloc, *[part for part in parsed.path.split("/") if part]])
        for values in parse_qs(parsed.query).values():
            tokens.extend(values)
    tokens.extend(_UUID.findall(raw))
    tokens.extend(_ID.findall(raw))
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        token = unquote(token).strip().strip(".,;:()[]{}<>\"'")
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def resolve_session(conn: sqlite3.Connection, value: str) -> dict[str, Any]:
    value = unquote(value.strip())
    if not value:
        raise SessionResolutionError("empty session id")

    direct = _candidate_rows(conn, value)
    if len(direct) == 1:
        return direct[0]

    candidates: dict[str, dict[str, Any]] = {row["id"]: row for row in direct}
    for token in _tokens(value):
        for row in _candidate_rows(conn, token):
            candidates[row["id"]] = row

    scheme = urlparse(value).scheme.lower()
    allowed = _SCHEME_AGENTS.get(scheme)
    if allowed:
        candidates = {
            session_id: row
            for session_id, row in candidates.items()
            if row["agent"] in allowed
        }

    if len(candidates) == 1:
        return next(iter(candidates.values()))
    if not candidates:
        raise SessionResolutionError(f"session not found: {value}")
    raise SessionResolutionError(
        f"ambiguous session id: {value}",
        sorted(candidates.values(), key=lambda row: row["id"]),
    )