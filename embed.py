#!/usr/bin/env python3
"""
conversation-search - Embedding engine

Reads opencode/Hanako session JSONL files, stores them in a local SQLite
database, and generates vector embeddings via the Alibaba Cloud DashScope
text-embedding-v4 API.

Usage:
    python3 embed.py                # incremental update
    python3 embed.py --full         # full rebuild
    python3 embed.py --dry-run      # preview only
"""

import json
import os
import re
import sys
import glob
import time
import sqlite3
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# =============================================
# Configuration (override via environment variables)
# =============================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Session directory: where the agent stores JSONL session files.
# Override with CONVERSATION_SESSIONS_DIR.
SESSIONS_DIR = os.environ.get(
    "CONVERSATION_SESSIONS_DIR",
    os.path.expanduser("~/.hanako/agents/hanako/sessions"),
)

# SQLite database path.
DB_PATH = os.environ.get(
    "CONVERSATION_DB_PATH",
    os.path.join(SCRIPT_DIR, "conversations.db"),
)

# Alibaba Cloud DashScope (OpenAI-compatible) embedding config.
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-v4")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))
EMBEDDING_BATCH_SIZE = 10      # API max per request
EMBEDDING_MAX_TOKENS = 8000    # per-input token limit
EMBEDDING_MAX_CHARS = 12000    # soft character limit per input

# Request control
REQUEST_INTERVAL = 0.5         # seconds between API calls
MAX_RETRIES = 3

# Files inside the sessions directory that are not session logs
NON_SESSION_FILES = {"session-meta.json", "session-titles.json"}


# =============================================
# Helpers
# =============================================

def strip_internal_tags(text: str) -> str:
    """Strip internal reasoning/mood tags from message text."""
    text = re.sub(r'<mood>.*?</mood>', '', text, flags=re.DOTALL)
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    text = re.sub(r'^\s*\[思考\].*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[思考\]\s*', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_text_content(content) -> str:
    """Extract plain text from a message's `content` field."""
    if isinstance(content, str):
        return strip_internal_tags(content)
    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = strip_internal_tags(item.get("text", ""))
                if text:
                    texts.append(text)
            elif item.get("type") == "toolCall":
                name = item.get("name", "unknown")
                texts.append(f"[tool_call: {name}]")
            elif item.get("type") == "toolResult":
                result = str(item.get("content", ""))[:100]
                texts.append(f"[tool_result: {result}]")
        return " ".join(texts)
    return str(content)


def smart_truncate(text: str, max_chars: int = EMBEDDING_MAX_CHARS) -> str:
    """Truncate at a natural boundary when text exceeds the limit."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    for sep in ['。', '！', '？', '\n', '，', '、', ' ', '.', '!', '?']:
        last_pos = truncated.rfind(sep)
        if last_pos > max_chars * 0.6:
            return text[:last_pos + 1]
    return truncated + "..."


# =============================================
# Database
# =============================================

def init_db(conn: sqlite3.Connection):
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id    TEXT PRIMARY KEY,
            filename      TEXT NOT NULL,
            date          TEXT,
            title         TEXT,
            message_count INTEGER DEFAULT 0,
            file_mtime    REAL DEFAULT 0,
            embedded_at   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL,
            msg_id        TEXT,
            role          TEXT NOT NULL,
            content       TEXT NOT NULL,
            content_short TEXT,
            timestamp     TEXT,
            msg_index     INTEGER DEFAULT 0,
            embedding     BLOB,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_session    ON messages(session_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_role       ON messages(role)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)")
    conn.commit()


def get_existing_sessions(conn: sqlite3.Connection) -> Dict[str, float]:
    """Map filename -> file_mtime for sessions already in the database."""
    c = conn.cursor()
    c.execute("SELECT filename, file_mtime FROM sessions")
    return dict(c.fetchall())


def get_existing_msg_ids(conn: sqlite3.Connection, session_id: str) -> set:
    c = conn.cursor()
    c.execute(
        "SELECT msg_id FROM messages WHERE session_id = ? AND msg_id IS NOT NULL AND msg_id != ''",
        (session_id,),
    )
    return {row[0] for row in c.fetchall()}


# =============================================
# Session file parsing
# =============================================

def load_session(filepath: str) -> Tuple[str, str, List[Dict]]:
    """Load a session JSONL file, return (session_id, date, messages)."""
    messages: List[Dict] = []
    session_id = ""
    date_str = ""

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Session header line
                if record.get("type") == "session":
                    session_id = record.get("id", "")
                    ts = record.get("timestamp", "")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            date_str = dt.strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            date_str = ts[:16]
                    continue

                # Skip non-message records
                if record.get("type") != "message":
                    continue

                msg_data = record.get("message", {})
                role = msg_data.get("role", "")
                if role not in ("user", "assistant"):
                    continue

                content = extract_text_content(msg_data.get("content", ""))
                if not content.strip():
                    continue

                content = smart_truncate(content)

                msg_id = record.get("id", "")

                ts = record.get("timestamp", "")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        ts_str = ts[:19] if len(ts) >= 19 else ts
                else:
                    ts_str = ""

                content_short = content[:200].replace("\n", " ")
                if len(content) > 200:
                    content_short += "..."

                messages.append({
                    "msg_id": msg_id,
                    "role": role,
                    "content": content,
                    "content_short": content_short,
                    "timestamp": ts_str,
                })
    except Exception as e:
        print(f"  [warn] failed to read {os.path.basename(filepath)}: {e}", file=sys.stderr)

    if not session_id:
        session_id = os.path.basename(filepath).replace(".jsonl", "")

    return session_id, date_str, messages


# =============================================
# Embedding API
# =============================================

def call_embedding_api(texts: List[str]) -> List[List[float]]:
    """Call DashScope embeddings endpoint, return vectors in input order."""
    if not DASHSCOPE_API_KEY:
        raise ValueError("DASHSCOPE_API_KEY environment variable is not set")

    url = f"{DASHSCOPE_BASE_URL}/embeddings"
    data = json.dumps({
        "model": EMBEDDING_MODEL,
        "input": texts,
        "dimensions": EMBEDDING_DIMENSIONS,
        "encoding_format": "float",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    })

    last_error: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            if "error" in result:
                raise Exception(f"API error: {result['error']}")

            embeddings_data = sorted(result["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in embeddings_data]

        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429:
                wait = (attempt + 1) * 5
                print(f"  [rate-limit] waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = (attempt + 1) * 2
                print(f"  [retry {attempt+1}/{MAX_RETRIES}] {e}", file=sys.stderr)
                time.sleep(wait)
            else:
                raise

    raise Exception(f"Embedding API failed after {MAX_RETRIES} retries: {last_error}")


def embed_messages_batch(messages: List[Dict]) -> List[Optional[List[float]]]:
    """Embed a batch of messages; failed items return None."""
    texts = [msg["content"] for msg in messages]
    all_embeddings: List[Optional[List[float]]] = [None] * len(messages)

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i:i + EMBEDDING_BATCH_SIZE]
        try:
            batch_embeddings = call_embedding_api(batch)
            for j, emb in enumerate(batch_embeddings):
                all_embeddings[i + j] = emb
            time.sleep(REQUEST_INTERVAL)
        except Exception as e:
            print(f"  [error] embedding batch {i}-{i+len(batch)}: {e}", file=sys.stderr)

    return all_embeddings


# =============================================
# Main pipeline
# =============================================

def embed_sessions(conn: sqlite3.Connection, full_rebuild: bool = False, dry_run: bool = False):
    session_files = sorted(
        [p for p in glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))
         if os.path.basename(p) not in NON_SESSION_FILES],
        key=os.path.getmtime,
        reverse=True,
    )

    if not session_files:
        print(f"[error] no .jsonl files found in {SESSIONS_DIR}")
        return

    existing = {} if full_rebuild else get_existing_sessions(conn)

    to_process = []            # (filepath, is_new_session, new_messages)
    mtime_only_updates = []    # sessions with no new messages

    for filepath in session_files:
        filename = os.path.basename(filepath)
        file_mtime = os.path.getmtime(filepath)

        if full_rebuild or filename not in existing:
            sid, _, msgs = load_session(filepath)
            if msgs:
                to_process.append((filepath, True, msgs))
            continue

        if existing[filename] >= file_mtime:
            continue

        sid, _, msgs = load_session(filepath)
        if not msgs:
            mtime_only_updates.append((sid, file_mtime))
            continue

        existing_msg_ids = get_existing_msg_ids(conn, sid)
        new_msgs = [m for m in msgs if m["msg_id"] not in existing_msg_ids]

        if new_msgs:
            to_process.append((filepath, False, new_msgs))
        else:
            mtime_only_updates.append((sid, file_mtime))

    for sid, mtime in mtime_only_updates:
        conn.execute(
            "UPDATE sessions SET file_mtime = ? WHERE session_id = ?",
            (mtime, sid),
        )
    if mtime_only_updates:
        conn.commit()

    if not to_process:
        print("[ok] all sessions up to date")
        return

    total_new = sum(1 for _, is_new, _ in to_process if is_new)
    total_incr = sum(1 for _, is_new, _ in to_process if not is_new)
    print(f"[plan] {len(to_process)} sessions to process "
          f"(new: {total_new}, incremental: {total_incr})")

    if dry_run:
        total_msgs = sum(len(msgs) for _, _, msgs in to_process)
        est_tokens = total_msgs * 200
        est_cost = est_tokens / 1_000_000 * 0.5
        print(f"[dry-run] would embed ~{total_msgs} messages")
        print(f"[dry-run] est. tokens: ~{est_tokens:,}, est. cost: ~{est_cost:.4f} CNY")
        return

    if full_rebuild:
        print("[rebuild] clearing database")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM sessions")
        conn.commit()

    total_embedded = 0
    total_new_sessions = 0
    total_incremental_msgs = 0

    for idx, (filepath, is_new_session, new_messages) in enumerate(to_process):
        filename = os.path.basename(filepath)

        if is_new_session:
            session_id, date_str, _ = load_session(filepath)
            session_id = session_id or ""
            date_str = date_str or ""
            print(f"  [{idx+1}/{len(to_process)}] {filename} (new, {len(new_messages)} msgs)")
        else:
            session_id = ""
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line.strip())
                        if r.get('type') == 'session':
                            session_id = r.get('id', '')
                            break
                    except Exception:
                        pass
            if not session_id:
                session_id = filename.replace('.jsonl', '')
            print(f"  [{idx+1}/{len(to_process)}] {filename} (+{len(new_messages)} msgs)")

        embeddings = embed_messages_batch(new_messages)

        if is_new_session:
            file_mtime = os.path.getmtime(filepath)
            title = _get_session_title(filepath)
            conn.execute(
                "INSERT INTO sessions (session_id, filename, date, title, message_count, file_mtime, embedded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, filename, date_str, title, len(new_messages), file_mtime, datetime.now().isoformat()),
            )

        c = conn.cursor()
        c.execute("SELECT MAX(msg_index) FROM messages WHERE session_id = ?", (session_id,))
        max_index_result = c.fetchone()[0]
        next_index = (max_index_result + 1) if max_index_result is not None else 0

        import numpy as np

        for i, msg in enumerate(new_messages):
            emb = embeddings[i]
            emb_blob = None
            if emb is not None:
                emb_blob = np.array(emb, dtype=np.float32).tobytes()
                total_embedded += 1

            msg_index = next_index + i
            conn.execute(
                "INSERT INTO messages (session_id, msg_id, role, content, content_short, timestamp, msg_index, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, msg["msg_id"], msg["role"], msg["content"],
                 msg["content_short"], msg["timestamp"], msg_index, emb_blob),
            )

        file_mtime = os.path.getmtime(filepath)
        conn.execute(
            "UPDATE sessions SET message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?), "
            "file_mtime = ?, embedded_at = ? WHERE session_id = ?",
            (session_id, file_mtime, datetime.now().isoformat(), session_id),
        )

        conn.commit()

        if is_new_session:
            total_new_sessions += 1
        else:
            total_incremental_msgs += len(new_messages)

    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM sessions")
    total_sessions = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages")
    total_messages = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM messages WHERE embedding IS NOT NULL")
    total_embedded_in_db = c.fetchone()[0]

    print()
    print("=" * 50)
    print("[done] embedding complete")
    if total_new_sessions:
        print(f"  new sessions:        {total_new_sessions}")
    if total_incremental_msgs:
        print(f"  incremental messages: {total_incremental_msgs}")
    print(f"  embedded this run:   {total_embedded}")
    print(f"  database totals:     {total_sessions} sessions, {total_messages} messages "
          f"({total_embedded_in_db} with vectors)")
    est_cost = total_embedded * 200 / 1_000_000 * 0.5
    print(f"  est. cost this run:  ~{est_cost:.4f} CNY")


def _get_session_title(filepath: str) -> str:
    titles_path = os.path.join(SESSIONS_DIR, "session-titles.json")
    try:
        if os.path.exists(titles_path):
            with open(titles_path, "r", encoding="utf-8") as f:
                titles = json.load(f)
            return titles.get(filepath, "")
    except Exception:
        pass
    return ""


# =============================================
# CLI
# =============================================

def main():
    parser = argparse.ArgumentParser(description="conversation-search embedding engine")
    parser.add_argument("--full", action="store_true", help="full rebuild: drop database and start over")
    parser.add_argument("--dry-run", action="store_true", help="preview only, do not write")
    args = parser.parse_args()

    if not DASHSCOPE_API_KEY:
        print("[error] DASHSCOPE_API_KEY is not set")
        print("        export DASHSCOPE_API_KEY='your-api-key'")
        print("        or add it to your shell environment file")
        sys.exit(1)

    print("conversation-search embedding engine")
    print(f"  database:        {DB_PATH}")
    print(f"  sessions dir:    {SESSIONS_DIR}")
    print(f"  embedding model: {EMBEDDING_MODEL} ({EMBEDDING_DIMENSIONS}d)")
    print()

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        embed_sessions(conn, full_rebuild=args.full, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
