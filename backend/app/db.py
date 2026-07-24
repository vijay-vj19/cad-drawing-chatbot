import json
import sqlite3

import numpy as np

from . import config


def get_conn(doc_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(config.sqlite_path(doc_id))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_chunks_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_sheet TEXT,
            reliability TEXT,
            text TEXT,
            embedding BLOB
        )
        """
    )
    conn.commit()


def insert_chunk(conn: sqlite3.Connection, source_sheet: str, reliability: str, text: str, embedding: list[float]) -> None:
    vec = np.array(embedding, dtype=np.float32).tobytes()
    conn.execute(
        "INSERT INTO chunks (source_sheet, reliability, text, embedding) VALUES (?, ?, ?, ?)",
        (source_sheet, reliability, text, vec),
    )
    conn.commit()


def search_chunks(conn: sqlite3.Connection, query_embedding: list[float], top_k: int = 4) -> list[dict]:
    rows = conn.execute("SELECT source_sheet, reliability, text, embedding FROM chunks").fetchall()
    if not rows:
        return []
    q = np.array(query_embedding, dtype=np.float32)
    q_norm = np.linalg.norm(q) or 1.0

    scored = []
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        denom = (np.linalg.norm(vec) * q_norm) or 1.0
        score = float(np.dot(vec, q) / denom)
        scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "source_sheet": row["source_sheet"],
            "reliability": row["reliability"],
            "text": row["text"],
            "score": round(score, 4),
        }
        for score, row in scored[:top_k]
    ]


def run_readonly_sql(conn: sqlite3.Connection, sql: str) -> list[dict]:
    stripped = sql.strip().lower()
    if not stripped.startswith("select"):
        raise ValueError("Only SELECT statements are allowed")
    cursor = conn.execute(sql)
    columns = [d[0] for d in cursor.description] if cursor.description else []
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def table_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    for table in ("sheets", "schedules", "instances", "notes"):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = 0
    return counts


def list_sheets(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute("SELECT * FROM sheets").fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]
