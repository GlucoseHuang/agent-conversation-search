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

AGENT = "kimi"


def discover(config: dict[str, Any]) -> list[Source]:
    root = config["sources"]["kimi_sessions"]
    sources: list[Source] = []
    if not root.exists():
        return sources
    for session_dir in root.glob("*/session_*"):
        for wire in sorted((session_dir / "agents").glob("*/wire.jsonl")):
            sources.append(Source(agent=AGENT, path=wire))
    return sources


def _state(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def _subagent_title(session_dir: Path, agent_id: str) -> Optional[str]:
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    if not wire.exists():
        return None
    for _, _, obj in iter_jsonl(wire):
        if not isinstance(obj, dict) or obj.get("type") != "task.started":
            continue
        info = obj.get("info") or {}
        if info.get("agentId") == agent_id:
            return info.get("description")
    return None


def _blocks(
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
            events.append(Event(seq, timestamp, role, "message", block, file_ref(path, line, raw)))
            seq += 1
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text") or block.get("input_text") or block.get("output_text") or ""
            if text:
                events.append(Event(seq, timestamp, role, "message", text, file_ref(path, line, raw)))
                seq += 1
        elif block_type in {"thinking", "reasoning"}:
            text = block.get("thinking") or block.get("text") or ""
            if text:
                events.append(Event(seq, timestamp, role, "reasoning", text, file_ref(path, line, raw)))
                seq += 1
        elif block_type in {"toolCall", "tool_call", "tool_use"}:
            name = block.get("name") or block.get("tool") or "tool"
            arguments = block.get("arguments") or block.get("input") or {}
            call_id = block.get("id") or block.get("callId")
            events.append(
                Event(
                    seq,
                    timestamp,
                    role,
                    "tool_call",
                    tool_text(name, arguments),
                    file_ref(path, line, raw),
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
        elif block_type in {"toolResult", "tool_result"}:
            text = content_text(block.get("content") or block.get("result"))
            call_id = block.get("toolCallId") or block.get("tool_use_id") or block.get("id")
            events.append(
                Event(
                    seq,
                    timestamp,
                    role,
                    "tool_result",
                    text,
                    file_ref(path, line, raw),
                    tool_call_id=call_id,
                )
            )
            calls.append(
                ToolCall(
                    id=stable_id(path, line, call_id, "result"),
                    name="tool_result",
                    call_id=call_id,
                    status="completed",
                    output_text=text,
                )
            )
            seq += 1
    return events, calls


def load(source: Source) -> list[Session]:
    session_dir = source.path.parent.parent
    state = _state(session_dir)
    agent_id = source.path.parent.name
    main = agent_id == "main"
    native_id = state.get("id") or session_dir.name
    if not main:
        native_id = f"{native_id}:{agent_id}"

    events: list[Event] = []
    calls: list[ToolCall] = []
    todos: list[Todo] = []
    seq = 0
    last_time: Optional[str] = None
    seen: set[tuple[str, str, str]] = set()
    seen_calls: set[str] = set()

    def add(event: Event) -> None:
        nonlocal seq
        key = (event.role or "", event.kind, event.text[:2000])
        if key in seen:
            return
        seen.add(key)
        event.seq = seq
        events.append(event)
        seq += 1

    for line_no, raw, obj in iter_jsonl(source.path):
        if not isinstance(obj, dict):
            continue
        event_type = obj.get("type")
        timestamp = iso(obj.get("time")) or last_time
        if timestamp:
            last_time = timestamp
        if event_type == "turn.prompt":
            text = content_text(obj.get("input"))
            if text:
                add(Event(seq, timestamp, "user", "message", text, file_ref(source.path, line_no, raw)))
        elif event_type == "context.append_message":
            message = obj.get("message") or {}
            new_events, new_calls = _blocks(
                message.get("content"),
                message.get("role") or "user",
                timestamp,
                seq,
                source.path,
                line_no,
                raw,
            )
            for event in new_events:
                add(event)
            calls.extend(new_calls)
        elif event_type == "agent.message.appended":
            payload = obj.get("message") or {}
            message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
            if not isinstance(message, dict):
                continue
            role = message.get("role") or "assistant"
            new_events, new_calls = _blocks(
                message.get("content"),
                role,
                timestamp,
                seq,
                source.path,
                line_no,
                raw,
            )
            for event in new_events:
                add(event)
            calls.extend(new_calls)
        elif event_type == "context.append_loop_event":
            event = obj.get("event") or {}
            event_kind = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) else event
            if event_kind == "tool.call":
                call_id = data.get("callId") or data.get("call_id") or data.get("id")
                if call_id and call_id in seen_calls:
                    continue
                if call_id:
                    seen_calls.add(call_id)
                name = data.get("name") or data.get("tool") or "tool"
                arguments = data.get("arguments") or data.get("input") or {}
                text = tool_text(name, arguments)
                add(
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
            elif event_kind == "tool.result":
                call_id = data.get("toolCallId") or data.get("callId") or data.get("tool_call_id")
                text = content_text(data.get("result") or data.get("message") or data.get("content"))
                add(
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
        elif event_type == "task.started":
            info = obj.get("info") or {}
            task_id = info.get("taskId") or stable_id(line_no, info.get("description"))
            todos.append(
                Todo(
                    id=task_id,
                    content=info.get("description") or task_id,
                    status="running" if info.get("status") == "running" else info.get("status"),
                    position=len(todos),
                )
            )
        elif event_type == "task.terminated":
            info = obj.get("info") or {}
            task_id = info.get("taskId") or stable_id(line_no, info.get("description"))
            todos.append(
                Todo(
                    id=task_id,
                    content=info.get("description") or task_id,
                    status=info.get("status") or "completed",
                    position=len(todos),
                )
            )
        elif event_type == "interaction.request":
            text = json.dumps(obj.get("request") or {}, ensure_ascii=False)
            add(Event(seq, timestamp, "assistant", "question", text, file_ref(source.path, line_no, raw)))

    if not events:
        return []
    title = state.get("title") if main else _subagent_title(session_dir, agent_id)
    if not title:
        title = title_from(first_user_text(events), native_id)
    session = Session(
        agent=AGENT,
        native_id=native_id,
        title=title,
        project=state.get("cwd"),
        cwd=state.get("cwd"),
        created_at=iso(state.get("createdAt")),
        updated_at=iso(state.get("updatedAt")),
        parent_id=f"{AGENT}:{state.get('id')}" if not main and state.get("id") else None,
        raw_path=str(source.path),
        metadata={"agent_id": agent_id, "main": main, "state": state},
        events=events,
        tool_calls=calls,
        todos=todos,
        source_path=str(source.path),
    )
    session.finalize()
    return [session]