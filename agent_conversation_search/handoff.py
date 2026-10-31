from __future__ import annotations

import re
from typing import Any, Optional

from .models import now_iso

_PATH_RE = re.compile(
    r"(?:/Users|/private|/tmp|/opt|/Applications|/Library|/Volumes)(?:[^\n\"'<>|,}\]])+"
)
_DECISION_RE = re.compile(
    r"(决定|方案|结论|选择|采用|不再|改为|必须|不能|约束|结论是|最终)"
)


def _event_ref(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("id"),
        "timestamp": event.get("timestamp"),
        "role": event.get("role"),
        "kind": event.get("kind"),
        "raw": event.get("raw"),
    }


def _message_ref(event: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    return {
        **_event_ref(event),
        "text": (event.get("text") or "")[:limit],
        "truncated": len(event.get("text") or "") > limit,
    }


def _extract_files(session: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for call in session.get("tool_calls") or []:
        blob = f"{call.get('input_text') or ''}\n{call.get('output_text') or ''}"
        for path in _PATH_RE.findall(blob):
            path = path.rstrip(".,;:)]}")
            found.setdefault(
                path,
                {
                    "path": path,
                    "first_seen_event": call.get("event_id"),
                    "source": "tool_call",
                },
            )
    for event in session.get("events") or []:
        metadata = event.get("metadata") or {}
        files = metadata.get("files")
        if isinstance(files, dict):
            files = list(files)
        if isinstance(files, list):
            for path in files:
                if isinstance(path, str):
                    found.setdefault(
                        path,
                        {
                            "path": path,
                            "first_seen_event": event.get("id"),
                            "source": "file_change",
                        },
                    )
    for attachment in session.get("attachments") or []:
        path = attachment.get("path") or attachment.get("filename")
        if path:
            found.setdefault(
                path,
                {
                    "path": path,
                    "first_seen_event": attachment.get("event_id"),
                    "source": "attachment",
                },
            )
    return list(found.values())[:100]


def _decisions(session: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for event in session.get("events") or []:
        if event.get("kind") != "message" or event.get("role") != "assistant":
            continue
        text = event.get("text") or ""
        if not _DECISION_RE.search(text):
            continue
        snippet = text.strip().replace("\n", " ")[:500]
        results.append({**_event_ref(event), "snippet": snippet})
        if len(results) >= 12:
            break
    return results


def build_handoff(
    session: dict[str, Any],
    recent: int = 12,
    max_message_chars: int = 4000,
) -> dict[str, Any]:
    events = session.get("events") or []
    messages = [
        event
        for event in events
        if event.get("kind") == "message" and event.get("role") in {"user", "assistant"}
    ]
    recent_messages = (
        [_message_ref(event, max_message_chars) for event in messages[-recent:]]
        if recent > 0
        else []
    )
    recent_events = (
        [
            _message_ref(event, min(max_message_chars, 1500))
            for event in events[-recent * 2 :]
            if event.get("kind")
            in {"message", "reasoning", "tool_call", "tool_result", "file_change"}
        ]
        if recent > 0
        else []
    )
    key_messages: list[dict[str, Any]] = []
    if messages:
        key_messages.append(_message_ref(messages[0], max_message_chars))
    if len(messages) > 1:
        key_messages.append(_message_ref(messages[-1], max_message_chars))
    for event in events:
        if event.get("kind") == "question":
            key_messages.append(_message_ref(event, max_message_chars))
            break

    goals = [
        {
            "id": goal.get("id"),
            "objective": goal.get("objective"),
            "status": goal.get("status"),
            "phase": goal.get("phase"),
            "budget": goal.get("budget"),
            "raw_event": goal.get("event_id"),
        }
        for goal in session.get("goals") or []
    ]
    todos = [
        {
            "id": todo.get("id"),
            "content": todo.get("content"),
            "status": todo.get("status"),
            "priority": todo.get("priority"),
            "blocked_by": todo.get("blocked_by"),
        }
        for todo in session.get("todos") or []
    ]
    blockers: list[dict[str, Any]] = []
    for todo in todos:
        if todo.get("status") not in {"completed", "done", "cancelled"}:
            blockers.append({"kind": "todo", **todo})
    for call in session.get("tool_calls") or []:
        if call.get("status") in {"error", "failed", "running", "pending"}:
            blockers.append(
                {
                    "kind": "tool_call",
                    "name": call.get("name"),
                    "status": call.get("status"),
                    "call_id": call.get("call_id"),
                }
            )
    for event in events:
        metadata = event.get("metadata") or {}
        if metadata.get("is_error"):
            blockers.append(
                {
                    "kind": "event_error",
                    "event_id": event.get("id"),
                    "role": event.get("role"),
                    "snippet": (event.get("text") or "")[:300],
                    "raw": event.get("raw"),
                }
            )

    tool_state: dict[str, int] = {}
    for call in session.get("tool_calls") or []:
        status = call.get("status") or "unknown"
        tool_state[status] = tool_state.get(status, 0) + 1

    raw_refs: list[dict[str, Any]] = []
    seen_raw: set[str] = set()
    for item in recent_messages + recent_events + key_messages:
        raw = item.get("raw")
        marker = repr(raw)
        if raw and marker not in seen_raw:
            seen_raw.add(marker)
            raw_refs.append(raw)
    session_raw = session.get("raw_locator") or session.get("raw")
    if session_raw:
        raw_refs.append(session_raw)

    overview = (
        f"{session.get('agent')} / {session.get('title') or 'untitled'} / "
        f"{len(events)} events / {len(messages)} messages"
    )
    return {
        "handoff_version": 1,
        "trace_id": f"handoff:{session.get('id')}@{now_iso()}",
        "generated_at": now_iso(),
        "source": {
            "agent": session.get("agent"),
            "session_id": session.get("id"),
            "native_id": session.get("native_id"),
            "title": session.get("title"),
            "cwd": session.get("cwd"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "raw": session_raw,
            "event_count": len(events),
        },
        "overview": overview,
        "goals": goals,
        "todos": todos,
        "blockers": blockers[:30],
        "files": _extract_files(session)[:100],
        "decisions": _decisions(session),
        "recent_messages": recent_messages,
        "recent_events": recent_events,
        "key_messages": key_messages,
        "tool_state": tool_state,
        "raw_refs": raw_refs,
        "full_context": {
            "command": f"acs get --session {session.get('id')} --format json",
            "note": "Use get for the complete event stream; handoff is a navigation package, not a replacement.",
        },
    }


def to_markdown(package: dict[str, Any]) -> str:
    source = package["source"]
    lines = [
        f"# Handoff: {source.get('title') or 'untitled'}",
        "",
        f"- Agent: `{source.get('agent')}`",
        f"- Session: `{source.get('session_id')}`",
        f"- CWD: `{source.get('cwd')}`",
        f"- Events: {source.get('event_count')}",
        f"- Raw: `{source.get('raw')}`",
        "",
        "## Goals",
    ]
    if package["goals"]:
        for goal in package["goals"]:
            lines.append(f"- [{goal.get('status')}] {goal.get('objective')} (`{goal.get('id')}`)")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Todos"])
    if package["todos"]:
        for todo in package["todos"]:
            lines.append(f"- [{todo.get('status')}] {todo.get('content')} (`{todo.get('id')}`)")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Blockers"])
    if package["blockers"]:
        for blocker in package["blockers"]:
            lines.append(f"- {blocker}")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Files"])
    if package["files"]:
        for item in package["files"]:
            lines.append(f"- `{item['path']}`")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Recent Events"])
    for event in package.get("recent_events", [])[-12:]:
        lines.append(f"- {event.get('role')}/{event.get('kind')}: {(event.get('text') or '')[:300]}")
    lines.extend(["", "## Recent Messages"])
    for message in package["recent_messages"]:
        lines.append(f"### {message.get('role')} @ {message.get('timestamp')}")
        lines.append(message.get("text") or "")
        lines.append(f"raw: `{message.get('raw')}`")
        lines.append("")
    lines.extend(["## Full Context", package["full_context"]["command"]])
    return "\n".join(lines)