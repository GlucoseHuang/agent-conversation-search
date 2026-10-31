from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from .config import ensure_app_dir, load_config
from .current import detect_current_session
from .db import connect, get_event, get_session, init_db, stats
from .embeddings import EmbeddingClient
from .handoff import build_handoff, to_markdown
from .indexer import index_all
from .search import search


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _client(config: dict[str, Any], no_embed: bool) -> Optional[EmbeddingClient]:
    if no_embed:
        return None
    embedding = config["embedding"]
    if not embedding["api_key"]:
        return None
    return EmbeddingClient(
        base_url=embedding["base_url"],
        model=embedding["model"],
        api_key=embedding["api_key"],
        batch_size=embedding["batch_size"],
        timeout=embedding["timeout"],
        max_chars=embedding["max_chars"],
    )


def _open(config: dict[str, Any], init: bool = False) -> sqlite3.Connection:
    if init:
        ensure_app_dir()
        conn = connect(config["db_path"])
        init_db(conn)
        return conn
    if not Path(config["db_path"]).exists():
        raise SystemExit(f"index not found: {config['db_path']} (run `acs index`)")
    return connect(config["db_path"])


def cmd_index(args: argparse.Namespace, config: dict[str, Any]) -> int:
    def progress(message: str) -> None:
        print(message, file=sys.stderr)

    result = index_all(
        config=config,
        agents=args.agent,
        full=args.full,
        no_embed=args.no_embed,
        embed_limit=args.embed_limit,
        dry_run=args.dry_run,
        progress=progress,
    )
    print(_json(result))
    return 0


def cmd_search(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    query = args.query or args.keyword
    if not query:
        raise SystemExit("provide --query or --keyword")

    excluded: set[str] = set()
    if args.exclude_session:
        excluded.add(args.exclude_session)
    elif not args.no_exclude_current:
        current = detect_current_session(agent_filter=args.agent, cwd=None)
        if current:
            excluded.add(current.session_id)
            print(
                f"[acs] excluding current session {current.session_id} "
                f"({current.confidence}: {current.source})",
                file=sys.stderr,
            )
        else:
            print(
                "[acs] current session not detected; pass --exclude-session <agent>:<id> "
                "or set ACS_CURRENT_SESSION_ID to avoid echo",
                file=sys.stderr,
            )

    client = None if args.keyword or args.no_embed else _client(config, False)
    results = search(
        conn,
        query,
        client=client,
        limit=args.limit + (2 if excluded else 0),
        agent=args.agent,
        project=args.project,
        cwd=args.cwd,
        date_from=args.date_from,
        date_to=args.date_to,
        role=args.role,
        kind=args.kind,
        tool=args.tool,
        subagent=args.subagent,
        include_events=args.events,
    )
    if excluded:
        results = [
            item
            for item in results
            if item.get("session_id") not in excluded
        ][: args.limit]
    conn.close()
    if args.format == "md":
        print(_search_md(results, args.events))
    else:
        print(_json(results))
    return 0


def cmd_get(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    if args.session:
        value = get_session(conn, args.session)
        if not value:
            raise SystemExit(f"session not found: {args.session}")
    elif args.event:
        value = get_event(conn, args.event)
        if not value:
            raise SystemExit(f"event not found: {args.event}")
        if args.context:
            session = get_session(conn, value["session_id"])
            events = session["events"] if session else []
            index = next(
                (index for index, event in enumerate(events) if event["id"] == value["id"]),
                None,
            )
            if index is not None:
                value = {
                    "event": value,
                    "context": events[max(0, index - args.context) : index + args.context + 1],
                }
    else:
        raise SystemExit("provide --session or --event")
    conn.close()
    print(_json(value) if args.format == "json" else _get_md(value))
    return 0


def cmd_handoff(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    session = get_session(conn, args.session)
    conn.close()
    if not session:
        raise SystemExit(f"session not found: {args.session}")
    package = build_handoff(session, recent=args.recent, max_message_chars=args.max_message_chars)
    print(_json(package) if args.format == "json" else to_markdown(package))
    return 0


def cmd_raw(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    event = get_event(conn, args.event)
    conn.close()
    if not event:
        raise SystemExit(f"event not found: {args.event}")
    print(_read_raw(event["raw"]))
    return 0


def _read_raw(raw: Any) -> str:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if not isinstance(raw, dict):
        return str(raw)
    path = raw.get("path")
    if raw.get("kind") == "sqlite":
        table = str(raw.get("table") or "")
        column = str(raw.get("column") or "")
        pk = raw.get("pk")
        if table not in {"part", "message", "session", "todo", "thread_items"} or not column:
            return _json(raw)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    f"SELECT {column} FROM {table} WHERE id = ?", (pk,)
                ).fetchone()
            except sqlite3.Error:
                row = conn.execute(
                    f"SELECT {column} FROM {table} WHERE rowid = ?", (pk,)
                ).fetchone()
            conn.close()
            return row[column] if row else ""
        except sqlite3.Error as error:
            return f"raw sqlite error: {error}"
    if not path:
        return _json(raw)
    line_number = raw.get("line")
    compressed = raw.get("compressed")
    try:
        if compressed == "zstd" or str(path).endswith((".zst", ".zstd")):
            from .adapters.base import iter_zstd_lines

            for number, line in enumerate(iter_zstd_lines(Path(path)), 1):
                if line_number and number == line_number:
                    return line.decode("utf-8", "replace")
                if not line_number:
                    return line.decode("utf-8", "replace")[:100_000]
            return f"line {line_number} not found"
        with open(path, "rb") as handle:
            for number, line in enumerate(handle, 1):
                if line_number and number == line_number:
                    return line.decode("utf-8", "replace")
                if not line_number:
                    return line.decode("utf-8", "replace")[:100_000]
        return f"line {line_number} not found"
    except OSError as error:
        return f"raw read error: {error}"


def cmd_sources(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT path, agent, kind, size, mtime, last_indexed_at, status, error "
            "FROM sources ORDER BY agent, path"
        )
    ]
    conn.close()
    print(_json(rows))
    return 0


def cmd_stats(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    print(_json(stats(conn)))
    conn.close()
    return 0


def cmd_current(args: argparse.Namespace, config: dict[str, Any]) -> int:
    current = detect_current_session(agent_filter=args.agent, cwd=None)
    if not current:
        print("not detected", file=sys.stderr)
        return 1
    if args.format == "id":
        print(current.session_id)
    else:
        print(_json(current.__dict__))
    return 0


def cmd_install(args: argparse.Namespace, config: dict[str, Any]) -> int:
    from .install import install

    result = install(
        force=args.force,
        replace_old=args.replace_old,
        copy=args.copy,
        target=Path(args.target).expanduser() if args.target else None,
    )
    print(_json(result))
    return 0


def _search_md(results: list[dict[str, Any]], events: bool) -> str:
    if not results:
        return "No matching conversations found."
    lines = [f"Found {len(results)} result(s):", ""]
    for index, item in enumerate(results, 1):
        raw = item.get("raw") or {}
        lines.append(
            f"[{index}] {item.get('agent')} - {item.get('title') or 'untitled'} "
            f"(score {item.get('score', 0):.4f})"
        )
        lines.append(f"    session: {item.get('session_id')}")
        if raw:
            lines.append(f"    raw: {raw.get('path')}:{raw.get('line') or raw.get('pk')}")
        match = item.get("match") or item
        if match:
            lines.append(f"    {match.get('role')}/{match.get('kind')}: {(match.get('snippet') or '')[:300]}")
        lines.append("")
    return "\n".join(lines)


def _get_md(value: dict[str, Any]) -> str:
    if "events" in value:
        lines = [
            f"# {value.get('title') or value.get('id')}",
            f"agent: {value.get('agent')}  cwd: {value.get('cwd')}",
            f"raw: {value.get('raw_locator')}",
            "",
        ]
        for event in value["events"]:
            lines.append(f"[{event.get('seq')}] {event.get('role')}/{event.get('kind')}: {(event.get('text') or '')[:500]}")
            lines.append(f"raw: {event.get('raw')}")
        return "\n".join(lines)
    return _json(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acs", description="Agent conversation search")
    parser.add_argument("--db", help="index database path")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="discover and index conversations")
    index.add_argument("--full", action="store_true")
    index.add_argument("--no-embed", action="store_true")
    index.add_argument("--embed-limit", type=int)
    index.add_argument("--dry-run", action="store_true")
    index.add_argument("--agent", action="append")
    index.set_defaults(func=cmd_index)

    search_parser = sub.add_parser("search", help="search conversations")
    search_parser.add_argument("--query")
    search_parser.add_argument("--keyword")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--agent")
    search_parser.add_argument("--project")
    search_parser.add_argument("--cwd")
    search_parser.add_argument("--date-from")
    search_parser.add_argument("--date-to")
    search_parser.add_argument("--role")
    search_parser.add_argument("--kind")
    search_parser.add_argument("--tool")
    search_parser.add_argument("--subagent", action="store_true")
    search_parser.add_argument("--events", action="store_true")
    search_parser.add_argument(
        "--exclude-session",
        help="your current session id (agent:native_id); explicit exclusion wins",
    )
    search_parser.add_argument(
        "--no-exclude-current",
        action="store_true",
        help="do not auto-exclude the current session",
    )
    search_parser.add_argument("--no-embed", action="store_true")
    search_parser.add_argument("--format", choices=["json", "md"], default="json")
    search_parser.set_defaults(func=cmd_search)

    get = sub.add_parser("get", help="read a session or event")
    get.add_argument("--session")
    get.add_argument("--event")
    get.add_argument("--context", type=int, default=0)
    get.add_argument("--format", choices=["json", "md"], default="json")
    get.set_defaults(func=cmd_get)

    handoff = sub.add_parser("handoff", help="build a structured handoff package")
    handoff.add_argument("--session", required=True)
    handoff.add_argument("--recent", type=int, default=12)
    handoff.add_argument("--max-message-chars", type=int, default=4000)
    handoff.add_argument("--format", choices=["json", "md"], default="json")
    handoff.set_defaults(func=cmd_handoff)

    raw = sub.add_parser("raw", help="print the raw source for an event")
    raw.add_argument("--event", required=True)
    raw.set_defaults(func=cmd_raw)

    current = sub.add_parser("current", help="show the detected current session")
    current.add_argument("--agent")
    current.add_argument("--format", choices=["json", "id"], default="json")
    current.set_defaults(func=cmd_current)

    sources = sub.add_parser("sources", help="list indexed sources")
    sources.set_defaults(func=cmd_sources)

    stats_parser = sub.add_parser("stats", help="show index statistics")
    stats_parser.set_defaults(func=cmd_stats)

    install = sub.add_parser("install", help="install the skill into ~/.agents and symlink agents")
    install.add_argument("--force", action="store_true")
    install.add_argument("--replace-old", action="store_true")
    install.add_argument("--copy", action="store_true")
    install.add_argument("--target")
    install.set_defaults(func=cmd_install)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config()
    if args.db:
        config["db_path"] = Path(args.db).expanduser()
    return args.func(args, config)