from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from ..models import Attachment, Event, Session, Todo, ToolCall, stable_id
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

AGENT = "claude-code"
DESKTOP_AGENT = "claude-desktop-code"


def discover(config: dict[str, Any]) -> list[Source]:
    sources: list[Source] = []
    root = config["sources"]["claude_projects"]
    if root.exists():
        for path in root.rglob("*.jsonl"):
            if "/subagents/" in str(path):
                continue
            sources.append(Source(agent=AGENT, path=path))

    desktop_glob = config["sources"]["claude_desktop_glob"]
    import glob

    for root_text in glob.glob(desktop_glob):
        base = Path(root_text)
        if not base.exists():
            continue
        for path in base.glob("*/*/*/.claude/projects/**/*.jsonl"):
            if "/subagents/" in str(path):
                continue
            sources.append(Source(agent=DESKTOP_AGENT, path=path))
    return sources


def _metadata_for(path: Path) -> dict[str, Any]:
    parts = path.parts
    try:
        index = parts.index(".claude")
    except ValueError:
        return {}
    base = Path(*parts[:index]).parent
    metadata: dict[str, Any] = {}
    for candidate in base.glob("local_*.json"):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if value.get("cliSessionId") and value.get("cliSessionId") in str(path):
            metadata.update(value)
            break
    return metadata


def _blocks(
    blocks: Any,
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
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    if not isinstance(blocks, list):
        blocks = [{"type": "text", "text": content_text(blocks)}]

    seq = seq_start
    for block in blocks:
        if isinstance(block, str):
            events.append(
                Event(seq, timestamp, role, "message", block, file_ref(path, line, raw))
            )
            seq += 1
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text") or block.get("input_text") or block.get("output_text") or ""
            if text:
                events.append(
                    Event(seq, timestamp, role, "message", text, file_ref(path, line, raw))
                )
                seq += 1
        elif block_type in {"thinking", "reasoning", "redacted_thinking"}:
            text = block.get("thinking") or block.get("text") or content_text(block)
            if text:
                events.append(
                    Event(seq, timestamp, role, "reasoning", text, file_ref(path, line, raw))
                )
                seq += 1
        elif block_type == "tool_use":
            name = block.get("name", "tool")
            text = tool_text(name, block.get("input", {}))
            call_id = block.get("id")
            event = Event(
                seq,
                timestamp,
                role,
                "tool_call",
                text,
                file_ref(path, line, raw),
                tool_call_id=call_id,
            )
            events.append(event)
            calls.append(
                ToolCall(
                    id=stable_id(path, line, call_id, "call"),
                    name=name,
                    call_id=call_id,
                    input_text=text,
                    event_id=event.event_id,
                )
            )
            seq += 1
        elif block_type == "tool_result":
            text = content_text(block.get("content"))
            call_id = block.get("tool_use_id")
            event = Event(
                seq,
                timestamp,
                role,
                "tool_result",
                text,
                file_ref(path, line, raw),
                tool_call_id=call_id,
                metadata={"is_error": block.get("is_error", False)},
            )
            events.append(event)
            calls.append(
                ToolCall(
                    id=stable_id(path, line, call_id, "result"),
                    name="tool_result",
                    call_id=call_id,
                    status="error" if block.get("is_error") else "completed",
                    output_text=text,
                    event_id=event.event_id,
                )
            )
            seq += 1
        elif block_type in {"image", "document"}:
            source = block.get("source", {})
            filename = block.get("name") or source.get("filename")
            mime = source.get("media_type") or block.get("mime_type")
            event = Event(
                seq,
                timestamp,
                role,
                "attachment",
                filename or block_type,
                file_ref(path, line, raw),
                metadata={"block": block_type, "mime": mime},
            )
            events.append(event)
            attachments.append(
                Attachment(
                    id=stable_id(path, line, block_type, filename),
                    filename=filename,
                    mime=mime,
                    event_id=event.event_id,
                )
            )
            seq += 1
    return events, calls, attachments


def _load_session(
    source: Source,
    path: Path,
    agent: str,
    parent_id: Optional[str] = None,
    native_id: Optional[str] = None,
) -> Optional[Session]:
    events: list[Event] = []
    calls: list[ToolCall] = []
    attachments: list[Attachment] = []
    title: Optional[str] = None
    cwd: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    session_id: Optional[str] = None
    seq = 0
    metadata = _metadata_for(path) if agent == DESKTOP_AGENT else {}

    for line_no, raw, obj in iter_jsonl(path):
        if not isinstance(obj, dict):
            continue
        line_type = obj.get("type")
        timestamp = obj.get("timestamp")
        if timestamp and not created_at:
            created_at = timestamp
        if timestamp:
            updated_at = timestamp
        if obj.get("sessionId"):
            session_id = obj["sessionId"]
        if obj.get("cwd") and not cwd:
            cwd = obj["cwd"]
        if line_type == "custom-title":
            title = obj.get("customTitle") or title
        elif line_type == "agent-name":
            metadata.setdefault("agent_name", obj.get("agentName"))
        elif line_type == "mode":
            metadata.setdefault("mode", obj.get("mode"))
        elif line_type == "last-prompt":
            metadata.setdefault("last_prompt", obj.get("lastPrompt"))
        elif line_type == "system":
            text = content_text(obj.get("content") or obj.get("message"))
            if text:
                events.append(
                    Event(seq, timestamp, "system", "system", text, file_ref(path, line_no, raw))
                )
                seq += 1
        elif line_type == "attachment":
            attachment = obj.get("attachment") or {}
            text = attachment.get("filename") or attachment.get("type") or "attachment"
            event = Event(
                seq,
                timestamp,
                "attachment",
                "attachment",
                text,
                file_ref(path, line_no, raw),
                metadata=attachment,
            )
            events.append(event)
            attachments.append(
                Attachment(
                    id=stable_id(path, line_no, text),
                    filename=attachment.get("filename"),
                    mime=attachment.get("mime_type") or attachment.get("media_type"),
                    path=attachment.get("path"),
                    size=attachment.get("size"),
                    event_id=event.event_id,
                )
            )
            seq += 1
        elif line_type in {"user", "assistant"}:
            message = obj.get("message") or {}
            role = message.get("role") or line_type
            new_events, new_calls, new_attachments = _blocks(
                message.get("content"),
                role,
                timestamp,
                seq,
                path,
                line_no,
                raw,
            )
            events.extend(new_events)
            calls.extend(new_calls)
            attachments.extend(new_attachments)
            seq += len(new_events)

    if not session_id:
        session_id = path.stem
    native_id = native_id or session_id
    if not created_at:
        created_at = iso(metadata.get("createdAt"))
    if not updated_at:
        updated_at = iso(metadata.get("lastActivityAt"))
    if metadata.get("title"):
        title = metadata["title"] or title
    if not title:
        title = title_from(first_user_text(events), native_id)
    cwd = metadata.get("cwd") or cwd
    if not events and not metadata:
        return None

    session = Session(
        agent=agent,
        native_id=native_id,
        title=title,
        project=cwd,
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        parent_id=parent_id,
        raw_path=str(path),
        metadata={**metadata, "source": str(path)},
        events=events,
        tool_calls=calls,
        attachments=attachments,
        source_path=str(path),
    )
    session.finalize()
    return session


def _tasks_for(path: Path, session_id: str) -> list[Todo]:
    parts = path.parts
    try:
        index = parts.index(".claude")
    except ValueError:
        return []
    base = Path(*parts[:index])
    tasks_dir = base / ".claude" / "tasks" / session_id
    if not tasks_dir.exists():
        return []
    todos: list[Todo] = []
    for task_path in sorted(tasks_dir.glob("*.json")):
        try:
            value = json.loads(task_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        todos.append(
            Todo(
                id=str(value.get("id") or task_path.stem),
                content=value.get("subject") or value.get("description") or "",
                status=value.get("status"),
                priority=value.get("priority"),
                position=int(value.get("id")) if str(value.get("id", "")).isdigit() else None,
                blocked_by=list(value.get("blockedBy") or []),
            )
        )
    return todos


def load(source: Source) -> list[Session]:
    main = _load_session(source, source.path, source.agent)
    if not main:
        return []
    if source.agent == DESKTOP_AGENT:
        main.todos.extend(_tasks_for(source.path, main.native_id))
    sessions = [main]

    stem_dir = source.path.with_suffix("")
    subagent_dir = stem_dir / "subagents"
    if subagent_dir.exists():
        for path in sorted(subagent_dir.glob("*.jsonl")):
            sub = _load_session(
                source,
                path,
                source.agent,
                parent_id=main.id,
                native_id=f"{main.native_id}:sub:{path.stem}",
            )
            if sub:
                sub.title = sub.title or f"subagent {path.stem}"
                sessions.append(sub)
    return sessions