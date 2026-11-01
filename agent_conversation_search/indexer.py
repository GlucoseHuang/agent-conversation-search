from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .adapters import ADAPTERS, claude_code, codex, dsh, hana, kimi, opencode, workbuddy
from .adapters.base import Source
from .config import load_config
from .db import (
    connect,
    init_db,
    remove_stale_sessions,
    replace_session,
    reset_db,
    stats,
)
from .embeddings import EmbeddingClient, EmbeddingUnavailable, embed_pending
from .models import now_iso

MODULES = [claude_code, codex, kimi, opencode, dsh, hana, workbuddy]


def discover_all(config: dict[str, Any], agents: Optional[list[str]] = None) -> list[Source]:
    wanted = set(agents or [])
    sources: list[Source] = []
    seen: set[tuple[str, str]] = set()
    for module in MODULES:
        for source in module.discover(config):
            if wanted and source.agent not in wanted:
                continue
            key = (source.agent, str(source.path))
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)
    sources.sort(key=lambda item: (item.agent, str(item.path)))
    return sources


def _signature(path: Path, kind: str = "file") -> tuple[int, float]:
    stat = path.stat()
    size = stat.st_size
    mtime = stat.st_mtime
    if kind == "sqlite":
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            wal_stat = wal.stat()
            size += wal_stat.st_size
            mtime = max(mtime, wal_stat.st_mtime)
    return size, mtime


def _source_row(conn: sqlite3.Connection, path: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()


def index_source(
    conn: sqlite3.Connection,
    source: Source,
    config: dict[str, Any],
    force: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    active_grace_seconds: int = 0,
    include_active: bool = False,
) -> dict[str, Any]:
    path = source.path
    if not path.exists():
        return {"path": str(path), "status": "missing", "sessions": 0}
    size, mtime = _signature(path, source.kind)
    age = max(0.0, time.time() - mtime)
    if not force and not include_active and active_grace_seconds and age < active_grace_seconds:
        return {
            "path": str(path),
            "status": "active",
            "sessions": 0,
            "age_seconds": round(age, 1),
        }
    row = _source_row(conn, str(path))
    if (
        not force
        and row
        and row["size"] == size
        and abs((row["mtime"] or 0) - mtime) < 0.001
        and row["status"] == "ok"
    ):
        return {"path": str(path), "status": "skipped", "sessions": 0}

    module = ADAPTERS.get(source.agent)
    if module is None:
        return {"path": str(path), "status": "unsupported", "sessions": 0}
    try:
        sessions = module.load(source)
        for session in sessions:
            replace_session(
                conn,
                session,
                config["index"]["max_event_chars"],
                config["index"]["event_caps"],
            )
        remove_stale_sessions(
            conn, source.agent, str(path), [session.id for session in sessions]
        )
        conn.execute(
            "INSERT INTO sources(path, agent, kind, size, mtime, last_indexed_at, status, error) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET agent=excluded.agent, kind=excluded.kind, "
            "size=excluded.size, mtime=excluded.mtime, last_indexed_at=excluded.last_indexed_at, "
            "status=excluded.status, error=excluded.error",
            (str(path), source.agent, source.kind, size, mtime, now_iso(), "ok", None),
        )
        conn.commit()
        if progress:
            progress(f"  {source.agent}: {len(sessions)} session(s) <- {path}")
        return {"path": str(path), "status": "ok", "sessions": len(sessions)}
    except Exception as error:  # noqa: BLE001
        conn.rollback()
        conn.execute(
            "INSERT INTO sources(path, agent, kind, size, mtime, last_indexed_at, status, error) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
            "last_indexed_at=excluded.last_indexed_at, status=excluded.status, error=excluded.error",
            (str(path), source.agent, source.kind, size, mtime, now_iso(), "error", str(error)[:1000]),
        )
        conn.commit()
        if progress:
            progress(f"  ERROR {source.agent}: {path}: {error}")
        return {"path": str(path), "status": "error", "sessions": 0, "error": str(error)}


def index_all(
    config: Optional[dict[str, Any]] = None,
    agents: Optional[list[str]] = None,
    full: bool = False,
    no_embed: bool = False,
    embed_limit: Optional[int] = None,
    dry_run: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    active_grace_seconds: Optional[int] = None,
    include_active: bool = False,
) -> dict[str, Any]:
    config = config or load_config()
    sources = discover_all(config, agents)
    if dry_run:
        return {"sources": [{"agent": item.agent, "path": str(item.path)} for item in sources]}

    conn = connect(config["db_path"])
    init_db(conn)
    if full:
        reset_db(conn)

    results: list[dict[str, Any]] = []
    grace = (
        config["index"]["active_grace_seconds"]
        if active_grace_seconds is None
        else active_grace_seconds
    )
    for source in sources:
        result = index_source(
            conn,
            source,
            config,
            force=full,
            progress=progress,
            active_grace_seconds=grace,
            include_active=include_active,
        )
        results.append(result)

    embedded: dict[str, Any] = {}
    if not no_embed:
        embedding = config["embedding"]
        client = EmbeddingClient(
            base_url=embedding["base_url"],
            model=embedding["model"],
            api_key=embedding["api_key"],
            batch_size=embedding["batch_size"],
            timeout=embedding["timeout"],
            max_chars=embedding["max_chars"],
        )
        if client.available:
            try:
                embedded = embed_pending(
                    conn,
                    client,
                    config["index"]["embed_kinds"],
                    limit=embed_limit,
                    batch_size=embedding["batch_size"],
                    progress=(lambda done, total: progress(f"  embedded {done}/{total}"))
                    if progress
                    else None,
                )
            except EmbeddingUnavailable as error:
                embedded = {"error": str(error)}
                if progress:
                    progress(f"  embedding skipped: {error}")
        else:
            embedded = {"skipped": "ACS_EMBEDDING_API_KEY is not set"}

    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    summary = stats(conn, check_orphans=False)
    conn.close()
    return {
        "indexed": results,
        "embedded": embedded,
        "stats": summary,
    }