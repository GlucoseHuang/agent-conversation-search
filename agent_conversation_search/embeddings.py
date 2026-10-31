from __future__ import annotations

import array
import json
import math
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

from .models import now_iso, text_hash


class EmbeddingUnavailable(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        batch_size: int = 16,
        timeout: int = 60,
        max_chars: int = 6000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.max_chars = max_chars

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        if not self.available:
            raise EmbeddingUnavailable("ACS_EMBEDDING_API_KEY is not set")
        values = [text[: self.max_chars] for text in texts]
        output: list[list[float]] = []
        for start in range(0, len(values), self.batch_size):
            output.extend(self._embed_batch(values[start : start + self.batch_size]))
        return output

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_texts([text])
        if not vectors:
            raise EmbeddingUnavailable("empty embedding response")
        return vectors[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps(
            {
                "model": self.model,
                "input": texts,
                "encoding_format": "float",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        url = f"{self.base_url}/embeddings"
        last_error: Optional[Exception] = None
        for attempt in range(4):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return self._parse_response(body, len(texts))
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    break
                time.sleep(2**attempt)
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if attempt == 3:
                    break
                time.sleep(2**attempt)
        raise EmbeddingUnavailable(f"embedding request failed: {last_error}")

    @staticmethod
    def _parse_response(body: dict[str, Any], expected: int) -> list[list[float]]:
        data = body.get("data")
        if isinstance(data, list):
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            vectors = [item.get("embedding") for item in ordered]
            if len(vectors) == expected and all(isinstance(vector, list) for vector in vectors):
                return vectors
        output = body.get("output", {})
        embeddings = output.get("embeddings") if isinstance(output, dict) else None
        if isinstance(embeddings, list) and len(embeddings) == expected:
            return embeddings
        raise EmbeddingUnavailable(f"unexpected embedding response: {body}")


def pack_vector(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> array.array:
    value = array.array("f")
    value.frombytes(blob)
    return value


def cosine(left: Iterable[float], right: Iterable[float]) -> float:
    a = list(left)
    b = list(right)
    if not a or len(a) != len(b):
        return 0.0
    dot = math.fsum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(math.fsum(x * x for x in a))
    norm_b = math.sqrt(math.fsum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def pending_events(
    conn: sqlite3.Connection,
    kinds: list[str],
    limit: Optional[int] = None,
    session_id: Optional[str] = None,
    agent: Optional[str] = None,
    min_chars: int = 0,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in kinds)
    conditions = [
        "e.text IS NOT NULL",
        "e.text != ''",
        f"e.kind IN ({placeholders})",
        "(m.event_id IS NULL OR m.text_hash != e.text_hash)",
    ]
    params: list[Any] = list(kinds)
    if session_id:
        conditions.append("e.session_id = ?")
        params.append(session_id)
    if agent:
        conditions.append("e.agent = ?")
        params.append(agent)
    if min_chars:
        conditions.append("length(e.text) >= ?")
        params.append(min_chars)
    sql = f"""
        SELECT e.id, e.text, e.text_hash
        FROM events e
        LEFT JOIN embeddings m ON m.event_id = e.id
        WHERE {' AND '.join(conditions)}
        ORDER BY e.timestamp, e.id
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


def embed_pending(
    conn: sqlite3.Connection,
    client: EmbeddingClient,
    kinds: list[str],
    limit: Optional[int] = None,
    batch_size: int = 16,
    progress: Optional[Any] = None,
    session_id: Optional[str] = None,
    agent: Optional[str] = None,
    min_chars: int = 0,
) -> dict[str, int]:
    if not client.available:
        raise EmbeddingUnavailable("ACS_EMBEDDING_API_KEY is not set")
    rows = pending_events(
        conn,
        kinds,
        limit,
        session_id=session_id,
        agent=agent,
        min_chars=min_chars,
    )
    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        texts = [row["text"] for row in batch]
        vectors = client.embed_texts(texts)
        for row, vector in zip(batch, vectors):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings(event_id, model, dim, vector, text_hash, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    row["id"],
                    client.model,
                    len(vector),
                    pack_vector(vector),
                    row["text_hash"],
                    now_iso(),
                ),
            )
        done += len(batch)
        conn.commit()
        if progress:
            progress(done, len(rows))
    return {"pending": len(rows), "embedded": done}


def embedding_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT model, dim, COUNT(*) AS count FROM embeddings "
            "GROUP BY model, dim ORDER BY count DESC"
        )
    ]


def mark_embedding_model(conn: sqlite3.Connection, model: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('embedding_model', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (model,),
    )