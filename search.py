#!/usr/bin/env python3
"""
conversation-search - Semantic search

Performs vector cosine-similarity search over an embedded conversation
database produced by embed.py. Supports session aggregation and date filters.

Usage:
    python3 search.py --query "TTS voice synthesis" --limit 5
    python3 search.py --query "thesis" --date-from 2026-04-01 --date-to 2026-04-27
    python3 search.py --keyword "Lo-Fi" --limit 10
    python3 search.py --query "music" --exclude-session <session_id> --format json
"""

import json
import os
import sys
import sqlite3
import argparse
import urllib.request
import numpy as np
from typing import List, Dict, Optional

# =============================================
# Configuration
# =============================================

DB_PATH = os.environ.get(
    "CONVERSATION_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.db"),
)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-v4")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))

DEFAULT_LIMIT = 5
MAX_LIMIT = 20


# =============================================
# Query embedding
# =============================================

def embed_query(query: str) -> List[float]:
    if not DASHSCOPE_API_KEY:
        raise ValueError("DASHSCOPE_API_KEY environment variable is not set")

    url = f"{DASHSCOPE_BASE_URL}/embeddings"
    data = json.dumps({
        "model": EMBEDDING_MODEL,
        "input": query,
        "dimensions": EMBEDDING_DIMENSIONS,
        "encoding_format": "float",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    })

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if "error" in result:
        raise Exception(f"API error: {result['error']}")

    return result["data"][0]["embedding"]


# =============================================
# Search
# =============================================

def search_semantic(query: str, limit: int = DEFAULT_LIMIT,
                    date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    exclude_session: Optional[str] = None) -> List[Dict]:
    if not os.path.exists(DB_PATH):
        print(f"[error] database not found: {DB_PATH}\n"
              f"        run embed.py first to build the index", file=sys.stderr)
        return []

    query_vec = np.array(embed_query(query), dtype=np.float32)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    conditions = ["m.embedding IS NOT NULL"]
    params: list = []

    if date_from:
        conditions.append("m.timestamp >= ?")
        params.append(date_from)

    if date_to:
        conditions.append("m.timestamp <= ?")
        params.append(date_to + " 23:59:59" if len(date_to) == 10 else date_to)

    if exclude_session:
        conditions.append("m.session_id != ?")
        params.append(exclude_session)

    where_clause = " AND ".join(conditions)

    c.execute(f"""
        SELECT m.id, m.session_id, m.msg_id, m.role, m.content, m.content_short,
               m.timestamp, m.msg_index, m.embedding, s.date as session_date, s.title
        FROM messages m
        LEFT JOIN sessions s ON m.session_id = s.session_id
        WHERE {where_clause}
    """, params)

    rows = c.fetchall()
    conn.close()

    if not rows:
        return []

    results = []
    for row in rows:
        emb_blob = row["embedding"]
        if not emb_blob:
            continue

        msg_vec = np.frombuffer(emb_blob, dtype=np.float32)
        if len(msg_vec) != len(query_vec):
            continue

        norm_q = np.linalg.norm(query_vec)
        norm_m = np.linalg.norm(msg_vec)
        if norm_q == 0 or norm_m == 0:
            continue
        cos_sim = float(np.dot(query_vec, msg_vec) / (norm_q * norm_m))

        results.append({
            "id": row["id"],
            "session_id": row["session_id"],
            "msg_id": row["msg_id"],
            "role": row["role"],
            "content": row["content"],
            "content_short": row["content_short"],
            "timestamp": row["timestamp"],
            "msg_index": row["msg_index"],
            "session_date": row["session_date"],
            "session_title": row["title"],
            "score": cos_sim,
        })

    results.sort(key=lambda x: x["score"], reverse=True)

    # Deduplicate by session, keeping the highest-scoring message per session
    seen_sessions = set()
    unique = []
    for r in results:
        if r["session_id"] in seen_sessions:
            continue
        seen_sessions.add(r["session_id"])
        unique.append(r)
        if len(unique) >= limit:
            break

    return unique


def search_keyword(keyword: str, limit: int = DEFAULT_LIMIT,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None,
                   exclude_session: Optional[str] = None) -> List[Dict]:
    if not os.path.exists(DB_PATH):
        print(f"[error] database not found: {DB_PATH}\n"
              f"        run embed.py first to build the index", file=sys.stderr)
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    conditions = ["m.content LIKE ?"]
    params: list = [f"%{keyword}%"]

    if date_from:
        conditions.append("m.timestamp >= ?")
        params.append(date_from)

    if date_to:
        conditions.append("m.timestamp <= ?")
        params.append(date_to + " 23:59:59" if len(date_to) == 10 else date_to)

    if exclude_session:
        conditions.append("m.session_id != ?")
        params.append(exclude_session)

    where_clause = " AND ".join(conditions)

    c.execute(f"""
        SELECT m.id, m.session_id, m.msg_id, m.role, m.content, m.content_short,
               m.timestamp, m.msg_index, s.date as session_date, s.title
        FROM messages m
        LEFT JOIN sessions s ON m.session_id = s.session_id
        WHERE {where_clause}
        ORDER BY m.timestamp DESC
        LIMIT {limit * 3}
    """, params)

    rows = c.fetchall()
    conn.close()

    seen_sessions = set()
    unique = []
    for row in rows:
        if row["session_id"] in seen_sessions:
            continue
        seen_sessions.add(row["session_id"])
        unique.append({
            "id": row["id"],
            "session_id": row["session_id"],
            "msg_id": row["msg_id"],
            "role": row["role"],
            "content": row["content"],
            "content_short": row["content_short"],
            "timestamp": row["timestamp"],
            "msg_index": row["msg_index"],
            "session_date": row["session_date"],
            "session_title": row["title"],
            "score": 1.0,
        })
        if len(unique) >= limit:
            break

    return unique


def get_session_messages(conn: sqlite3.Connection, session_id: str) -> List[Dict]:
    c = conn.cursor()
    c.execute("""
        SELECT id, session_id, msg_id, role, content, content_short, timestamp, msg_index
        FROM messages
        WHERE session_id = ?
        ORDER BY msg_index ASC
    """, (session_id,))
    rows = c.fetchall()
    return [
        {
            "id": row[0],
            "session_id": row[1],
            "msg_id": row[2],
            "role": row[3],
            "content": row[4],
            "content_short": row[5],
            "timestamp": row[6],
            "msg_index": row[7],
        }
        for row in rows
    ]


# =============================================
# Output formatting
# =============================================

ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
}


def _short(content: str, content_short: str, max_len: int = 200) -> str:
    if len(content) > max_len:
        return content_short
    return content.replace("\n", " ").strip()


def format_results(results: List[Dict], show_full_session: bool = True) -> str:
    if not results:
        return "No matching conversations found."

    lines: List[str] = [f"Found {len(results)} matching conversation(s):", ""]

    conn = sqlite3.connect(DB_PATH)

    try:
        for idx, r in enumerate(results):
            session_date = (r.get("session_date") or r.get("timestamp") or "")[:10]
            session_title = r.get("session_title") or ""
            title_str = f" - {session_title}" if session_title else ""

            lines.append("=" * 60)
            lines.append(f"[{idx+1}] {session_date}{title_str}  (score: {r['score']:.3f})")
            lines.append("")

            if show_full_session:
                session_msgs = get_session_messages(conn, r["session_id"])
                if session_msgs:
                    for msg in session_msgs:
                        is_match = msg["id"] == r["id"]
                        prefix = ">>" if is_match else "  "
                        role = ROLE_LABELS.get(msg["role"], msg["role"])
                        text = _short(msg["content"], msg["content_short"])
                        lines.append(f"{prefix} [{role}] {text}")
                    lines.append("")
                else:
                    role = ROLE_LABELS.get(r["role"], r["role"])
                    text = _short(r["content"], r["content_short"])
                    lines.append(f">> [{role}] {text}")
                    lines.append("")
            else:
                role = ROLE_LABELS.get(r["role"], r["role"])
                text = _short(r["content"], r["content_short"])
                lines.append(f">> [{role}] {text}")
                lines.append("")
    finally:
        conn.close()

    return "\n".join(lines)


def format_results_json(results: List[Dict]) -> str:
    return json.dumps(results, ensure_ascii=False, indent=2)


# =============================================
# CLI
# =============================================

def main():
    parser = argparse.ArgumentParser(description="conversation-search semantic search")
    parser.add_argument("--query", help="semantic query (vector search)")
    parser.add_argument("--keyword", help="keyword search (text match fallback)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"max results per page (default {DEFAULT_LIMIT}, max {MAX_LIMIT})")
    parser.add_argument("--date-from", help="start date YYYY-MM-DD")
    parser.add_argument("--date-to", help="end date YYYY-MM-DD")
    parser.add_argument("--exclude-session", help="exclude a session id from results")
    parser.add_argument("--no-full-session", action="store_true",
                        help="show only matched messages, not the full session")
    parser.add_argument("--format", choices=["json", "readable"], default="readable",
                        help="output format")
    args = parser.parse_args()

    if not args.query and not args.keyword:
        print("[error] provide --query (semantic) or --keyword (text)", file=sys.stderr)
        sys.exit(1)

    limit = min(args.limit, MAX_LIMIT)
    show_full = not args.no_full_session

    if args.query:
        results = search_semantic(
            query=args.query,
            limit=limit,
            date_from=args.date_from,
            date_to=args.date_to,
            exclude_session=args.exclude_session,
        )
    else:
        results = search_keyword(
            keyword=args.keyword,
            limit=limit,
            date_from=args.date_from,
            date_to=args.date_to,
            exclude_session=args.exclude_session,
        )

    if args.format == "json":
        print(format_results_json(results))
    else:
        print(format_results(results, show_full_session=show_full))


if __name__ == "__main__":
    main()
