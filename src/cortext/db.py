"""Embedded entity-relationship graph store (SQLite + WAL + sqlite-vec)."""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

DEFAULT_DB_PATH = Path("~/.config/cortext/memory.db")
EMBEDDING_DIM = 384

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    archetype TEXT NOT NULL,
    summary TEXT DEFAULT '',
    metadata JSON DEFAULT '{{}}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES entities(id),
    target_id INTEGER NOT NULL REFERENCES entities(id),
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    metadata JSON DEFAULT '{{}}',
    UNIQUE(source_id, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    session_id TEXT NOT NULL,
    narrative TEXT NOT NULL,
    extracted_entities JSON DEFAULT '[]'
);

CREATE VIRTUAL TABLE IF NOT EXISTS entity_embeddings USING vec0(
    entity_id INTEGER PRIMARY KEY,
    embedding float[{EMBEDDING_DIM}]
);
"""

# Undirected walk from :root out to :max_hops. `reach` keeps the minimum hop at
# which each node is first seen. An edge sits at hop min(endpoint hops) + 1 and
# is only returned if that hop is within :max_hops.
_SUBGRAPH_SQL = """
WITH RECURSIVE walk(node_id, hop) AS (
    SELECT :root, 0
    UNION
    SELECT CASE WHEN e.source_id = w.node_id THEN e.target_id ELSE e.source_id END,
           w.hop + 1
    FROM walk AS w
    JOIN edges AS e ON e.source_id = w.node_id OR e.target_id = w.node_id
    WHERE w.hop < :max_hops
),
reach(node_id, hop) AS (
    SELECT node_id, MIN(hop) FROM walk GROUP BY node_id
)
SELECT ed.id AS edge_id,
       MIN(rs.hop, rt.hop) + 1 AS hop,
       ed.source_id,
       s.name AS source_name,
       s.archetype AS source_archetype,
       s.summary AS source_summary,
       ed.relation,
       ed.weight,
       ed.metadata,
       ed.target_id,
       t.name AS target_name,
       t.archetype AS target_archetype,
       t.summary AS target_summary
FROM edges AS ed
JOIN reach AS rs ON rs.node_id = ed.source_id
JOIN reach AS rt ON rt.node_id = ed.target_id
JOIN entities AS s ON s.id = ed.source_id
JOIN entities AS t ON t.id = ed.target_id
WHERE MIN(rs.hop, rt.hop) < :max_hops
ORDER BY hop ASC, ed.weight DESC, ed.id ASC
LIMIT :limit
"""


class DatabaseManager:
    """Owns the async SQLite connection and all graph read/write operations."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path if db_path is not None else DEFAULT_DB_PATH).expanduser()
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DatabaseManager is not connected; call connect() first")
        return self._conn

    async def connect(self) -> None:
        """Open the database, load sqlite-vec, apply pragmas, and create the schema."""
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.db_path))
        try:
            conn.row_factory = aiosqlite.Row

            await conn.enable_load_extension(True)
            await conn.load_extension(sqlite_vec.loadable_path())
            await conn.enable_load_extension(False)

            async with conn.execute("PRAGMA journal_mode=WAL") as cur:
                row = await cur.fetchone()
            if row is None or str(row[0]).lower() != "wal":
                raise RuntimeError(f"Failed to enable WAL journal mode (got {row[0] if row else None!r})")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")

            await conn.executescript(_SCHEMA)
            await conn.commit()
        except BaseException:
            await conn.close()
            raise
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()

    async def __aenter__(self) -> "DatabaseManager":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize writers on the shared connection; commit on success, roll back on error."""
        conn = self.conn
        async with self._write_lock:
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def upsert_entity(
        self,
        name: str,
        archetype: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
        embedding: Sequence[float] | None = None,
    ) -> int:
        """Insert or update an entity by unique name and return its id.

        On update, archetype and summary are overwritten; metadata is replaced
        only when provided. A given embedding (384 floats) replaces any stored one.
        """
        if embedding is not None and len(embedding) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have {EMBEDDING_DIM} dimensions, got {len(embedding)}")
        meta_json = json.dumps(metadata) if metadata is not None else None

        async with self._transaction() as conn:
            async with conn.execute(
                """
                INSERT INTO entities (name, archetype, summary, metadata)
                VALUES (?, ?, ?, COALESCE(?, '{}'))
                ON CONFLICT(name) DO UPDATE SET
                    archetype = excluded.archetype,
                    summary = excluded.summary,
                    metadata = COALESCE(?, entities.metadata),
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id
                """,
                (name, archetype, summary, meta_json, meta_json),
            ) as cur:
                row = await cur.fetchone()
            entity_id = int(row[0])

            if embedding is not None:
                # vec0 has no upsert; replace within the same transaction.
                await conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
                await conn.execute(
                    "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                    (entity_id, sqlite_vec.serialize_float32(list(embedding))),
                )
        return entity_id

    async def upsert_edge(
        self,
        source_id: int,
        target_id: int,
        relation: str,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert or update the (source, target, relation) edge and return its id.

        On update, weight is overwritten; metadata is replaced only when provided.
        Raises sqlite3.IntegrityError if either entity does not exist.
        """
        meta_json = json.dumps(metadata) if metadata is not None else None

        async with self._transaction() as conn:
            async with conn.execute(
                """
                INSERT INTO edges (source_id, target_id, relation, weight, metadata)
                VALUES (?, ?, ?, ?, COALESCE(?, '{}'))
                ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                    weight = excluded.weight,
                    metadata = COALESCE(?, edges.metadata)
                RETURNING id
                """,
                (source_id, target_id, relation, weight, meta_json, meta_json),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0])

    async def get_entity_subgraph(
        self, entity_id: int, max_hops: int = 1, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return up to `limit` edges within `max_hops` of an entity.

        Edges are walked in both directions but reported in stored orientation
        (source -> relation -> target). Each dict carries both endpoints' name,
        archetype and summary plus `hop` (1 = touches the root entity). Results
        are ordered nearest hop first, then by weight descending.
        """
        if max_hops < 1 or limit < 1:
            return []
        async with self.conn.execute(
            _SUBGRAPH_SQL, {"root": entity_id, "max_hops": max_hops, "limit": limit}
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"] or "{}")
            results.append(item)
        return results

    async def record_episode(
        self, session_id: str, narrative: str, entity_ids: Sequence[int] | None = None
    ) -> int:
        """Append an episode (timestamped now) and return its id."""
        async with self._transaction() as conn:
            async with conn.execute(
                """
                INSERT INTO episodes (timestamp, session_id, narrative, extracted_entities)
                VALUES (?, ?, ?, ?)
                RETURNING id
                """,
                (time.time(), session_id, narrative, json.dumps(list(entity_ids or []))),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0])
