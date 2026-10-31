from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from ..models import RawRef


@dataclass
class Source:
    agent: str
    path: Path
    kind: str = "file"
    compressed: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000
    elif number < 1_000_000_000:
        number *= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()


def digest(raw: bytes | str) -> str:
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def iter_zstd_lines(path: Path) -> Iterator[tuple[int, bytes]]:
    zstd_bin = shutil.which("zstd")
    if zstd_bin:
        process = subprocess.Popen(
            [zstd_bin, "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        try:
            for line_no, raw in enumerate(process.stdout, 1):
                yield line_no, raw
        finally:
            process.stdout.close()
            process.wait()
        return
    import zstandard as zstd

    with path.open("rb") as handle:
        reader = zstd.ZstdDecompressor().stream_reader(handle, read_across_frames=True)
        for line_no, raw in enumerate(reader, 1):
            yield line_no, raw


def iter_jsonl(path: Path, compressed: Optional[str] = None) -> Iterator[tuple[int, bytes, Any]]:
    is_zstd = compressed == "zstd" or path.suffix in {".zst", ".zstd"}
    try:
        if is_zstd:
            for line_no, raw in iter_zstd_lines(path):
                yield line_no, raw, _parse(raw)
        else:
            with path.open("rb") as handle:
                for line_no, raw in enumerate(handle, 1):
                    yield line_no, raw, _parse(raw)
    except (OSError, ValueError):
        return


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def file_ref(path: Path, line: int, raw: bytes | str, compressed: Optional[str] = None) -> RawRef:
    return RawRef(
        kind="file",
        path=str(path),
        line=line,
        compressed=compressed,
        hash=digest(raw),
    )


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            for key in ("text", "input_text", "output_text", "thinking", "reasoning_text"):
                value = block.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
                    break
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "content", "message", "output"):
            if key in content:
                value = content[key]
                if isinstance(value, str):
                    return value
                if isinstance(value, list):
                    return content_text(value)
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return content_text(message)
    return content_text(message.get("content"))


_INJECTED_PREFIXES = (
    "<system-reminder>",
    "<git-context>",
    "<INSTRUCTIONS>",
    "<app-context>",
    "Current runtime context",
    "You are running as a subagent",
    "# AGENTS.md",
)


def first_user_text(events: Iterable[Any]) -> Optional[str]:
    for event in events:
        if event.role != "user" or event.kind != "message":
            continue
        text = event.text.strip()
        if not text or text.startswith(_INJECTED_PREFIXES):
            continue
        return text.replace("\n", " ")[:100]
    return None


def title_from(text: Optional[str], fallback: str) -> str:
    if text:
        return text.strip().replace("\n", " ")[:120]
    return fallback


def tool_text(name: str, arguments: Any) -> str:
    if isinstance(arguments, str):
        body = arguments
    else:
        try:
            body = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(arguments)
    return f"{name} {body}"