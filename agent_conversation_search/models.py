from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ms_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 1_000_000_000:
        number *= 1000
    if number > 10_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def stable_id(*parts: Any, length: int = 28) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:length]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class RawRef:
    kind: str = "file"
    path: str = ""
    line: Optional[int] = None
    offset: Optional[int] = None
    table: Optional[str] = None
    pk: Optional[str] = None
    column: Optional[str] = None
    compressed: Optional[str] = None
    hash: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


@dataclass
class Event:
    seq: int
    timestamp: Optional[str]
    role: Optional[str]
    kind: str
    text: str
    raw: RawRef
    event_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    subagent_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def finalize(self, session_id: str) -> None:
        raw_marker = self.raw.hash or self.raw.pk or self.raw.line or self.seq
        self.event_id = self.event_id or stable_id(
            session_id, self.seq, self.kind, raw_marker, text_hash(self.text)
        )


@dataclass
class Goal:
    id: str
    objective: str
    status: Optional[str] = None
    phase: Optional[str] = None
    budget: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    event_id: Optional[str] = None


@dataclass
class Todo:
    id: str
    content: str
    status: Optional[str] = None
    priority: Optional[str] = None
    position: Optional[int] = None
    blocked_by: list[str] = field(default_factory=list)
    event_id: Optional[str] = None


@dataclass
class ToolCall:
    id: str
    name: str
    call_id: Optional[str] = None
    status: Optional[str] = None
    input_text: Optional[str] = None
    output_text: Optional[str] = None
    event_id: Optional[str] = None


@dataclass
class Attachment:
    id: str
    path: Optional[str] = None
    mime: Optional[str] = None
    size: Optional[int] = None
    filename: Optional[str] = None
    event_id: Optional[str] = None


@dataclass
class Session:
    agent: str
    native_id: str
    title: Optional[str] = None
    project: Optional[str] = None
    cwd: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    parent_id: Optional[str] = None
    root_id: Optional[str] = None
    raw_path: str = ""
    raw_locator: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    source_path: Optional[str] = None

    @property
    def id(self) -> str:
        return f"{self.agent}:{self.native_id}"

    def finalize(self) -> None:
        self.root_id = self.root_id or self.parent_id or self.id
        if not self.raw_locator:
            self.raw_locator = json_dumps(RawRef(kind="file", path=self.raw_path).to_dict())
        for event in self.events:
            event.finalize(self.id)
            if event.tool_call_id:
                for call in self.tool_calls:
                    if call.call_id == event.tool_call_id and not call.event_id:
                        call.event_id = event.event_id
        self._normalize_tool_calls()

    def _normalize_tool_calls(self) -> None:
        groups: dict[str, ToolCall] = {}
        for call in self.tool_calls:
            key = call.call_id or call.id
            current = groups.get(key)
            if current is None:
                groups[key] = call
                continue
            primary = current if current.name != "tool_result" else call
            result = call if primary is current else current
            if primary.status is None:
                primary.status = result.status
            if result.status in {"error", "failed"}:
                primary.status = result.status
            primary.input_text = primary.input_text or result.input_text
            primary.output_text = primary.output_text or result.output_text
            primary.event_id = primary.event_id or result.event_id
            groups[key] = primary
        self.tool_calls = list(groups.values())