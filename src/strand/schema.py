"""Database DDL and migrations (tables, indexes, the sqlite-vec virtual table)."""

import aiosqlite

from .common import EMBEDDING_DIM
from .logging import get_logger

logger = get_logger("db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    archetype TEXT NOT NULL,
    summary TEXT DEFAULT '',
    metadata JSON DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'user_explicit',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at DATETIME
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES entities(id),
    target_id INTEGER NOT NULL REFERENCES entities(id),
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    metadata JSON DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'user_explicit',
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at DATETIME,
    UNIQUE(source_id, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    session_id TEXT NOT NULL,
    narrative TEXT NOT NULL,
    extracted_entities JSON DEFAULT '[]',
    compacted_at DATETIME
);
"""

VEC_SCHEMA_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS entity_embeddings USING vec0(
    entity_id INTEGER PRIMARY KEY,
    embedding float[{EMBEDDING_DIM}]
);
"""

ORPHAN_VECTOR_SWEEP_SQL = "DELETE FROM entity_embeddings WHERE entity_id NOT IN (SELECT id FROM entities)"

# Columns added after the first release, as (table, column, DDL); applied to older databases.
_MIGRATION_COLUMNS = (
    ("entities", "origin", "TEXT NOT NULL DEFAULT 'user_explicit'"),
    ("entities", "confidence", "REAL NOT NULL DEFAULT 1.0"),
    ("entities", "access_count", "INTEGER NOT NULL DEFAULT 0"),
    ("entities", "last_accessed_at", "DATETIME"),
    ("edges", "origin", "TEXT NOT NULL DEFAULT 'user_explicit'"),
    ("edges", "confidence", "REAL NOT NULL DEFAULT 1.0"),
    ("edges", "access_count", "INTEGER NOT NULL DEFAULT 0"),
    ("edges", "last_accessed_at", "DATETIME"),
    ("edges", "updated_at", "DATETIME"),
    ("episodes", "compacted_at", "DATETIME"),
)


async def run_migrations(conn: aiosqlite.Connection, vec_enabled: bool) -> None:
    """Create any missing tables/indexes (vec0 only when `vec_enabled`), then add columns
    that databases created by older releases lack. Idempotent; the caller commits."""
    await conn.executescript(SCHEMA_SQL)
    if vec_enabled:
        await conn.executescript(VEC_SCHEMA_SQL)
        # Sweep vectors orphaned by historical drops, so a deleted entity leaves no embedding behind.
        cur = await conn.execute(ORPHAN_VECTOR_SWEEP_SQL)
        if cur.rowcount > 0:
            logger.info("Migration: removed %d orphan embedding(s)", cur.rowcount)
    existing: dict[str, set[str]] = {}
    for table, column, ddl in _MIGRATION_COLUMNS:
        if table not in existing:
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                existing[table] = {row[1] for row in await cur.fetchall()}
        if column not in existing[table]:
            logger.info("Migration: adding %s.%s", table, column)
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
