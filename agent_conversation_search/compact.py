from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .db import DEFAULT_EVENT_CAPS, FTS_TRIGGERS


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def compact(
    conn: sqlite3.Connection,
    vacuum: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    before = db_path.stat().st_size if db_path.exists() else 0

    def note(message: str) -> None:
        if progress:
            progress(message)

    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for trigger in [
        "events_fts_ai",
        "events_fts_ad",
        "events_fts_au_del",
        "events_fts_au_ins",
    ]:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS events_fts")
    conn.commit()

    columns = _table_columns(conn, "tool_calls")
    if "input_text" in columns or "output_text" in columns:
        note("rebuilding tool_calls without duplicate text")
        conn.execute(
            "CREATE TABLE tool_calls_new ("
            "id TEXT PRIMARY KEY, event_id TEXT, session_id TEXT, "
            "name TEXT, call_id TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO tool_calls_new(id, event_id, session_id, name, call_id, status) "
            "SELECT id, event_id, session_id, name, call_id, status FROM tool_calls"
        )
        conn.execute("DROP TABLE tool_calls")
        conn.execute("ALTER TABLE tool_calls_new RENAME TO tool_calls")
        conn.commit()

    note("truncating event text by kind")
    for kind, limit in DEFAULT_EVENT_CAPS.items():
        conn.execute(
            "UPDATE events SET text = substr(text, 1, ?) "
            "WHERE kind = ? AND text IS NOT NULL AND length(text) > ?",
            (limit, kind, limit),
        )
    conn.commit()

    columns = _table_columns(conn, "events")
    if "fts_rowid" in columns:
        try:
            conn.execute("ALTER TABLE events DROP COLUMN fts_rowid")
        except sqlite3.OperationalError:
            pass
    conn.commit()

    if vacuum:
        note("vacuuming (can take a few minutes)")
        conn.execute("VACUUM")
        conn.commit()

    note("building external-content FTS")
    conn.execute(
        "CREATE VIRTUAL TABLE events_fts USING fts5("
        "text, content='events', content_rowid='rowid', tokenize='trigram')"
    )
    conn.execute(
        "INSERT INTO events_fts(rowid, text) "
        "SELECT rowid, text FROM events "
        "WHERE kind IN ('message','reasoning','tool_call','question') "
        "AND text IS NOT NULL AND text != ''"
    )
    for statement in FTS_TRIGGERS:
        conn.execute(statement)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA optimize")

    after = db_path.stat().st_size if db_path.exists() else 0
    return {
        "db": str(db_path),
        "before_bytes": before,
        "after_bytes": after,
        "saved_bytes": max(0, before - after),
        "before_gib": round(before / 1024**3, 2),
        "after_gib": round(after / 1024**3, 2),
        "vacuumed": vacuum,
        "finished_at": datetime.now().isoformat(),
    }


def main() -> int:
    import argparse
    import json

    from .config import load_config
    from .db import connect

    parser = argparse.ArgumentParser(description="Compact an existing index")
    parser.add_argument("--no-vacuum", action="store_true")
    parser.add_argument("--db")
    args = parser.parse_args()
    config = load_config()
    path = Path(args.db).expanduser() if args.db else config["db_path"]
    conn = connect(path)

    def progress(message: str) -> None:
        print(message, flush=True)

    print(json.dumps(compact(conn, vacuum=not args.no_vacuum, progress=progress), ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())