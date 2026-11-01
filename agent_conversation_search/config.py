from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


APP_DIR = Path(os.environ.get("ACS_HOME", Path.home() / ".agent-conversation-search")).expanduser()
DB_PATH = Path(os.environ.get("ACS_DB_PATH", APP_DIR / "index.db")).expanduser()
CONFIG_PATH = APP_DIR / "config.toml"
ENV_PATH = APP_DIR / ".env"

DEFAULT_EMBEDDING_BASE_URL = (
    "https://ws-3sghadur8yw1xmse.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding"


def _load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _keychain_key(service: str) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _path(value: Any, default: Path) -> Path:
    if not value:
        return default
    return Path(str(value)).expanduser()


def load_config() -> dict[str, Any]:
    _load_env()
    site: dict[str, Any] = {}
    if CONFIG_PATH.exists() and tomllib is not None:
        try:
            site = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            site = {}

    sources = site.get("sources", {})
    embedding = site.get("embedding", {})
    index = site.get("index", {})

    return {
        "app_dir": APP_DIR,
        "db_path": _path(os.environ.get("ACS_DB_PATH") or site.get("db_path"), DB_PATH),
        "sources": {
            "claude_projects": _path(
                os.environ.get("ACS_CLAUDE_PROJECTS") or sources.get("claude_projects"),
                Path.home() / ".claude" / "projects",
            ),
            "claude_desktop_glob": os.environ.get("ACS_CLAUDE_DESKTOP_GLOB")
            or sources.get("claude_desktop_glob")
            or str(
                Path.home()
                / "Library"
                / "Application Support"
                / "Claude*"
                / "local-agent-mode-sessions"
            ),
            "codex_dir": _path(
                os.environ.get("ACS_CODEX_DIR") or sources.get("codex_dir"),
                Path.home() / ".codex",
            ),
            "kimi_sessions": _path(
                os.environ.get("ACS_KIMI_SESSIONS") or sources.get("kimi_sessions"),
                Path.home() / ".kimi-code" / "sessions",
            ),
            "opencode_db": _path(
                os.environ.get("ACS_OPENCODE_DB") or sources.get("opencode_db"),
                Path.home() / ".local" / "share" / "opencode" / "opencode.db",
            ),
            "dsh_sessions": _path(
                os.environ.get("ACS_DSH_SESSIONS") or sources.get("dsh_sessions"),
                Path.home() / ".dsh" / "sessions",
            ),
            "hana_agents": _path(
                os.environ.get("ACS_HANA_AGENTS") or sources.get("hana_agents"),
                Path.home() / ".hanako" / "agents",
            ),
            "workbuddy_dir": _path(
                os.environ.get("ACS_WORKBUDDY_DIR") or sources.get("workbuddy_dir"),
                Path.home() / ".workbuddy",
            ),
        },
        "embedding": {
            "base_url": (
                os.environ.get("ACS_EMBEDDING_BASE_URL")
                or embedding.get("base_url")
                or DEFAULT_EMBEDDING_BASE_URL
            ).rstrip("/"),
            "model": os.environ.get("ACS_EMBEDDING_MODEL")
            or embedding.get("model")
            or DEFAULT_EMBEDDING_MODEL,
            "api_key": os.environ.get("ACS_EMBEDDING_API_KEY")
            or embedding.get("api_key")
            or _keychain_key(os.environ.get("ACS_KEYCHAIN_SERVICE", "agent-conversation-search")),
            "batch_size": int(
                os.environ.get("ACS_EMBED_BATCH") or embedding.get("batch_size") or 16
            ),
            "timeout": int(
                os.environ.get("ACS_EMBED_TIMEOUT") or embedding.get("timeout") or 60
            ),
            "max_chars": int(
                os.environ.get("ACS_EMBED_MAX_CHARS") or embedding.get("max_chars") or 6000
            ),
        },
        "index": {
            "max_event_chars": int(
                os.environ.get("ACS_MAX_EVENT_CHARS")
                or index.get("max_event_chars")
                or 20000
            ),
            "active_grace_seconds": int(
                os.environ.get("ACS_ACTIVE_GRACE_SECONDS")
                if os.environ.get("ACS_ACTIVE_GRACE_SECONDS") is not None
                else index.get("active_grace_seconds", 0)
            ),
            "event_caps": index.get(
                "event_caps",
                {
                    "message": 8000,
                    "reasoning": 4000,
                    "tool_call": 2000,
                    "tool_result": 2000,
                    "question": 8000,
                    "file_change": 2000,
                },
            ),
            "include_archived": str(
                os.environ.get("ACS_INCLUDE_ARCHIVED", index.get("include_archived", True))
            ).lower()
            not in {"0", "false", "no", "off"},
            "embed_kinds": index.get(
                "embed_kinds",
                ["message", "reasoning", "question"],
            ),
        },
    }


def ensure_app_dir() -> Path:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        APP_DIR.chmod(0o700)
    except OSError:
        pass
    return APP_DIR