from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

APP_DIR = Path.home() / ".agent-conversation-search"
DEFAULT_TARGET = Path.home() / ".agents" / "skills" / "agent-conversation-search"
SYMLINK_PARENTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".hanako" / "skills",
    Path.home() / ".codex" / "skills",
    Path.home() / ".workbuddy" / "skills",
    Path.home() / ".dsh" / "skills",
    Path.home() / ".config" / "opencode" / "skills",
    Path.home() / ".kimi-code" / "skills",
]
IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "__pycache__",
    "*.pyc",
    "index.db",
    "index.db-*",
    ".env",
    "tests",
    ".pytest_cache",
    "*.egg-info",
)


def _trash(path: Path) -> Optional[Path]:
    if not path.exists() and not path.is_symlink():
        return None
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    stamp = f"agent-conversation-search-backup-{datetime.now():%Y%m%d-%H%M%S}"
    target = trash / stamp
    counter = 1
    while target.exists():
        target = trash / f"{stamp}-{counter}"
        counter += 1
    shutil.move(str(path), str(target))
    return target


def install(
    force: bool = False,
    replace_old: bool = False,
    copy: bool = False,
    target: Optional[Path] = None,
) -> dict[str, Any]:
    repo = Path(__file__).resolve().parent.parent
    target = (target or DEFAULT_TARGET).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"target": str(target), "links": [], "backups": []}

    if target.exists() or target.is_symlink():
        if not force:
            raise SystemExit(f"target exists: {target} (use --force)")
        backup = _trash(target)
        if backup:
            result["backups"].append(str(backup))

    if copy:
        shutil.copytree(repo, target, ignore=IGNORE)
        (target / "acs.py").chmod(0o755)
    else:
        target.symlink_to(repo, target_is_directory=True)

    for parent in SYMLINK_PARENTS:
        if not parent.exists():
            continue
        link = parent / "agent-conversation-search"
        if link.exists() or link.is_symlink():
            if link.resolve() == target.resolve():
                result["links"].append(str(link))
                continue
            if not force:
                continue
            backup = _trash(link)
            if backup:
                result["backups"].append(str(backup))
        link.symlink_to(target, target_is_directory=True)
        result["links"].append(str(link))

    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    acs_link = local_bin / "acs"
    if acs_link.exists() or acs_link.is_symlink():
        backup = _trash(acs_link)
        if backup:
            result["backups"].append(str(backup))
    acs_link.write_text(
        "#!/bin/sh\n"
        f'exec python3 "{target / "acs.py"}" "$@"\n',
        encoding="utf-8",
    )
    acs_link.chmod(0o755)
    result["acs"] = str(acs_link)

    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        APP_DIR.chmod(0o700)
    except OSError:
        pass
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        env_path.write_text(
            "ACS_EMBEDDING_BASE_URL=\nACS_EMBEDDING_API_KEY=\nACS_EMBEDDING_MODEL=qwen3.7-text-embedding\n",
            encoding="utf-8",
        )
        env_path.chmod(0o600)
    result["env"] = str(env_path)

    if replace_old:
        old = Path.home() / ".hanako" / "skills" / "conversation-search"
        backup = _trash(old)
        if backup:
            result["backups"].append(str(backup))

    return result