from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from ..models import Event, Goal, Session, ToolCall, stable_id
from .base import (
    Source,
    content_text,
    file_ref,
    first_user_text,
    iso,
    iter_jsonl,
    title_from,
    tool_text,
)

AGENT = "codex"


def discover(config: dict[str, Any]) -> list[Source]:
    root = config["sources"]["codex_dir"]
    sources: list[Source] = []
    for path in (root / "sessions").rglob("*.jsonl"):
        sources.append(Source(agent=AGENT, path=path, metadata={"root": str(root)}))
    if config["index"]["include_archived"]:
        for path in (root / "archived_sessions").glob("*.jsonl.zst"):
            sources.append(
                Source(
                    agent=AGENT,
                    path=path,
                    compressed="zstd",
                    metadata={"root": str(root)},
                )
            )
    return sources


def _sqlite_rows(path: Path, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(query, params)]
        conn.close()
        return rows
    except sqlite3.Error:
        return []


def _metadata(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    threads = {
        row["id"]: row
        for row in _sqlite_rows(
            root / "state_5.sqlite",
            "SELECT id, title, name, preview, first_user_message, cwd, created_at_ms, "
            "updated_at_ms, model, archived FROM threads",
        )
    }
    goals: dict[str, list[dict[str, Any]]] = {}
    for row in _sqlite_rows(
        root / "goals_1.sqlite",
        "SELECT thread_id, goal_id, objective, status, token_budget, tokens_used, "
        "time_used_seconds, created_at_ms, updated_at_ms FROM thread_goals",
    ):
        goals.setdefault(row["thread_id"], []).append(row)
    return threads, goals


def _session_id(obj: dict[str, Any]) -> Optional[str]:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    return payload.get("id") or payload.get("session_id")


def load(source: Source) -> list[Session]:
    root = Path(source.metadata.get("root", source.path.parent))
    threads, goals = _metadata(root)
    events: list[Event] = []
    calls: list[ToolCall] = []
    session_id: Optional[str] = None
    parent_id: Optional[str] = None
    cwd: Optional[str] = None
    title: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model: Optional[str] = None
    seq = 0

    for line_no, raw, obj in iter_jsonl(source.path, source.compressed):
        if not isinstance(obj, dict):
            continue
        timestamp = obj.get("timestamp")
        if timestamp and not created_at:
            created_at = timestamp
        if timestamp:
            updated_at = timestamp
        kind = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if kind == "session_meta":
            session_id = session_id or payload.get("id") or payload.get("session_id")
            parent_id = payload.get("parent_thread_id") or parent_id
            cwd = cwd or payload.get("cwd")
        elif kind == "turn_context":
            cwd = cwd or payload.get("cwd")
            model = payload.get("model") or model
        elif kind == "response_item":
            item_type = payload.get("type")
            if item_type == "message":
                role = payload.get("role") or "assistant"
                text = content_text(payload.get("content"))
                if text:
                    events.append(
                        Event(seq, timestamp, role, "message", text, file_ref(source.path, line_no, raw, source.compressed))
                    )
                    seq += 1
            elif item_type == "reasoning":
                summary = payload.get("summary")
                text = content_text(summary) or content_text(payload.get("content"))
                if text:
                    events.append(
                        Event(seq, timestamp, "assistant", "reasoning", text, file_ref(source.path, line_no, raw, source.compressed))
                    )
                    seq += 1
            elif item_type in {"function_call", "custom_tool_call", "local_shell_call"}:
                name = payload.get("name") or item_type
                arguments = payload.get("arguments") or payload.get("input") or ""
                call_id = payload.get("call_id") or payload.get("id")
                event = Event(
                    seq,
                    timestamp,
                    "assistant",
                    "tool_call",
                    tool_text(name, arguments),
                    file_ref(source.path, line_no, raw, source.compressed),
                    tool_call_id=call_id,
                )
                events.append(event)
                calls.append(
                    ToolCall(
                        id=stable_id(source.path, line_no, call_id, "call"),
                        name=name,
                        call_id=call_id,
                        status=payload.get("status"),
                        input_text=tool_text(name, arguments),
                        event_id=event.event_id,
                    )
                )
                seq += 1
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                call_id = payload.get("call_id")
                text = content_text(payload.get("output"))
                event = Event(
                    seq,
                    timestamp,
                    "tool",
                    "tool_result",
                    text,
                    file_ref(source.path, line_no, raw, source.compressed),
                    tool_call_id=call_id,
                )
                events.append(event)
                calls.append(
                    ToolCall(
                        id=stable_id(source.path, line_no, call_id, "result"),
                        name="tool_result",
                        call_id=call_id,
                        status="completed",
                        output_text=text,
                        event_id=event.event_id,
                    )
                )
                seq += 1
            elif item_type == "web_search_call":
                action = payload.get("action") or {}
                query = action.get("query") if isinstance(action, dict) else ""
                if query:
                    events.append(
                        Event(
                            seq,
                            timestamp,
                            "assistant",
                            "tool_call",
                            f"web_search {query}",
                            file_ref(source.path, line_no, raw, source.compressed),
                        )
                    )
                    seq += 1
        elif kind == "event_msg":
            event_type = payload.get("type")
            if event_type == "agent_message" and payload.get("message"):
                text = content_text(payload["message"])
                if text:
                    events.append(
                        Event(seq, timestamp, "assistant", "message", text, file_ref(source.path, line_no, raw, source.compressed))
                    )
                    seq += 1

    if not session_id:
        session_id = source.path.stem
    meta = threads.get(session_id, {})
    if meta:
        title = meta.get("name") or meta.get("title") or meta.get("preview") or meta.get("first_user_message")
        cwd = cwd or meta.get("cwd")
        created_at = created_at or iso(meta.get("created_at_ms"))
        updated_at = updated_at or iso(meta.get("updated_at_ms"))
        model = model or meta.get("model")
    if not title:
        title = title_from(first_user_text(events), session_id)
    if not events:
        return []

    goal_objects = [
        Goal(
            id=row["goal_id"],
            objective=row["objective"],
            status=row["status"],
            budget={
                "token_budget": row.get("token_budget"),
                "tokens_used": row.get("tokens_used"),
                "time_used_seconds": row.get("time_used_seconds"),
            },
            created_at=iso(row.get("created_at_ms")),
            updated_at=iso(row.get("updated_at_ms")),
        )
        for row in goals.get(session_id, [])
    ]

    session = Session(
        agent=AGENT,
        native_id=session_id,
        title=title,
        project=cwd,
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        parent_id=f"{AGENT}:{parent_id}" if parent_id else None,
        raw_path=str(source.path),
        metadata={"thread": meta, "model": model, "archived": bool(meta.get("archived"))},
        events=events,
        tool_calls=calls,
        goals=goal_objects,
        source_path=str(source.path),
    )
    session.finalize()
    return [session]