from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..models import Event, Session, Todo, ToolCall, stable_id
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

AGENT = "workbuddy"


def discover(config: dict[str, Any]) -> list[Source]:
    root = config["sources"]["workbuddy_dir"] / "projects"
    if not root.exists():
        return []
    return [
        Source(
            agent=AGENT,
            path=path,
            metadata={"workbuddy_dir": str(config["sources"]["workbuddy_dir"])},
        )
        for path in root.rglob("*.jsonl")
    ]


def _tasks(workbuddy_dir: Path, session_id: str) -> list[Todo]:
    task_dir = workbuddy_dir / "tasks" / session_id
    if not task_dir.exists():
        return []
    todos: list[Todo] = []
    for path in sorted(task_dir.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        todos.append(
            Todo(
                id=str(value.get("id") or path.stem),
                content=value.get("subject") or value.get("description") or "",
                status=value.get("status"),
                priority=value.get("priority"),
                position=int(value.get("id")) if str(value.get("id", "")).isdigit() else None,
            )
        )
    return todos


def load(source: Source) -> list[Session]:
    events: list[Event] = []
    calls: list[ToolCall] = []
    session_id: Optional[str] = None
    title: Optional[str] = None
    cwd: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    model: Optional[str] = None
    seq = 0

    for line_no, raw, obj in iter_jsonl(source.path):
        if not isinstance(obj, dict):
            continue
        line_type = obj.get("type")
        timestamp = iso(obj.get("timestamp")) or updated_at
        if timestamp:
            created_at = created_at or timestamp
            updated_at = timestamp
        if obj.get("sessionId"):
            session_id = obj["sessionId"]
        if line_type == "custom-title":
            title = obj.get("customTitle") or title
        elif line_type == "ai-title":
            title = obj.get("title") or obj.get("aiTitle") or title
        elif line_type == "session-meta":
            session_id = obj.get("sessionId") or session_id
            meta = obj.get("meta") or {}
            model = meta.get("model") or model
        elif line_type == "message":
            role = obj.get("role") or "assistant"
            text = content_text(obj.get("content"))
            if text:
                events.append(
                    Event(seq, timestamp, role, "message", text, file_ref(source.path, line_no, raw))
                )
                seq += 1
        elif line_type == "reasoning":
            text = content_text(obj.get("content")) or content_text(obj.get("rawContent"))
            if text:
                events.append(
                    Event(seq, timestamp, "assistant", "reasoning", text, file_ref(source.path, line_no, raw))
                )
                seq += 1
        elif line_type == "function_call":
            name = obj.get("name") or "tool"
            arguments = obj.get("arguments") or obj.get("input") or {}
            call_id = obj.get("callId") or obj.get("call_id") or obj.get("id")
            text = tool_text(name, arguments)
            events.append(
                Event(
                    seq,
                    timestamp,
                    "assistant",
                    "tool_call",
                    text,
                    file_ref(source.path, line_no, raw),
                    tool_call_id=call_id,
                )
            )
            calls.append(
                ToolCall(
                    id=stable_id(source.path, line_no, call_id, "call"),
                    name=name,
                    call_id=call_id,
                    input_text=text,
                )
            )
            seq += 1
        elif line_type == "function_call_result":
            call_id = obj.get("callId") or obj.get("call_id")
            text = content_text(obj.get("output") or obj.get("content") or obj.get("result"))
            events.append(
                Event(
                    seq,
                    timestamp,
                    "tool",
                    "tool_result",
                    text,
                    file_ref(source.path, line_no, raw),
                    tool_call_id=call_id,
                )
            )
            calls.append(
                ToolCall(
                    id=stable_id(source.path, line_no, call_id, "result"),
                    name="tool_result",
                    call_id=call_id,
                    status="completed",
                    output_text=text,
                )
            )
            seq += 1
        elif line_type == "file-history-snapshot":
            snapshot = obj.get("snapshot") or {}
            files = snapshot.get("trackedFileBackups") or {}
            event = Event(
                seq,
                timestamp,
                "system",
                "file_change",
                "\n".join(str(item) for item in files),
                file_ref(source.path, line_no, raw),
                metadata={"files": files, "cwd": obj.get("cwd")},
            )
            events.append(event)
            seq += 1
            cwd = cwd or obj.get("cwd")

    if not session_id:
        session_id = source.path.stem
    if not events:
        return []
    if not title:
        title = title_from(first_user_text(events), session_id)
    session = Session(
        agent=AGENT,
        native_id=session_id,
        title=title,
        project=cwd,
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        raw_path=str(source.path),
        metadata={"model": model, "source": str(source.path)},
        events=events,
        tool_calls=calls,
        source_path=str(source.path),
    )
    workbuddy_dir = Path(source.metadata.get("workbuddy_dir", ""))
    if workbuddy_dir:
        session.todos.extend(_tasks(workbuddy_dir, session_id))
    session.finalize()
    return [session]