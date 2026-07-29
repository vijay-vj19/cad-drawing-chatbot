import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    sheet_id TEXT,
    title TEXT,
    drawing_type TEXT,
    discipline TEXT,
    scale TEXT,
    rev TEXT,
    source_pdf TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    type TEXT,
    mark TEXT,
    properties TEXT,
    source_sheet TEXT,
    reliability TEXT
);

CREATE TABLE IF NOT EXISTS instances (
    tag TEXT,
    entity_type TEXT,
    x REAL,
    y REAL,
    sheet_id TEXT,
    grid TEXT,
    properties TEXT,
    reliability TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    topic TEXT,
    text TEXT,
    source_sheet TEXT,
    reliability TEXT
);

CREATE TABLE IF NOT EXISTS relationships (
    from_entity TEXT,
    relation TEXT,
    to_entity TEXT,
    source_sheet TEXT,
    reliability TEXT
);

CREATE TABLE IF NOT EXISTS context_notes (
    note TEXT,
    reliability TEXT,
    basis TEXT,
    what_to_verify TEXT,
    source_sheet TEXT
);

CREATE TABLE IF NOT EXISTS placeholders (
    entity_or_topic TEXT,
    what_is_missing TEXT,
    why TEXT,
    suggested_sheet TEXT
);
"""

CORE_TABLES = ["sheets", "schedules", "instances", "notes", "relationships", "context_notes", "placeholders"]


def get_conn(doc_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(config.sqlite_path(doc_id))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def run_readonly_sql(conn: sqlite3.Connection, sql: str) -> list[dict]:
    stripped = sql.strip().lower()
    if not stripped.startswith("select"):
        raise ValueError("Only SELECT statements are allowed")
    cursor = conn.execute(sql)
    columns = [d[0] for d in cursor.description] if cursor.description else []
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def table_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    for table in CORE_TABLES:
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts
