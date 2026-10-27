"""Embedded entity-relationship graph store (SQLite + WAL + sqlite-vec)."""

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

from .embed import Embedder, EmbeddingUnavailable
from .logging import get_logger

logger = get_logger("db")

DEFAULT_DB_PATH = Path("~/.config/cortext/memory.db")
EMBEDDING_DIM = 384

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    archetype TEXT NOT NULL,
    summary TEXT DEFAULT '',
    metadata JSON DEFAULT '{{}}',
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
    metadata JSON DEFAULT '{{}}',
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

_VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS entity_embeddings USING vec0(
    entity_id INTEGER PRIMARY KEY,
    embedding float[{EMBEDDING_DIM}]
);
"""

VEC_UNAVAILABLE_MESSAGE = "sqlite-vec extension failed to load; vector indexing disabled."

# Columns added after the first release; applied to pre-existing databases on connect.
_PROVENANCE_COLUMNS = (
    ("origin", "TEXT NOT NULL DEFAULT 'user_explicit'"),
    ("confidence", "REAL NOT NULL DEFAULT 1.0"),
)

# Access-tracking columns (temporal decay / frequency reinforcement), migrated onto older DBs.
_ACCESS_COLUMNS = (
    ("access_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_accessed_at", "DATETIME"),
)

# Provenance precedence: a higher-confidence assertion wins; at equal confidence a
# user_explicit assertion is never displaced by a non-user origin. Evaluated against
# the stored row (`{table}`) inside ON CONFLICT ... DO UPDATE.
_WINS = (
    "(excluded.confidence > {table}.confidence OR (excluded.confidence = {table}.confidence "
    "AND ({table}.origin != 'user_explicit' OR excluded.origin = 'user_explicit')))"
)


def embedding_text(name: str, summary: str | None) -> str:
    """Text embedded for an entity: `name: summary`, or just `name` without a summary."""
    summary = (summary or "").strip()
    return f"{name}: {summary}" if summary else name


def _norm_relation(relation: str) -> str:
    return "_".join((relation or "").casefold().split())


def _check_confidence(confidence: float) -> float:
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be between 0.0 and 1.0, got {confidence}")
    return float(confidence)


# An edge is active unless deprecated: weight > 0, confidence >= 0.2, no metadata.deprecated flag.
MIN_ACTIVE_CONFIDENCE = 0.2
_ACTIVE = (
    "{t}.weight > 0.0 AND {t}.confidence >= 0.2 "
    "AND COALESCE(json_extract({t}.metadata, '$.deprecated'), 0) != 1"
)

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
    WHERE w.hop < :max_hops AND (:active_only = 0 OR (ACTIVE_E))
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
       s.origin AS source_origin,
       s.confidence AS source_confidence,
       s.created_at AS source_created_at,
       s.access_count AS source_access_count,
       s.last_accessed_at AS source_last_accessed_at,
       ed.relation,
       ed.weight,
       ed.origin,
       ed.confidence,
       ed.metadata,
       ed.target_id,
       t.name AS target_name,
       t.archetype AS target_archetype,
       t.summary AS target_summary,
       t.origin AS target_origin,
       t.confidence AS target_confidence,
       t.created_at AS target_created_at,
       t.access_count AS target_access_count,
       t.last_accessed_at AS target_last_accessed_at,
       ed.updated_at AS edge_updated_at,
       ed.access_count AS edge_access_count,
       ed.last_accessed_at AS edge_last_accessed_at
FROM edges AS ed
JOIN reach AS rs ON rs.node_id = ed.source_id
JOIN reach AS rt ON rt.node_id = ed.target_id
JOIN entities AS s ON s.id = ed.source_id
JOIN entities AS t ON t.id = ed.target_id
WHERE MIN(rs.hop, rt.hop) < :max_hops AND (:active_only = 0 OR (ACTIVE_ED))
ORDER BY hop ASC, ed.confidence DESC, ed.weight DESC, ed.id ASC
LIMIT :limit
"""
_SUBGRAPH_SQL = _SUBGRAPH_SQL.replace("ACTIVE_ED", _ACTIVE.format(t="ed")).replace(
    "ACTIVE_E", _ACTIVE.format(t="e")
)


def _chmod_quiet(path: Path, mode: int) -> None:
    """chmod `path` if it exists; ignore PermissionError (not the owner / unprivileged)."""
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass
    except PermissionError:
        logger.debug("Cannot chmod %s to %o (permission denied)", path, mode)


class DatabaseManager:
    """Owns the async SQLite connection and all graph read/write operations."""

    def __init__(
        self, db_path: str | os.PathLike[str] | None = None, embedder: Embedder | None = None
    ) -> None:
        self.db_path = Path(db_path if db_path is not None else DEFAULT_DB_PATH).expanduser()
        self.embedder = embedder
        self._conn: aiosqlite.Connection | None = None
        self.vec_enabled = False
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DatabaseManager is not connected; call connect() first")
        return self._conn

    def _secure_files(self) -> None:
        """Restrict the database and its WAL/SHM companions to the owner (0600)."""
        for suffix in ("", "-wal", "-shm"):
            _chmod_quiet(self.db_path.with_name(self.db_path.name + suffix), 0o600)

    async def connect(self) -> None:
        """Open the database, load sqlite-vec, apply pragmas, and create the schema."""
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _chmod_quiet(self.db_path.parent, 0o700)
        conn = await aiosqlite.connect(str(self.db_path))
        try:
            conn.row_factory = aiosqlite.Row
            self._secure_files()

            vec_enabled = await self._load_vec(conn)

            async with conn.execute("PRAGMA journal_mode=WAL") as cur:
                row = await cur.fetchone()
            if row is None or str(row[0]).lower() != "wal":
                raise RuntimeError(f"Failed to enable WAL journal mode (got {row[0] if row else None!r})")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")

            logger.info("WAL journal mode enabled for %s", self.db_path)
            await conn.executescript(_SCHEMA)
            if vec_enabled:
                await conn.executescript(_VEC_SCHEMA)
            await self._migrate(conn)
            await conn.commit()
            self._secure_files()
        except BaseException:
            await conn.close()
            raise
        self._conn = conn
        self.vec_enabled = vec_enabled
        logger.info("Database connected: %s", self.db_path)

    @staticmethod
    async def _load_vec(conn: aiosqlite.Connection) -> bool:
        """Load sqlite-vec; on failure warn and return False so relational ops keep working."""
        try:
            await conn.enable_load_extension(True)
            await conn.load_extension(sqlite_vec.loadable_path())
            return True
        except (sqlite3.Error, AttributeError, OSError) as exc:
            logger.warning(VEC_UNAVAILABLE_MESSAGE)
            logger.debug("sqlite-vec load error: %r", exc, exc_info=True)
            return False
        finally:
            try:
                await conn.enable_load_extension(False)
            except (sqlite3.Error, AttributeError):
                pass

    @staticmethod
    async def _migrate(conn: aiosqlite.Connection) -> None:
        """Add provenance columns to databases created before they existed."""
        for table in ("entities", "edges"):
            async with conn.execute(f"PRAGMA table_info({table})") as cur:
                existing = {row[1] for row in await cur.fetchall()}
            for column, ddl in _PROVENANCE_COLUMNS:
                if column not in existing:
                    logger.info("Migration: adding %s.%s", table, column)
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            for column, ddl in _ACCESS_COLUMNS:
                if column not in existing:
                    logger.info("Migration: adding %s.%s", table, column)
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        async with conn.execute("PRAGMA table_info(episodes)") as cur:
            if "compacted_at" not in {row[1] for row in await cur.fetchall()}:
                logger.info("Migration: adding episodes.compacted_at")
                await conn.execute("ALTER TABLE episodes ADD COLUMN compacted_at DATETIME")
        async with conn.execute("PRAGMA table_info(edges)") as cur:
            if "updated_at" not in {row[1] for row in await cur.fetchall()}:
                logger.info("Migration: adding edges.updated_at")
                await conn.execute("ALTER TABLE edges ADD COLUMN updated_at DATETIME")

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()
            logger.info("Database closed: %s", self.db_path)

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

    async def _auto_embed(self, name: str, summary: str) -> list[float] | None:
        """Embed `name: summary` with the configured embedder; failures never block the write."""
        if not self.vec_enabled:
            return None
        try:
            return await asyncio.to_thread(self.embedder.embed, embedding_text(name, summary))
        except EmbeddingUnavailable:
            return None  # already warned once by the embedder
        except Exception:
            logger.warning("embedding generation failed for %r", name, exc_info=True)
            return None

    async def upsert_entity(
        self,
        name: str,
        archetype: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
        embedding: Sequence[float] | None = None,
        origin: str = "user_explicit",
        confidence: float = 1.0,
    ) -> int:
        """Insert or update an entity by unique name and return its id.

        On update, archetype, summary, origin and confidence are overwritten (and
        metadata replaced only when provided) provided the incoming assertion wins
        on provenance: higher confidence wins, and at equal confidence a user_explicit
        assertion is never displaced. A losing assertion leaves the stored row and
        embedding untouched. A given embedding (384 floats) replaces any stored one.
        """
        if embedding is not None and len(embedding) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have {EMBEDDING_DIM} dimensions, got {len(embedding)}")
        confidence = _check_confidence(confidence)
        if embedding is None and self.embedder is not None:
            embedding = await self._auto_embed(name, summary)
        wins = _WINS.format(table="entities")
        meta_json = json.dumps(metadata) if metadata is not None else None

        async with self._transaction() as conn:
            async with conn.execute(
                """
                INSERT INTO entities (name, archetype, summary, metadata, origin, confidence)
                VALUES (?, ?, ?, COALESCE(?, '{}'), ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    archetype = CASE WHEN WINS THEN excluded.archetype ELSE entities.archetype END,
                    summary = CASE WHEN WINS THEN excluded.summary ELSE entities.summary END,
                    metadata = CASE WHEN WINS THEN COALESCE(?, entities.metadata)
                                    ELSE entities.metadata END,
                    origin = CASE WHEN WINS THEN excluded.origin ELSE entities.origin END,
                    confidence = CASE WHEN WINS THEN excluded.confidence
                                      ELSE entities.confidence END,
                    updated_at = CASE WHEN WINS THEN CURRENT_TIMESTAMP ELSE entities.updated_at END
                RETURNING id, origin, confidence
                """.replace("WINS", wins),
                (name, archetype, summary, meta_json, origin, confidence, meta_json),
            ) as cur:
                row = await cur.fetchone()
            entity_id = int(row[0])
            applied = row[1] == origin and row[2] == confidence

            if embedding is not None and applied and self.vec_enabled:
                # vec0 has no upsert; replace within the same transaction.
                await conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
                await conn.execute(
                    "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                    (entity_id, sqlite_vec.serialize_float32(list(embedding))),
                )
        return entity_id

    @staticmethod
    def _aliases_of(raw: Any) -> list[str]:
        """String aliases from stored metadata (JSON text or dict); tolerates bad data."""
        if isinstance(raw, (str, bytes)) or raw is None:
            try:
                raw = json.loads(raw or "{}")
            except ValueError:
                return []
        stored = raw.get("aliases") if isinstance(raw, dict) else None
        if isinstance(stored, str):
            stored = [stored]
        return [a for a in stored if isinstance(a, str)] if isinstance(stored, list) else []

    async def find_duplicate_entity(
        self,
        name: str,
        archetype: str | None = None,
        summary: str = "",
        distance_threshold: float = 0.45,
    ) -> int | None:
        """Id of an existing entity that `name` duplicates, else None.

        An entity stored under exactly `name` is not a duplicate (callers upsert it
        normally). Otherwise: (1) a case-insensitive match on an entity name or a
        `metadata.aliases` entry wins; (2) when vectors and an embedder are available, the
        nearest entity within `distance_threshold` (L2) whose archetype equals `archetype`
        (`None` = unknown, matches any). Without vectors only step (1) applies.
        """
        folded = name.strip().casefold()
        if not folded:
            return None
        async with self.conn.execute("SELECT id, name, metadata FROM entities") as cur:
            rows = await cur.fetchall()
        if any(row[1] == name for row in rows):
            return None
        for entity_id, entity_name, raw in rows:
            aliases = {a.strip().casefold() for a in self._aliases_of(raw)}
            if entity_name.casefold() == folded or folded in aliases:
                return int(entity_id)
        if not self.vec_enabled or self.embedder is None:
            return None
        embedding = await self._auto_embed(name, summary)
        if embedding is None:
            return None
        for entity_id in await self.find_similar_entities(
            embedding, limit=5, distance_threshold=distance_threshold
        ):
            async with self.conn.execute(
                "SELECT archetype FROM entities WHERE id = ?", (entity_id,)
            ) as cur:
                row = await cur.fetchone()
            if row and (archetype is None or row[0] == archetype):
                return entity_id
        return None

    async def find_similar_entities(
        self, query_embedding: Sequence[float], limit: int = 5, distance_threshold: float = 1.0
    ) -> list[int]:
        """Entity ids nearest to `query_embedding` (sqlite-vec KNN, L2 distance), nearest first.

        Only neighbours with `distance <= distance_threshold` are returned.
        """
        if limit < 1:
            return []
        if len(query_embedding) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have {EMBEDDING_DIM} dimensions, got {len(query_embedding)}")
        if not self.vec_enabled:
            return []
        async with self.conn.execute(
            "SELECT entity_id, distance FROM entity_embeddings "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(list(query_embedding)), limit),
        ) as cur:
            rows = await cur.fetchall()
        return [int(r[0]) for r in rows if r[1] <= distance_threshold]

    async def merge_entity_alias(
        self,
        entity_id: int,
        alias: str,
        summary: str | None = None,
        origin: str = "user_explicit",
        confidence: float = 1.0,
    ) -> None:
        """Fold a duplicate candidate into entity `entity_id` without inserting a row.

        Appends `alias` to `metadata.aliases` (case-insensitive de-dup, never the entity's
        own name), fills an empty summary from `summary` (re-embedding it), and applies
        origin/confidence only if the assertion wins on provenance. Always bumps `updated_at`.
        """
        confidence = _check_confidence(confidence)
        alias = alias.strip()
        async with self._transaction() as conn:
            async with conn.execute(
                "SELECT name, summary, metadata, origin, confidence FROM entities WHERE id = ?",
                (entity_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"entity {entity_id} does not exist")
            name, old_summary, raw, old_origin, old_conf = row
            try:
                meta = json.loads(raw or "{}")
            except ValueError:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            aliases = self._aliases_of(meta)
            seen = {a.casefold() for a in aliases} | {name.casefold()}
            if alias and alias.casefold() not in seen:
                aliases.append(alias)
            meta["aliases"] = aliases
            new_summary = old_summary or ""
            fill = bool(summary and summary.strip() and not new_summary.strip())
            if fill:
                new_summary = summary.strip()
            wins = confidence > old_conf or (
                confidence == old_conf
                and (old_origin != "user_explicit" or origin == "user_explicit")
            )
            await conn.execute(
                "UPDATE entities SET metadata = ?, summary = ?, origin = ?, confidence = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    json.dumps(meta),
                    new_summary,
                    origin if wins else old_origin,
                    confidence if wins else old_conf,
                    entity_id,
                ),
            )
        if fill and self.vec_enabled and self.embedder is not None:
            embedding = await self._auto_embed(name, new_summary)
            if embedding is not None:
                async with self._transaction() as conn:
                    await conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
                    await conn.execute(
                        "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                        (entity_id, sqlite_vec.serialize_float32(list(embedding))),
                    )

    async def upsert_entity_deduped(
        self,
        name: str,
        archetype: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
        embedding: Sequence[float] | None = None,
        origin: str = "user_explicit",
        confidence: float = 1.0,
        distance_threshold: float = 0.45,
    ) -> int:
        """Like `upsert_entity`, but a near-duplicate of an existing entity is merged into it.

        Returns the canonical entity id (see `find_duplicate_entity`, `merge_entity_alias`).
        """
        dup = await self.find_duplicate_entity(name, archetype, summary, distance_threshold)
        if dup is not None:
            await self.merge_entity_alias(dup, name, summary, origin, confidence)
            return dup
        return await self.upsert_entity(
            name, archetype, summary, metadata, embedding, origin, confidence
        )

    async def upsert_edge(
        self,
        source_id: int,
        target_id: int,
        relation: str,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
        origin: str = "user_explicit",
        confidence: float = 1.0,
    ) -> int:
        """Insert or update the (source, target, relation) edge and return its id.

        On update, weight, origin and confidence are overwritten (and metadata replaced
        only when provided) provided the incoming assertion wins on provenance (see
        `upsert_entity`); a losing assertion leaves the stored edge untouched.
        Raises sqlite3.IntegrityError if either entity does not exist.
        """
        meta_json = json.dumps(metadata) if metadata is not None else None
        confidence = _check_confidence(confidence)
        wins = _WINS.format(table="edges")

        async with self._transaction() as conn:
            async with conn.execute(
                """
                INSERT INTO edges (source_id, target_id, relation, weight, metadata, origin, confidence)
                VALUES (?, ?, ?, ?, COALESCE(?, '{}'), ?, ?)
                ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                    weight = CASE WHEN WINS THEN excluded.weight ELSE edges.weight END,
                    metadata = CASE WHEN WINS THEN COALESCE(
                                        ?,
                                        json_remove(edges.metadata, '$.deprecated', '$.deprecation_reason'))
                                    ELSE edges.metadata END,
                    origin = CASE WHEN WINS THEN excluded.origin ELSE edges.origin END,
                    confidence = CASE WHEN WINS THEN excluded.confidence
                                      ELSE edges.confidence END,
                    updated_at = CASE WHEN WINS THEN CURRENT_TIMESTAMP ELSE edges.updated_at END
                RETURNING id
                """.replace("WINS", wins),
                (source_id, target_id, relation, weight, meta_json, origin, confidence, meta_json),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0])

    async def _resolve_entity_id(self, ref: str | int) -> int | None:
        """Canonical entity id for an id, exact name, or casefolded name/`metadata.aliases` match."""
        if isinstance(ref, int):
            async with self.conn.execute("SELECT id FROM entities WHERE id = ?", (ref,)) as cur:
                row = await cur.fetchone()
            return int(row[0]) if row else None
        name = ref.strip()
        folded = name.casefold()
        if not folded:
            return None
        async with self.conn.execute("SELECT id, name, metadata FROM entities") as cur:
            rows = await cur.fetchall()
        for entity_id, entity_name, _ in rows:
            if entity_name == name:
                return int(entity_id)
        for entity_id, entity_name, raw in rows:
            aliases = {a.strip().casefold() for a in self._aliases_of(raw)}
            if entity_name.casefold() == folded or folded in aliases:
                return int(entity_id)
        return None

    async def deprecate_edge(
        self,
        source_name: str | int,
        target_name: str | int,
        relation: str,
        reason: str = "deprecated",
    ) -> bool:
        """Sever the active (source, target, relation) edge without deleting it.

        Endpoints are entity ids or names (canonical names and `metadata.aliases` both
        resolve). The relation is compared casefolded with spaces as underscores. The row
        is kept (episodic history intact) with `weight = 0.0`, `confidence = 0.0`,
        `metadata.deprecated = True`, `metadata.deprecation_reason = reason` and a fresh
        `updated_at`. Returns False when no matching, not-yet-deprecated edge exists.
        """
        source_id = await self._resolve_entity_id(source_name)
        target_id = await self._resolve_entity_id(target_name)
        if source_id is None or target_id is None:
            return False
        wanted = _norm_relation(relation)
        async with self._transaction() as conn:
            async with conn.execute(
                "SELECT id, relation, metadata FROM edges WHERE source_id = ? AND target_id = ?",
                (source_id, target_id),
            ) as cur:
                rows = await cur.fetchall()
            for edge_id, stored_relation, raw in rows:
                if _norm_relation(stored_relation) != wanted:
                    continue
                try:
                    meta = json.loads(raw or "{}")
                except ValueError:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                if meta.get("deprecated") is True:
                    continue
                meta["deprecated"] = True
                meta["deprecation_reason"] = reason
                await conn.execute(
                    "UPDATE edges SET weight = 0.0, confidence = 0.0, metadata = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (json.dumps(meta), edge_id),
                )
                return True
        return False

    async def get_entity_subgraph(
        self, entity_id: int, max_hops: int = 1, limit: int = 10, active_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return up to `limit` edges within `max_hops` of an entity.

        Edges are walked in both directions but reported in stored orientation
        (source -> relation -> target). Each dict carries both endpoints' name,
        archetype, summary, origin and confidence plus `hop` (1 = touches the root
        entity); the edge's own `origin`/`confidence` are included too. Results are
        ordered nearest hop first, then by confidence and weight descending. With
        `active_only`, deprecated edges (see `deprecate_edge`) are neither returned nor
        traversed.
        """
        if max_hops < 1 or limit < 1:
            return []
        async with self.conn.execute(
            _SUBGRAPH_SQL, {"root": entity_id, "max_hops": max_hops, "limit": limit, "active_only": int(active_only)}
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"] or "{}")
            results.append(item)
        return results

    async def touch_access(self, entity_ids: Sequence[int], edge_ids: Sequence[int]) -> None:
        """Reinforce facts: `access_count += 1` and `last_accessed_at = now` for the given ids.

        One transaction covers both tables; duplicate ids count once and empty
        inputs are a no-op.
        """
        entity_rows = [(int(i),) for i in dict.fromkeys(entity_ids)]
        edge_rows = [(int(i),) for i in dict.fromkeys(edge_ids)]
        if not entity_rows and not edge_rows:
            return
        async with self._transaction() as conn:
            for table, rows in (("entities", entity_rows), ("edges", edge_rows)):
                if rows:
                    await conn.executemany(
                        f"UPDATE {table} SET access_count = access_count + 1, "
                        "last_accessed_at = CURRENT_TIMESTAMP WHERE id = ?",
                        rows,
                    )

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


    async def get_uncompacted_sessions(self, older_than_seconds: float) -> list[str]:
        """Session ids whose newest uncompacted episode is older than the cutoff (oldest first)."""
        cutoff = time.time() - older_than_seconds
        async with self.conn.execute(
            "SELECT session_id FROM episodes WHERE compacted_at IS NULL "
            "GROUP BY session_id HAVING MAX(timestamp) < ? ORDER BY MAX(timestamp), session_id",
            (cutoff,),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def get_session_episodes(
        self, session_id: str, uncompacted_only: bool = True
    ) -> list[dict[str, Any]]:
        """Episodes of a session in chronological order; `extracted_entities` is decoded."""
        sql = "SELECT * FROM episodes WHERE session_id = ?"
        if uncompacted_only:
            sql += " AND compacted_at IS NULL"
        async with self.conn.execute(sql + " ORDER BY timestamp, id", (session_id,)) as cur:
            rows = await cur.fetchall()
        episodes = []
        for row in rows:
            item = dict(row)
            try:
                item["extracted_entities"] = json.loads(item["extracted_entities"] or "[]")
            except ValueError:
                item["extracted_entities"] = []
            episodes.append(item)
        return episodes

    async def mark_episodes_compacted(self, episode_ids: Sequence[int]) -> None:
        """Stamp `compacted_at = CURRENT_TIMESTAMP` on the given episodes (empty is a no-op)."""
        rows = [(int(i),) for i in dict.fromkeys(episode_ids)]
        if not rows:
            return
        async with self._transaction() as conn:
            await conn.executemany(
                "UPDATE episodes SET compacted_at = CURRENT_TIMESTAMP WHERE id = ?", rows
            )

    async def update_entity_summary(
        self, name: str, summary: str, origin: str = "model_inferred", confidence: float = 0.6
    ) -> bool:
        """Replace only an entity's summary when the assertion wins on provenance.

        Same precedence as `upsert_entity` (higher confidence wins; at equal confidence
        user_explicit is never displaced), but the stored origin/confidence are kept.
        Returns False when no entity matches (case-insensitive) or the assertion loses.
        """
        confidence = _check_confidence(confidence)
        async with self._transaction() as conn:
            cur = await conn.execute(
                "UPDATE entities SET summary = :summary, updated_at = CURRENT_TIMESTAMP "
                "WHERE name = :name COLLATE NOCASE AND (:conf > confidence OR (:conf = confidence "
                "AND (origin != 'user_explicit' OR :origin = 'user_explicit')))",
                {"summary": summary, "name": name, "conf": confidence, "origin": origin},
            )
            return cur.rowcount > 0
