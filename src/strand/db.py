"""Embedded entity-relationship graph store (SQLite + WAL + sqlite-vec)."""

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

from .common import EMBEDDING_DIM, MIN_EDGE_CONFIDENCE, aliases_of, load_metadata
from .embed import Embedder, EmbeddingUnavailable
from .logging import get_logger
from .schema import run_migrations

logger = get_logger("db")

DEFAULT_DB_PATH = Path("~/.config/strand/memory.db")

VEC_UNAVAILABLE_MESSAGE = "sqlite-vec extension failed to load; vector indexing disabled."

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
MIN_ACTIVE_CONFIDENCE = MIN_EDGE_CONFIDENCE
_ACTIVE = (
    "{t}.weight > 0.0 AND {t}.confidence >= " + str(MIN_EDGE_CONFIDENCE) + " "
    "AND COALESCE(json_extract({t}.metadata, '$.deprecated'), 0) != 1"
)

# SQL twin of `common.is_private` for the table alias `{t}`. A CASE (not OR/AND) because SQLite
# does not guarantee short-circuiting and json_type() raises on malformed JSON; CASE evaluates in
# order. Fails closed exactly like the Python predicate: corrupt or non-object metadata is private,
# NULL/blank metadata is not, the archetype is matched case-insensitively and the flag may be
# true, 1 / 1.0, or the strings "true" / "1" (any case).
_TRIM_SQL = "char(32, 9, 10, 13)"
PRIVATE_ENTITY_SQL = (
    "(CASE "
    "WHEN lower(trim({t}.archetype, " + _TRIM_SQL + ")) = 'private' THEN 1 "
    "WHEN {t}.metadata IS NULL OR trim({t}.metadata, " + _TRIM_SQL + ") = '' THEN 0 "
    "WHEN NOT json_valid({t}.metadata) THEN 1 "
    "WHEN json_type({t}.metadata) != 'object' THEN 1 "
    "WHEN json_type({t}.metadata, '$.private') = 'true' THEN 1 "
    "WHEN json_type({t}.metadata, '$.private') IN ('integer', 'real') "
    "AND json_extract({t}.metadata, '$.private') = 1 THEN 1 "
    "WHEN json_type({t}.metadata, '$.private') = 'text' "
    "AND lower(trim(json_extract({t}.metadata, '$.private'), " + _TRIM_SQL + ")) IN ('true', '1') THEN 1 "
    "ELSE 0 END = 1)"
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
    JOIN entities AS en ON en.id = CASE WHEN e.source_id = w.node_id THEN e.target_id ELSE e.source_id END
    WHERE w.hop < :max_hops AND (:active_only = 0 OR (ACTIVE_E))
      AND NOT_PRIVATE_EN
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
_SUBGRAPH_SQL = (
    _SUBGRAPH_SQL.replace("NOT_PRIVATE_EN", "NOT " + PRIVATE_ENTITY_SQL.format(t="en"))
    .replace("ACTIVE_ED", _ACTIVE.format(t="ed"))
    .replace("ACTIVE_E", _ACTIVE.format(t="e"))
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
            await run_migrations(conn, vec_enabled)
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
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
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

        async with self.transaction() as conn:
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
            aliases = {a.strip().casefold() for a in aliases_of(raw)}
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

    async def find_entity(self, name_or_alias: str) -> tuple[int, str] | None:
        """(id, canonical name) of the entity `name_or_alias` refers to, else None.

        A case-insensitive canonical-name match wins; otherwise a `metadata.aliases` match
        counts only when it is unambiguous (exactly one entity), so a shared alias never
        resolves to an arbitrary entity.
        """
        folded = (name_or_alias or "").strip().casefold()
        if not folded:
            return None
        async with self.conn.execute("SELECT id, name, metadata FROM entities ORDER BY id") as cur:
            rows = await cur.fetchall()
        for entity_id, name, _ in rows:
            if name.casefold() == folded:
                return int(entity_id), name
        matches = [
            (int(entity_id), name)
            for entity_id, name, raw in rows
            if folded in {a.strip().casefold() for a in aliases_of(raw)}
        ]
        return matches[0] if len(matches) == 1 else None

    async def drop_entity(self, entity_id: int) -> bool:
        """Permanently delete an entity with its edges and embedding (one transaction).

        Returns False when no such entity exists.
        """
        async with self.transaction() as conn:
            await conn.execute(
                "DELETE FROM edges WHERE source_id = ? OR target_id = ?", (entity_id, entity_id)
            )
            if self.vec_enabled:
                await conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
            cur = await conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
            return cur.rowcount > 0

    async def drop_entity_by_name(self, name_or_alias: str) -> bool:
        """`drop_entity` for the entity a canonical name or alias resolves to."""
        found = await self.find_entity(name_or_alias)
        return found is not None and await self.drop_entity(found[0])

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
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT name, summary, metadata, origin, confidence FROM entities WHERE id = ?",
                (entity_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise KeyError(f"entity {entity_id} does not exist")
            name, old_summary, raw, old_origin, old_conf = row
            meta = load_metadata(raw)
            aliases = aliases_of(meta)
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
                async with self.transaction() as conn:
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

        async with self.transaction() as conn:
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
            aliases = {a.strip().casefold() for a in aliases_of(raw)}
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
        async with self.transaction() as conn:
            async with conn.execute(
                "SELECT id, relation, metadata FROM edges WHERE source_id = ? AND target_id = ?",
                (source_id, target_id),
            ) as cur:
                rows = await cur.fetchall()
            for edge_id, stored_relation, raw in rows:
                if _norm_relation(stored_relation) != wanted:
                    continue
                meta = load_metadata(raw)
                if meta.get("deprecated") is True:
                    logger.info(
                        "edge %s -[%s]-> %s already deprecated; nothing to do",
                        source_id, relation, target_id,
                    )
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

    async def prune_decayed_edges(
        self, days: float = 90.0, dry_run: bool = False, max_access_count: int = 2
    ) -> tuple[int, int]:
        """Delete deprecated edges and cold, rarely used edges in one atomic transaction.

        An edge is pruned when `metadata.deprecated` is true, or when its last access
        (else `updated_at`) is older than `days` and `access_count <= max_access_count`.
        `model_inferred` entities left with no edges by the prune are deleted with their
        embeddings, so no orphans leak. With `dry_run` nothing is written. Returns
        `(edges, entities)` removed (or that would be removed).
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)

        async def plan(conn: aiosqlite.Connection) -> tuple[list[int], list[int]]:
            async with conn.execute(
                "SELECT id, source_id, target_id, metadata, access_count, "
                "COALESCE(last_accessed_at, updated_at) FROM edges"
            ) as cur:
                rows = await cur.fetchall()
            doomed: set[int] = set()
            for edge_id, _, _, raw, access_count, ref in rows:
                if load_metadata(raw).get("deprecated") is True:
                    doomed.add(edge_id)
                    continue
                try:
                    stamp = datetime.fromisoformat(str(ref)) if ref else None
                except ValueError:
                    stamp = None
                if stamp is None:
                    continue
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                if stamp < cutoff and (access_count or 0) <= max_access_count:
                    doomed.add(edge_id)
            touched = {i for r in rows if r[0] in doomed for i in (r[1], r[2])}
            alive = {i for r in rows if r[0] not in doomed for i in (r[1], r[2])}
            orphans: list[int] = []
            for entity_id in sorted(touched - alive):
                async with conn.execute(
                    "SELECT 1 FROM entities WHERE id = ? AND origin = 'model_inferred'", (entity_id,)
                ) as cur:
                    if await cur.fetchone():
                        orphans.append(entity_id)
            return sorted(doomed), orphans

        if dry_run:
            edge_ids, orphan_ids = await plan(self.conn)
            return len(edge_ids), len(orphan_ids)
        async with self.transaction() as conn:
            edge_ids, orphan_ids = await plan(conn)
            for edge_id in edge_ids:
                await conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
            for entity_id in orphan_ids:
                if self.vec_enabled:
                    await conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
                await conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        return len(edge_ids), len(orphan_ids)

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
            item["metadata"] = load_metadata(item["metadata"])
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
        async with self.transaction() as conn:
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
        async with self.transaction() as conn:
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


    async def get_recent_episodes_for_entities(
        self,
        entity_ids: Sequence[int],
        max_age_days: float = 7.0,
        limit: int = 2,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest episodes within `max_age_days` that mention any of `entity_ids`.

        `created_at` is the episode timestamp rendered as a UTC `YYYY-MM-DD HH:MM:SS` string and
        `entities` is the episode's full decoded `extracted_entities` list. Episodes of
        `exclude_session_id` are skipped (the live transcript already carries them).
        """
        ids = [int(i) for i in dict.fromkeys(entity_ids)]
        if not ids or limit < 1 or max_age_days <= 0:
            return []
        marks = ",".join("?" for _ in ids)
        sql = (
            "SELECT id, narrative, extracted_entities, "
            "datetime(timestamp, 'unixepoch') AS created_at FROM episodes "
            "WHERE timestamp >= ? AND CASE WHEN json_valid(extracted_entities) THEN EXISTS ("
            f"SELECT 1 FROM json_each(episodes.extracted_entities) WHERE json_each.value IN ({marks})"
            ") ELSE 0 END"
        )
        params: list[Any] = [time.time() - max_age_days * 86400.0, *ids]
        if exclude_session_id is not None:
            sql += " AND session_id != ?"
            params.append(exclude_session_id)
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(int(limit))
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": row["id"],
                "narrative": row["narrative"],
                "created_at": row["created_at"],
                "entities": json.loads(row["extracted_entities"]),
            }
            for row in rows
        ]

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
        async with self.transaction() as conn:
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
        async with self.transaction() as conn:
            cur = await conn.execute(
                "UPDATE entities SET summary = :summary, updated_at = CURRENT_TIMESTAMP "
                "WHERE name = :name COLLATE NOCASE AND (:conf > confidence OR (:conf = confidence "
                "AND (origin != 'user_explicit' OR :origin = 'user_explicit')))",
                {"summary": summary, "name": name, "conf": confidence, "origin": origin},
            )
            return cur.rowcount > 0
