from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from .compact import compact
from .config import ensure_app_dir, load_config
from .current import detect_current_session
from .db import connect, get_event, get_session, init_db, stats
from .embeddings import EmbeddingClient, EmbeddingUnavailable, embed_pending
from .handoff import build_handoff, to_markdown
from .indexer import index_all
from .ondemand import ensure_session
from .resolve import SessionResolutionError, resolve_session
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


def _resolve_session_id(conn: sqlite3.Connection, value: str) -> str:
    try:
        return resolve_session(conn, value)["id"]
    except SessionResolutionError:
        return value


def _non_negative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


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

    embed = bool(args.embed or args.embed_all)
    embed_limit = (
        None
        if args.embed_all
        else (
            args.embed_limit
            if args.embed_limit is not None
            else (500 if embed else None)
        )
    )
    result = index_all(
        config=config,
        agents=args.agent,
        full=args.full,
        no_embed=args.no_embed or not embed,
        embed_limit=embed_limit,
        dry_run=args.dry_run,
        progress=progress,
        active_grace_seconds=args.active_grace,
        include_active=args.include_active,
    )
    print(_json(result))
    return 0


def cmd_embed(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    client = _client(config, False)
    if not client:
        raise SystemExit("embedding key not configured; set ACS_EMBEDDING_API_KEY or Keychain")
    limit = None if args.all else args.limit

    def progress(done: int, total: int) -> None:
        print(f"[acs] embedded {done}/{total}", file=sys.stderr)

    try:
        result = embed_pending(
            conn,
            client,
            config["index"]["embed_kinds"],
            limit=limit,
            batch_size=config["embedding"]["batch_size"],
            progress=progress,
            session_id=args.session,
            agent=args.agent,
            min_chars=args.min_chars,
        )
    except EmbeddingUnavailable as error:
        raise SystemExit(str(error)) from error
    print(_json(result))
    conn.close()
    return 0


def cmd_search(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    query = args.query or args.keyword
    if not query:
        raise SystemExit("provide --query or --keyword")

    excluded: set[str] = set()
    if args.exclude_session:
        excluded.add(_resolve_session_id(conn, args.exclude_session))
    elif not args.no_exclude_current:
        current = detect_current_session(agent_filter=args.agent, cwd=None)
        if current:
            current.session_id = _resolve_session_id(conn, current.session_id)
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
        limit=args.limit,
        agent=args.agent,
        project=args.project,
        cwd=args.cwd,
        date_from=args.date_from,
        date_to=args.date_to,
        role=args.role,
        kind=args.kind,
        tool=args.tool,
        subagent=args.subagent,
        exclude_sessions=excluded,
        include_events=args.events,
        fts_limit=max(200, min(2000, args.limit * 4)),
    )
    conn.close()
    if args.format == "md":
        print(_search_md(results, args.events))
    else:
        print(_json(results))
    return 0


def cmd_get(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    if args.session:
        try:
            session_id = ensure_session(conn, config, args.session)["id"]
        except SessionResolutionError as error:
            raise SystemExit(str(error)) from error
        value = get_session(conn, session_id)
        if not value:
            raise SystemExit(f"session not found: {session_id}")
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
    try:
        session_id = ensure_session(conn, config, args.session)["id"]
    except SessionResolutionError as error:
        conn.close()
        raise SystemExit(str(error)) from error
    session = get_session(conn, session_id)
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
    print(_read_raw(event["raw"]), end="")
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


def cmd_compact(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)

    def progress(message: str) -> None:
        print(f"[acs] {message}", file=sys.stderr)

    result = compact(
        conn,
        vacuum=not args.no_vacuum,
        progress=progress,
        caps=config["index"]["event_caps"],
        backup=not args.no_backup,
    )
    conn.close()
    print(_json(result))
    return 0


def cmd_current(args: argparse.Namespace, config: dict[str, Any]) -> int:
    current = detect_current_session(agent_filter=args.agent, cwd=None)
    if not current:
        print("not detected", file=sys.stderr)
        return 1
    try:
        conn = _open(config)
        current.session_id = _resolve_session_id(conn, current.session_id)
        conn.close()
    except SystemExit:
        pass
    if args.format == "id":
        print(current.session_id)
    else:
        print(_json(current.__dict__))
    return 0


def cmd_resolve(args: argparse.Namespace, config: dict[str, Any]) -> int:
    conn = _open(config)
    try:
        session = ensure_session(conn, config, args.input)
    except SessionResolutionError as error:
        conn.close()
        if error.candidates:
            print(_json({"error": str(error), "candidates": error.candidates}))
        else:
            print(str(error), file=sys.stderr)
        return 1
    conn.close()
    if args.format == "id":
        print(session["id"])
    else:
        print(_json(session))
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
        match = item.get("match") or item
        raw = (match.get("raw") if isinstance(match, dict) else None) or item.get("raw") or {}
        lines.append(
            f"[{index}] {item.get('agent')} - {item.get('title') or 'untitled'} "
            f"(score {item.get('score', 0):.4f})"
        )
        lines.append(f"    session: {item.get('session_id')}")
        if raw:
            lines.append(f"    raw: {raw.get('path')}:{raw.get('line') or raw.get('pk')}")
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
    index.add_argument("--embed", action="store_true", help="embed a bounded batch after indexing")
    index.add_argument("--embed-all", action="store_true", help="embed every pending event (can be very large)")
    index.add_argument("--no-embed", action="store_true")
    index.add_argument("--embed-limit", type=int)
    index.add_argument("--dry-run", action="store_true")
    index.add_argument(
        "--include-active",
        action="store_true",
        help="also index sources modified within the active grace window",
    )
    index.add_argument(
        "--active-grace",
        type=int,
        help="seconds to treat a recently written source as active (default 0: index active sources)",
    )
    index.add_argument("--agent", action="append")
    index.set_defaults(func=cmd_index)

    search_parser = sub.add_parser("search", help="search conversations")
    search_parser.add_argument("--query")
    search_parser.add_argument("--keyword")
    search_parser.add_argument("--limit", type=_non_negative, default=10)
    search_parser.add_argument("--agent")
    search_parser.add_argument("--project")
    search_parser.add_argument("--cwd")
    search_parser.add_argument("--date-from")
    search_parser.add_argument("--date-to")
    search_parser.add_argument("--role")
    search_parser.add_argument("--kind")
    search_parser.add_argument("--tool", help="filter tool_call events by tool name")
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
    get.add_argument("--session", help="session id, native id, or deep link")
    get.add_argument("--event")
    get.add_argument("--context", type=int, default=0)
    get.add_argument("--format", choices=["json", "md"], default="json")
    get.set_defaults(func=cmd_get)

    handoff = sub.add_parser("handoff", help="build a structured handoff package")
    handoff.add_argument("--session", required=True, help="session id, native id, or deep link")
    handoff.add_argument("--recent", type=int, default=12)
    handoff.add_argument("--max-message-chars", type=int, default=4000)
    handoff.add_argument("--format", choices=["json", "md"], default="json")
    handoff.set_defaults(func=cmd_handoff)

    resolve = sub.add_parser("resolve", help="resolve a session id or deep link")
    resolve.add_argument("input")
    resolve.add_argument("--format", choices=["json", "id"], default="json")
    resolve.set_defaults(func=cmd_resolve)

    raw = sub.add_parser("raw", help="print the raw source for an event")
    raw.add_argument("--event", required=True)
    raw.set_defaults(func=cmd_raw)

    embed = sub.add_parser("embed", help="embed pending events (bounded by default)")
    embed.add_argument("--limit", type=_non_negative, default=500)
    embed.add_argument("--all", action="store_true")
    embed.add_argument("--session")
    embed.add_argument("--agent")
    embed.add_argument("--min-chars", type=int, default=20)
    embed.set_defaults(func=cmd_embed)

    compact_parser = sub.add_parser("compact", help="compact an existing index in place")
    compact_parser.add_argument("--no-vacuum", action="store_true")
    compact_parser.add_argument("--no-backup", action="store_true")
    compact_parser.set_defaults(func=cmd_compact)

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