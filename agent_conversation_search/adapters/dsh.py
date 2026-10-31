from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..models import Event, Goal, Session, Todo, ToolCall, stable_id
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

AGENT = "dsh"


def discover(config: dict[str, Any]) -> list[Source]:
    root = config["sources"]["dsh_sessions"]
    if not root.exists():
        return []
    return [
        Source(agent=AGENT, path=path, compressed="zstd")
        for path in root.rglob("session.v4.jsonl.zstd")
    ]


def _content_blocks(
    content: Any,
    role: str,
    timestamp: Optional[str],
    seq_start: int,
    path: Path,
    line: int,
    raw: bytes,
) -> tuple[list[Event], list[ToolCall]]:
    events: list[Event] = []
    calls: list[ToolCall] = []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        content = [{"type": "text", "text": content_text(content)}]
    seq = seq_start
    for block in content:
        if isinstance(block, str):
            events.append(Event(seq, timestamp, role, "message", block, file_ref(path, line, raw, "zstd")))
            seq += 1
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text") or block.get("input_text") or block.get("output_text") or ""
            if text:
                events.append(Event(seq, timestamp, role, "message", text, file_ref(path, line, raw, "zstd")))
                seq += 1
        elif block_type in {"thinking", "reasoning", "reasoning_text"}:
            text = block.get("text") or block.get("thinking") or ""
            if text:
                events.append(Event(seq, timestamp, role, "reasoning", text, file_ref(path, line, raw, "zstd")))
                seq += 1
        elif block_type in {"tool_call", "toolCall", "tool_use"}:
            name = block.get("name") or "tool"
            arguments = block.get("arguments") or block.get("input") or {}
            call_id = block.get("id") or block.get("callId")
            events.append(
                Event(
                    seq,
                    timestamp,
                    role,
                    "tool_call",
                    tool_text(name, arguments),
                    file_ref(path, line, raw, "zstd"),
                    tool_call_id=call_id,
                )
            )
            calls.append(
                ToolCall(
                    id=stable_id(path, line, call_id, "call"),
                    name=name,
                    call_id=call_id,
                    input_text=tool_text(name, arguments),
                )
            )
            seq += 1
    return events, calls


def _todos(data: dict[str, Any]) -> list[Todo]:
    values = data.get("todos") or data.get("items") or data.get("todo")
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        return []
    todos: list[Todo] = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            continue
        todos.append(
            Todo(
                id=str(item.get("id") or stable_id(index, item.get("content"))),
                content=item.get("content") or item.get("text") or item.get("title") or "",
                status=item.get("status"),
                priority=item.get("priority"),
                position=item.get("position", index),
                blocked_by=list(item.get("blockedBy") or item.get("blocked_by") or []),
            )
        )
    return todos


def load(source: Source) -> list[Session]:
    events: list[Event] = []
    calls: list[ToolCall] = []
    goals: list[Goal] = []
    todos: list[Todo] = []
    session_id: Optional[str] = None
    title: Optional[str] = None
    cwd: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    parent_id: Optional[str] = None
    seq = 0
    seen: set[tuple[str, str, str]] = set()

    def add(event: Event) -> None:
        nonlocal seq
        key = (event.role or "", event.kind, event.text[:2000])
        if key in seen:
            return
        seen.add(key)
        event.seq = seq
        events.append(event)
        seq += 1

    for line_no, raw, obj in iter_jsonl(source.path, "zstd"):
        if not isinstance(obj, dict):
            continue
        event_type = obj.get("type")
        timestamp = iso(obj.get("time")) or updated_at
        if timestamp:
            updated_at = timestamp
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if event_type == "session":
            session_id = obj.get("id") or session_id
            cwd = cwd or obj.get("cwd")
            created_at = iso(obj.get("createdAt")) or created_at
            parent_id = data.get("parentSessionId") or obj.get("parentSessionId") or parent_id
        elif event_type == "session/title":
            title = data.get("title") or data.get("name") or (data if isinstance(data, str) else None)
        elif event_type in {"user/message", "agent/inbox/spliced"}:
            inserted = data.get("inserted") if event_type == "agent/inbox/spliced" else None
            if inserted:
                for item in inserted:
                    if isinstance(item, dict):
                        new_events, new_calls = _content_blocks(
                            item.get("content"), item.get("role") or "user", timestamp, seq, source.path, line_no, raw
                        )
                        for event in new_events:
                            add(event)
                        calls.extend(new_calls)
            else:
                message = data.get("message") or data
                new_events, new_calls = _content_blocks(
                    message.get("content") if isinstance(message, dict) else message,
                    message.get("role") if isinstance(message, dict) else "user",
                    timestamp,
                    seq,
                    source.path,
                    line_no,
                    raw,
                )
                for event in new_events:
                    add(event)
                calls.extend(new_calls)
        elif event_type in {"assistant/message", "system/message", "developer/message"}:
            message = data.get("message") or data
            role = message.get("role") if isinstance(message, dict) else event_type.split("/")[0]
            new_events, new_calls = _content_blocks(
                message.get("content") if isinstance(message, dict) else message,
                role or "assistant",
                timestamp,
                seq,
                source.path,
                line_no,
                raw,
            )
            for event in new_events:
                add(event)
            calls.extend(new_calls)
        elif event_type == "tool/call":
            call_id = data.get("callId") or data.get("call_id") or data.get("id")
            name = data.get("name") or "tool"
            arguments = data.get("arguments") or data.get("input") or {}
            text = tool_text(name, arguments)
            add(
                Event(
                    seq,
                    timestamp,
                    "assistant",
                    "tool_call",
                    text,
                    file_ref(source.path, line_no, raw, "zstd"),
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
        elif event_type == "tool/result":
            call_id = data.get("toolCallId") or data.get("callId") or data.get("tool_call_id")
            message = data.get("message") or data.get("result") or data
            text = content_text(message.get("content") if isinstance(message, dict) else message)
            add(
                Event(
                    seq,
                    timestamp,
                    "tool",
                    "tool_result",
                    text,
                    file_ref(source.path, line_no, raw, "zstd"),
                    tool_call_id=call_id,
                    metadata={"is_error": data.get("isError", False)},
                )
            )
            calls.append(
                ToolCall(
                    id=stable_id(source.path, line_no, call_id, "result"),
                    name="tool_result",
                    call_id=call_id,
                    status="error" if data.get("isError") else "completed",
                    output_text=text,
                )
            )
        elif event_type == "goal/change":
            goal = data.get("goal") or {}
            if isinstance(goal, dict) and goal.get("id"):
                goals.append(
                    Goal(
                        id=goal["id"],
                        objective=goal.get("objective") or "",
                        status=goal.get("phase") or goal.get("status"),
                        phase=goal.get("phase"),
                        budget={"maxGoalRounds": goal.get("maxGoalRounds")},
                        created_at=iso(goal.get("createdAt") or data.get("createdAt")),
                        updated_at=iso(goal.get("updatedAt") or data.get("updatedAt")),
                    )
                )
        elif event_type == "todo/write":
            todos.extend(_todos(data))
        elif event_type in {"subagent/descriptor", "tool-workflow/agent-start", "tool-workflow/agent-end"}:
            text = json.dumps(data, ensure_ascii=False)
            add(
                Event(
                    seq,
                    timestamp,
                    "assistant",
                    "subagent" if "subagent" in event_type else "tool_workflow",
                    text,
                    file_ref(source.path, line_no, raw, "zstd"),
                    metadata={"event_type": event_type},
                )
            )
            if event_type == "tool-workflow/agent-start":
                todos.append(
                    Todo(
                        id=data.get("childId") or stable_id(line_no, data.get("label")),
                        content=data.get("label") or "subagent",
                        status="running",
                        position=len(todos),
                    )
                )
            elif event_type == "tool-workflow/agent-end":
                todos.append(
                    Todo(
                        id=data.get("childId") or stable_id(line_no, data.get("label")),
                        content=data.get("label") or "subagent",
                        status=data.get("outcome") or "completed",
                        position=len(todos),
                    )
                )

    if not session_id:
        session_id = source.path.parent.name
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
        parent_id=f"{AGENT}:{parent_id}" if parent_id else None,
        raw_path=str(source.path),
        metadata={"source": str(source.path)},
        events=events,
        tool_calls=calls,
        goals=goals,
        todos=todos,
        source_path=str(source.path),
    )
    session.finalize()
    return [session]