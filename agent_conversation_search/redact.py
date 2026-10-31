from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{12,}\b"), "API_KEY"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "GITHUB_TOKEN"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "SLACK_TOKEN"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS_KEY"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "GOOGLE_KEY"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]{16,}"), "BEARER_TOKEN"),
]

_KEY_VALUE = re.compile(
    r"(?i)((?:api[_-]?key|apikey|access[_-]?key|secret|client[_-]?secret|token|password|passwd|authorization)"
    r"\s*[:=]\s*[\"']?)([A-Za-z0-9._\-+/=]{8,})"
)


def redact_text(text: str) -> str:
    if not text:
        return text
    result = text
    for pattern, label in _PATTERNS:
        result = pattern.sub(f"[REDACTED:{label}]", result)
    result = _KEY_VALUE.sub(lambda match: f"{match.group(1)}[REDACTED:SECRET]", result)
    return result


def is_secret_like(value: str) -> bool:
    if len(value) < 12:
        return False
    return redact_text(value) != value