from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..models import Attachment, Event, RawRef, Session, Todo, ToolCall, stable_id
from .base import Source, content_text, first_user_text, iso, title_from, tool_text

AGENT = "opencode"


def discover(config: dict[str, Any]) -> list[Source]:
    path = config["sources"]["opencode_db"]
    return [Source(agent=AGENT, path=path, kind="sqlite")] if path.exists() else []


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load(source: Source) -> list[Session]:
    path = source.path
    conn = _connect(path)
    sessions = [dict(row) for row in conn.execute("SELECT * FROM session ORDER BY time_created")]
    messages = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT id, session_id, time_created, data FROM message")
    }
    parts_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT id, message_id, session_id, time_created, data FROM part ORDER BY time_created, id"
    ):
        parts_by_session.setdefault(row["session_id"], []).append(dict(row))
    todos_by_session: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute("SELECT * FROM todo ORDER BY position, rowid"):
        todos_by_session.setdefault(row["session_id"], []).append(dict(row))
    conn.close()

    output: list[Session] = []
    for row in sessions:
        native_id = row["id"]
        events: list[Event] = []
        calls: list[ToolCall] = []
        attachments: list[Attachment] = []
        seq = 0
        for part in parts_by_session.get(native_id, []):
            try:
                data = json.loads(part["data"])
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            message = messages.get(part["message_id"], {})
            try:
                message_data = json.loads(message.get("data", "{}"))
            except (TypeError, ValueError):
                message_data = {}
            role = message_data.get("role") or "assistant"
            timestamp = iso(part.get("time_created")) or iso(message.get("time_created"))
            raw = RawRef(
                kind="sqlite",
                path=str(path),
                table="part",
                pk=part["id"],
                column="data",
                hash=stable_id(part["id"], part["data"]),
            )
            part_type = data.get("type")
            if part_type == "text":
                text = data.get("text") or ""
                if text:
                    events.append(Event(seq, timestamp, role, "message", text, raw))
                    seq += 1
            elif part_type == "reasoning":
                text = data.get("text") or ""
                if text:
                    events.append(Event(seq, timestamp, role, "reasoning", text, raw))
                    seq += 1
            elif part_type == "tool":
                name = data.get("tool") or "tool"
                state = data.get("state") if isinstance(data.get("state"), dict) else {}
                call_id = data.get("callID") or data.get("call_id")
                input_value = state.get("input", {})
                tool_output = state.get("output")
                status = state.get("status")
                text = tool_text(name, input_value)
                event = Event(
                    seq,
                    timestamp,
                    role,
                    "tool_call",
                    text,
                    raw,
                    tool_call_id=call_id,
                    metadata={"status": status, "tool": name},
                )
                events.append(event)
                calls.append(
                    ToolCall(
                        id=stable_id(part["id"], call_id, "call"),
                        name=name,
                        call_id=call_id,
                        status=status,
                        input_text=text,
                        output_text=content_text(tool_output),
                    )
                )
                seq += 1
                if tool_output:
                    result = Event(
                        seq,
                        timestamp,
                        "tool",
                        "tool_result",
                        content_text(tool_output),
                        raw,
                        tool_call_id=call_id,
                    )
                    events.append(result)
                    calls.append(
                        ToolCall(
                            id=stable_id(part["id"], call_id, "result"),
                            name="tool_result",
                            call_id=call_id,
                            status=status,
                            output_text=content_text(tool_output),
                        )
                    )
                    seq += 1
            elif part_type == "patch":
                files = data.get("files") or []
                if files:
                    event = Event(
                        seq,
                        timestamp,
                        role,
                        "file_change",
                        "\n".join(str(item) for item in files),
                        raw,
                        metadata={"files": files},
                    )
                    events.append(event)
                    seq += 1
            elif part_type == "file":
                filename = data.get("filename") or "attachment"
                mime = data.get("mime")
                event = Event(seq, timestamp, role, "attachment", filename, raw, metadata=data)
                events.append(event)
                attachments.append(
                    Attachment(
                        id=stable_id(part["id"], filename),
                        filename=filename,
                        mime=mime,
                        event_id=event.event_id,
                    )
                )
                seq += 1
        if not events:
            continue
        title = row.get("title") or title_from(first_user_text(events), native_id)
        session = Session(
            agent=AGENT,
            native_id=native_id,
            title=title,
            project=row.get("directory"),
            cwd=row.get("directory"),
            created_at=iso(row.get("time_created")),
            updated_at=iso(row.get("time_updated")),
            parent_id=f"{AGENT}:{row['parent_id']}" if row.get("parent_id") else None,
            raw_path=str(path),
            raw_locator=json.dumps(
                RawRef(kind="sqlite", path=str(path), table="session", pk=native_id).to_dict(),
                ensure_ascii=False,
            ),
            metadata={
                "agent": row.get("agent"),
                "model": row.get("model"),
                "version": row.get("version"),
                "tokens_input": row.get("tokens_input"),
                "tokens_output": row.get("tokens_output"),
            },
            events=events,
            tool_calls=calls,
            attachments=attachments,
            source_path=str(path),
        )
        for index, todo in enumerate(todos_by_session.get(native_id, [])):
            session.todos.append(
                Todo(
                    id=str(todo.get("id") or f"{native_id}:todo:{index}"),
                    content=todo.get("content") or "",
                    status=todo.get("status"),
                    priority=todo.get("priority"),
                    position=todo.get("position", index),
                )
            )
        session.finalize()
        output.append(session)
    return output