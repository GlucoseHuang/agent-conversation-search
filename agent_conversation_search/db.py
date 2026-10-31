from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import DB_PATH
from .models import (
    Attachment,
    Event,
    Goal,
    Session,
    Todo,
    ToolCall,
    json_dumps,
    text_hash,
)
from .redact import redact_text

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  path TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'file',
  size INTEGER,
  mtime REAL,
  sha256 TEXT,
  last_indexed_at TEXT,
  status TEXT DEFAULT 'ok',
  error TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  native_id TEXT NOT NULL,
  source_path TEXT,
  title TEXT,
  project TEXT,
  cwd TEXT,
  created_at TEXT,
  updated_at TEXT,
  parent_id TEXT,
  root_id TEXT,
  raw_path TEXT,
  raw_locator TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source_path);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  agent TEXT NOT NULL,
  seq INTEGER,
  timestamp TEXT,
  role TEXT,
  kind TEXT NOT NULL,
  text TEXT,
  text_hash TEXT,
  raw_path TEXT,
  raw_locator TEXT,
  raw_line INTEGER,
  raw_offset INTEGER,
  raw_hash TEXT,
  fts_rowid INTEGER,
  parent_event_id TEXT,
  tool_call_id TEXT,
  subagent_id TEXT,
  metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);

CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY,
  event_id TEXT,
  session_id TEXT,
  name TEXT,
  call_id TEXT,
  status TEXT,
  input_text TEXT,
  output_text TEXT
);

CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  objective TEXT,
  status TEXT,
  phase TEXT,
  budget_json TEXT,
  created_at TEXT,
  updated_at TEXT,
  event_id TEXT
);

CREATE TABLE IF NOT EXISTS todos (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  content TEXT,
  status TEXT,
  priority TEXT,
  position INTEGER,
  blocked_by TEXT,
  event_id TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  event_id TEXT,
  path TEXT,
  mime TEXT,
  size INTEGER,
  filename TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
  event_id TEXT PRIMARY KEY,
  model TEXT,
  dim INTEGER,
  vector BLOB,
  text_hash TEXT,
  created_at TEXT
);
"""


def connect(path: Optional[Path] = None, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    else:
        conn = sqlite3.connect(db_path, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _create_fts(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts "
            "USING fts5(event_id UNINDEXED, session_id UNINDEXED, text, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts "
            "USING fts5(event_id UNINDEXED, session_id UNINDEXED, text)"
        )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _create_fts(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "fts_rowid" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN fts_rowid INTEGER")
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    for table in [
        "events_fts",
        "events",
        "tool_calls",
        "goals",
        "todos",
        "attachments",
        "embeddings",
        "sessions",
        "sources",
        "meta",
    ]:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    init_db(conn)


def _json(value: Any) -> str:
    return json_dumps(value)


def upsert_session(conn: sqlite3.Connection, session: Session) -> None:
    session.finalize()
    conn.execute(
        """
        INSERT INTO sessions (
          id, agent, native_id, source_path, title, project, cwd, created_at,
          updated_at, parent_id, root_id, raw_path, raw_locator, metadata_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          source_path=excluded.source_path,
          title=excluded.title,
          project=excluded.project,
          cwd=excluded.cwd,
          created_at=excluded.created_at,
          updated_at=excluded.updated_at,
          parent_id=excluded.parent_id,
          root_id=excluded.root_id,
          raw_path=excluded.raw_path,
          raw_locator=excluded.raw_locator,
          metadata_json=excluded.metadata_json
        """,
        (
            session.id,
            session.agent,
            session.native_id,
            session.source_path,
            session.title,
            session.project,
            session.cwd,
            session.created_at,
            session.updated_at,
            session.parent_id,
            session.root_id,
            session.raw_path,
            session.raw_locator,
            _json(session.metadata),
        ),
    )


def _fts_delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    missing = conn.execute(
        "SELECT COUNT(*) FROM events WHERE session_id = ? AND fts_rowid IS NULL",
        (session_id,),
    ).fetchone()[0]
    if missing:
        try:
            conn.execute("DELETE FROM events_fts WHERE session_id = ?", (session_id,))
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT rowid FROM events_fts WHERE session_id = ?", (session_id,)
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM events_fts WHERE rowid = ?", (row["rowid"],))
        return
    rows = conn.execute(
        "SELECT fts_rowid FROM events WHERE session_id = ? AND fts_rowid IS NOT NULL",
        (session_id,),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM events_fts WHERE rowid = ?", (row["fts_rowid"],))


def _delete_session_rows(conn: sqlite3.Connection, session_id: str) -> None:
    _fts_delete_session(conn, session_id)
    conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM goals WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM attachments WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def replace_session(
    conn: sqlite3.Connection,
    session: Session,
    max_event_chars: int = 60000,
) -> None:
    session.finalize()
    upsert_session(conn, session)

    existing_ids = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM events WHERE session_id = ?", (session.id,)
        )
    }
    new_ids = {event.event_id for event in session.events if event.event_id}

    for old_id in existing_ids - new_ids:
        conn.execute("DELETE FROM embeddings WHERE event_id = ?", (old_id,))
        conn.execute("DELETE FROM events WHERE id = ?", (old_id,))

    _fts_delete_session(conn, session.id)

    for event in session.events:
        if not event.event_id:
            continue
        stored_text = redact_text(event.text or "")
        original_hash = text_hash(event.text or "")
        event_limit = (
            min(max_event_chars, 8000)
            if event.kind in {"tool_call", "tool_result", "file_change"}
            else max_event_chars
        )
        truncated = len(stored_text) > event_limit
        if truncated:
            stored_text = stored_text[:event_limit]
        raw = event.raw.to_dict()
        conn.execute(
            """
            INSERT INTO events (
              id, session_id, agent, seq, timestamp, role, kind, text, text_hash,
              raw_path, raw_locator, raw_line, raw_offset, raw_hash,
              parent_event_id, tool_call_id, subagent_id, metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              timestamp=excluded.timestamp,
              role=excluded.role,
              text=excluded.text,
              text_hash=excluded.text_hash,
              raw_locator=excluded.raw_locator,
              metadata_json=excluded.metadata_json
            """,
            (
                event.event_id,
                session.id,
                session.agent,
                event.seq,
                event.timestamp,
                event.role,
                event.kind,
                stored_text,
                original_hash,
                event.raw.path,
                _json(raw),
                event.raw.line,
                event.raw.offset,
                event.raw.hash,
                event.parent_event_id,
                event.tool_call_id,
                event.subagent_id,
                _json({**event.metadata, **({"truncated": True} if truncated else {})}),
            ),
        )
        if stored_text:
            cursor = conn.execute(
                "INSERT INTO events_fts(event_id, session_id, text) VALUES (?,?,?)",
                (event.event_id, session.id, stored_text),
            )
            conn.execute(
                "UPDATE events SET fts_rowid = ? WHERE id = ?",
                (cursor.lastrowid, event.event_id),
            )

    conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session.id,))
    for call in session.tool_calls:
        conn.execute(
            "INSERT OR REPLACE INTO tool_calls "
            "(id, event_id, session_id, name, call_id, status, input_text, output_text) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                call.id,
                call.event_id,
                session.id,
                call.name,
                call.call_id,
                call.status,
                redact_text(call.input_text or "")[:max_event_chars] if call.input_text else None,
                redact_text(call.output_text or "")[:max_event_chars] if call.output_text else None,
            ),
        )

    conn.execute("DELETE FROM goals WHERE session_id = ?", (session.id,))
    for goal in session.goals:
        conn.execute(
            "INSERT OR REPLACE INTO goals "
            "(id, session_id, objective, status, phase, budget_json, created_at, updated_at, event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                goal.id,
                session.id,
                redact_text(goal.objective or "")[:max_event_chars],
                goal.status,
                goal.phase,
                _json(goal.budget),
                goal.created_at,
                goal.updated_at,
                goal.event_id,
            ),
        )

    conn.execute("DELETE FROM todos WHERE session_id = ?", (session.id,))
    for todo in session.todos:
        conn.execute(
            "INSERT OR REPLACE INTO todos "
            "(id, session_id, content, status, priority, position, blocked_by, event_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                todo.id,
                session.id,
                redact_text(todo.content or "")[:max_event_chars],
                todo.status,
                todo.priority,
                todo.position,
                _json(todo.blocked_by),
                todo.event_id,
            ),
        )

    conn.execute("DELETE FROM attachments WHERE session_id = ?", (session.id,))
    for attachment in session.attachments:
        conn.execute(
            "INSERT OR REPLACE INTO attachments "
            "(id, session_id, event_id, path, mime, size, filename) VALUES (?,?,?,?,?,?,?)",
            (
                attachment.id,
                session.id,
                attachment.event_id,
                attachment.path,
                attachment.mime,
                attachment.size,
                attachment.filename,
            ),
        )


def remove_stale_sessions(
    conn: sqlite3.Connection, agent: str, source_path: str, keep_ids: Iterable[str]
) -> int:
    keep = set(keep_ids)
    rows = conn.execute(
        "SELECT id FROM sessions WHERE agent = ? AND source_path = ?",
        (agent, source_path),
    ).fetchall()
    removed = 0
    for row in rows:
        if row["id"] not in keep:
            _delete_session_rows(conn, row["id"])
            removed += 1
    return removed


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    _delete_session_rows(conn, session_id)


def get_session(conn: sqlite3.Connection, session_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return None
    session = dict(row)
    session["metadata"] = _loads(session.pop("metadata_json", None))
    session["events"] = [
        _row_with_metadata(event)
        for event in conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY seq, id", (session_id,)
        )
    ]
    session["tool_calls"] = [
        dict(call)
        for call in conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id", (session_id,)
        )
    ]
    session["goals"] = [
        {**dict(goal), "budget": _loads(goal["budget_json"])}
        for goal in conn.execute(
            "SELECT * FROM goals WHERE session_id = ? ORDER BY created_at", (session_id,)
        )
    ]
    session["todos"] = [
        {**dict(todo), "blocked_by": _loads(todo["blocked_by"]) or []}
        for todo in conn.execute(
            "SELECT * FROM todos WHERE session_id = ? ORDER BY position, id", (session_id,)
        )
    ]
    session["attachments"] = [
        dict(attachment)
        for attachment in conn.execute(
            "SELECT * FROM attachments WHERE session_id = ? ORDER BY id", (session_id,)
        )
    ]
    return session


def get_event(conn: sqlite3.Connection, event_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_with_metadata(row) if row else None


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _row_with_metadata(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["metadata"] = _loads(value.pop("metadata_json", None))
    value["raw"] = _loads(value.pop("raw_locator", None))
    return value


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT agent, COUNT(*) AS sessions FROM sessions GROUP BY agent ORDER BY agent"
    ).fetchall()
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    embedded = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    return {
        "sessions": sum(row["sessions"] for row in rows),
        "events": events,
        "embeddings": embedded,
        "sources": sources,
        "by_agent": {row["agent"]: row["sessions"] for row in rows},
    }