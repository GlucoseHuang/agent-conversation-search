from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Optional

from .embeddings import EmbeddingClient, EmbeddingUnavailable, cosine, unpack_vector


def _filters(
    agent: Optional[str] = None,
    project: Optional[str] = None,
    cwd: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    role: Optional[str] = None,
    kind: Optional[str] = None,
    tool: Optional[str] = None,
    subagent: bool = False,
    exclude_sessions: Optional[set[str]] = None,
) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        conditions.append("e.agent = ?")
        params.append(agent)
    if project:
        conditions.append("s.project LIKE ?")
        params.append(f"%{project}%")
    if cwd:
        conditions.append("s.cwd LIKE ?")
        params.append(f"%{cwd}%")
    if date_from:
        conditions.append("e.timestamp >= ?")
        params.append(date_from if len(date_from) > 10 else f"{date_from}T00:00:00")
    if date_to:
        conditions.append("e.timestamp <= ?")
        params.append(date_to if len(date_to) > 10 else f"{date_to}T23:59:59")
    if role:
        conditions.append("e.role = ?")
        params.append(role)
    if kind:
        conditions.append("e.kind = ?")
        params.append(kind)
    if tool:
        conditions.append(
            "e.kind = 'tool_call' AND EXISTS ("
            "SELECT 1 FROM tool_calls tc WHERE tc.session_id = e.session_id "
            "AND tc.call_id = e.tool_call_id AND tc.name LIKE ?)"
        )
        params.append(f"%{tool}%")
    if subagent:
        conditions.append("(e.subagent_id IS NOT NULL OR s.parent_id IS NOT NULL)")
    if exclude_sessions:
        placeholders = ",".join("?" for _ in exclude_sessions)
        conditions.append(f"e.session_id NOT IN ({placeholders})")
        params.extend(sorted(exclude_sessions))
    return conditions, params


def _fts_match(conn: sqlite3.Connection, query: str, limit: int, filters: list[str], params: list[Any]) -> list[dict[str, Any]]:
    quoted = '"' + query.replace('"', '""') + '"'
    where = " AND ".join(["events_fts MATCH ?", *filters])
    sql = f"""
        SELECT e.*, bm25(events_fts) AS rank
        FROM events_fts
        JOIN events e ON e.rowid = events_fts.rowid
        JOIN sessions s ON s.id = e.session_id
        WHERE {where}
        ORDER BY rank
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, [quoted, *params, limit]).fetchall()
    except sqlite3.OperationalError as error:
        print(f"[acs] FTS error, falling back to LIKE: {error}", file=sys.stderr)
        rows = []
    if rows:
        return [dict(row) for row in rows]
    return _like_match(conn, query, limit, filters, params)


def _like_match(conn: sqlite3.Connection, query: str, limit: int, filters: list[str], params: list[Any]) -> list[dict[str, Any]]:
    where = " AND ".join(
        [
            "e.text LIKE ?",
            "e.kind IN ('message','reasoning','tool_call','question')",
            "e.role NOT IN ('tool','toolResult','tool_result')",
            *filters,
        ]
    )
    sql = f"""
        SELECT e.*
        FROM events e
        JOIN sessions s ON s.id = e.session_id
        WHERE {where}
        ORDER BY e.timestamp DESC
        LIMIT ?
    """
    rows = conn.execute(sql, [f"%{query}%", *params, limit]).fetchall()
    return [dict(row) for row in rows]


def _vector_matches(
    conn: sqlite3.Connection,
    client: EmbeddingClient,
    query: str,
    limit: int,
    filters: list[str],
    params: list[Any],
) -> list[dict[str, Any]]:
    if not client.available:
        return []
    try:
        query_vector = client.embed_query(query)
    except EmbeddingUnavailable:
        return []
    where = " AND ".join(
        ["m.vector IS NOT NULL", "m.text_hash = e.text_hash", *filters]
    )
    sql = f"""
        SELECT e.*, m.vector AS vector, m.dim AS dim
        FROM embeddings m
        JOIN events e ON e.id = m.event_id
        JOIN sessions s ON s.id = e.session_id
        WHERE {where}
    """
    rows = conn.execute(sql, params).fetchall()
    scored: list[dict[str, Any]] = []
    for row in rows:
        vector = unpack_vector(row["vector"])
        score = cosine(query_vector, vector)
        item = dict(row)
        item.pop("vector", None)
        item["vector_score"] = score
        scored.append(item)
    scored.sort(key=lambda item: item["vector_score"], reverse=True)
    return scored[:limit]


def search(
    conn: sqlite3.Connection,
    query: str,
    client: Optional[EmbeddingClient] = None,
    limit: int = 10,
    agent: Optional[str] = None,
    project: Optional[str] = None,
    cwd: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    role: Optional[str] = None,
    kind: Optional[str] = None,
    tool: Optional[str] = None,
    subagent: bool = False,
    exclude_sessions: Optional[set[str]] = None,
    include_events: bool = False,
    fts_limit: int = 200,
) -> list[dict[str, Any]]:
    filters, params = _filters(
        agent=agent,
        project=project,
        cwd=cwd,
        date_from=date_from,
        date_to=date_to,
        role=role,
        kind=kind,
        tool=tool,
        subagent=subagent,
        exclude_sessions=exclude_sessions,
    )
    fts_rows = _fts_match(conn, query, fts_limit, filters, params)
    vector_rows = (
        _vector_matches(conn, client, query, fts_limit, filters, params) if client else []
    )

    combined: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(fts_rows):
        item = combined.setdefault(row["id"], {**row, "fts_rank": rank, "vector_rank": None})
        item["fts_rank"] = rank
    for rank, row in enumerate(vector_rows):
        item = combined.setdefault(row["id"], {**row, "fts_rank": None, "vector_rank": rank})
        item["vector_rank"] = rank
        item["vector_score"] = row.get("vector_score")

    for item in combined.values():
        score = 0.0
        if item.get("fts_rank") is not None:
            score += 1.0 / (60 + item["fts_rank"])
        if item.get("vector_rank") is not None:
            score += 1.0 / (60 + item["vector_rank"])
        item["score"] = score

    ranked = sorted(combined.values(), key=lambda item: item["score"], reverse=True)

    if include_events:
        return [_event_result(conn, item) for item in ranked[:limit]]

    sessions: dict[str, dict[str, Any]] = {}
    for item in ranked:
        session_id = item["session_id"]
        if session_id not in sessions:
            session = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not session:
                continue
            sessions[session_id] = {
                "session_id": session_id,
                "agent": session["agent"],
                "title": session["title"],
                "project": session["project"],
                "cwd": session["cwd"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "parent_id": session["parent_id"],
                "raw": _loads(session["raw_locator"]),
                "score": 0.0,
                "matches": 0,
                "match": None,
            }
        entry = sessions[session_id]
        previous_score = entry["score"]
        entry["score"] = max(previous_score, item["score"])
        entry["matches"] += 1
        if entry["match"] is None or item["score"] > previous_score:
            entry["match"] = _event_result(conn, item)

    result = sorted(sessions.values(), key=lambda item: item["score"], reverse=True)
    return result[:limit]


def _event_result(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    text = item.get("text") or ""
    return {
        "event_id": item.get("id"),
        "session_id": item.get("session_id"),
        "agent": item.get("agent"),
        "timestamp": item.get("timestamp"),
        "role": item.get("role"),
        "kind": item.get("kind"),
        "snippet": text[:500],
        "score": item.get("score"),
        "fts_rank": item.get("fts_rank"),
        "vector_rank": item.get("vector_rank"),
        "vector_score": item.get("vector_score"),
        "raw": _loads(item.get("raw_locator")),
    }


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def keyword_search(
    conn: sqlite3.Connection,
    keyword: str,
    limit: int = 10,
    **filters: Any,
) -> list[dict[str, Any]]:
    filters.pop("include_events", None)
    fts_rows = _like_match(conn, keyword, limit, *_filters(**filters))
    return [_event_result(conn, dict(row)) for row in fts_rows]