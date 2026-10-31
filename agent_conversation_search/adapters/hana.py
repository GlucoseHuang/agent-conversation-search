from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..models import Attachment, Event, Session, ToolCall, stable_id
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

AGENT = "hana"


def discover(config: dict[str, Any]) -> list[Source]:
    root = config["sources"]["hana_agents"]
    sources: list[Source] = []
    if not root.exists():
        return sources
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        for pattern in ("sessions/**/*.jsonl", "subagent-sessions/**/*.jsonl"):
            for path in agent_dir.glob(pattern):
                sources.append(Source(agent=AGENT, path=path, metadata={"agent_dir": str(agent_dir)}))
    return sources


def _blocks(
    content: Any,
    role: str,
    timestamp: Optional[str],
    seq_start: int,
    path: Path,
    line: int,
    raw: bytes,
) -> tuple[list[Event], list[ToolCall], list[Attachment]]:
    events: list[Event] = []
    calls: list[ToolCall] = []
    attachments: list[Attachment] = []
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
            call_id = block.get("toolCallId") or block.get("tool_use_id")
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
        elif block_type in {"image", "document"}:
            filename = block.get("filename") or block.get("name") or block_type
            safe = {
                key: value
                for key, value in block.items()
                if key in {"type", "filename", "name", "mime_type", "mime", "path"}
            }
            event = Event(
                seq,
                timestamp,
                role,
                "attachment",
                filename,
                file_ref(path, line, raw),
                metadata=safe,
            )
            events.append(event)
            attachments.append(
                Attachment(
                    id=stable_id(path, line, filename),
                    filename=filename,
                    mime=block.get("mime_type") or block.get("mime"),
                    event_id=event.event_id,
                )
            )
            seq += 1
    return events, calls, attachments


def load(source: Source) -> list[Session]:
    events: list[Event] = []
    calls: list[ToolCall] = []
    attachments: list[Attachment] = []
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    parent_id: Optional[str] = None
    title: Optional[str] = None
    model: Optional[str] = None
    seq = 0

    for line_no, raw, obj in iter_jsonl(source.path):
        if not isinstance(obj, dict):
            continue
        line_type = obj.get("type")
        timestamp = iso(obj.get("timestamp")) or updated_at
        if line_type == "session":
            session_id = obj.get("id") or session_id
            cwd = cwd or obj.get("cwd")
            created_at = timestamp or created_at
            parent_id = obj.get("parentSessionId") or obj.get("parent_id") or parent_id
        elif line_type == "model_change":
            model = obj.get("modelId") or model
        elif line_type == "thinking_level_change":
            pass
        elif line_type == "message":
            message = obj.get("message") or {}
            role = message.get("role") or "assistant"
            message_timestamp = iso(message.get("timestamp")) or timestamp
            if role in {"toolResult", "tool_result"}:
                event = Event(
                    seq,
                    message_timestamp,
                    "tool",
                    "tool_result",
                    content_text(message.get("content")),
                    file_ref(source.path, line_no, raw),
                    tool_call_id=message.get("toolCallId"),
                    metadata={
                        "tool_name": message.get("toolName"),
                        "is_error": bool(message.get("isError")),
                    },
                )
                events.append(event)
                seq += 1
                if message_timestamp:
                    updated_at = message_timestamp
                continue
            new_events, new_calls, new_attachments = _blocks(
                message.get("content"),
                role,
                message_timestamp,
                seq,
                source.path,
                line_no,
                raw,
            )
            events.extend(new_events)
            calls.extend(new_calls)
            attachments.extend(new_attachments)
            seq += len(new_events)
            if message_timestamp:
                updated_at = message_timestamp

    if not session_id:
        session_id = source.path.stem
    if not events:
        return []
    if not title:
        title = title_from(first_user_text(events), session_id)
    is_subagent = "subagent-sessions" in str(source.path)
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
        metadata={"subagent": is_subagent, "model": model, "source": str(source.path)},
        events=events,
        tool_calls=calls,
        attachments=attachments,
        source_path=str(source.path),
    )
    session.finalize()
    return [session]