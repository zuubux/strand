"""Context resolver: maps a prompt onto the entity graph and emits a compact, session-aware delta."""

import asyncio
import math
import re
from collections.abc import Iterable
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cortext.common import MIN_EDGE_CONFIDENCE, aliases_of, clean_text, is_private
from cortext.db import DatabaseManager
from cortext.embed import Embedder, EmbeddingUnavailable
from cortext.logging import get_logger
from cortext.session import SessionDelta, SessionLedger
from cortext.stopwords import FUNCTION_WORDS, is_stop_word

MAX_HOPS_LIMIT = 2
MIN_TOKEN_LENGTH = 3  # shortest whole-word token allowed in the token-overlap fallback
_HEADER = "## Cortext Memory"
ESTABLISHED_CONFIDENCE = 0.8  # facts at or above this are presented as established
MIN_EFFECTIVE_PRIORITY = 0.15  # with decay enabled, candidates below this are pruned
MAX_FREQUENCY_FACTOR = 1.5  # cap on the access-frequency boost
FREQUENCY_STEP = 0.05  # boost per recorded access
logger = get_logger("resolver")


@dataclass(frozen=True, slots=True)
class Entity:
    id: int
    name: str
    archetype: str
    summary: str
    origin: str = "user_explicit"
    confidence: float = 1.0
    access_count: int = field(default=0, compare=False)
    last_accessed_at: str | None = field(default=None, compare=False)
    created_at: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Edge:
    id: int
    source_id: int
    target_id: int
    relation: str
    origin: str = "user_explicit"
    confidence: float = 1.0
    weight: float = field(default=1.0, compare=False)
    access_count: int = field(default=0, compare=False)
    last_accessed_at: str | None = field(default=None, compare=False)
    created_at: str | None = field(default=None, compare=False)


def _parse_time(value: str | datetime | None) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP (naive UTC) or ISO text -> aware UTC datetime; None if unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def effective_priority(
    confidence: float,
    weight: float,
    access_count: int = 0,
    last_accessed_at: str | datetime | None = None,
    created_at: str | datetime | None = None,
    *,
    now: datetime,
    decay_enabled: bool = False,
    half_life_days: float = 30.0,
) -> float:
    """Retrieval priority of a fact.

    Without decay: `confidence * weight`. With decay, additionally multiplied by
    `2 ** (-max(0, days_since_reference) / half_life_days)` (reference = last access, else
    creation) and `min(1.5, 1 + 0.05 * access_count)`. A fact with no usable timestamp is
    treated as fresh.
    """
    base = confidence * weight
    if not decay_enabled or not math.isfinite(half_life_days) or half_life_days <= 0:
        return base
    ref = _parse_time(last_accessed_at) or _parse_time(created_at)
    delta_days = (now - ref).total_seconds() / 86400.0 if ref is not None else 0.0
    decay_factor = 2.0 ** (-max(0.0, delta_days) / half_life_days)
    frequency_factor = min(MAX_FREQUENCY_FACTOR, 1.0 + FREQUENCY_STEP * (access_count or 0))
    return base * decay_factor * frequency_factor


def _entity_from_row(row, prefix: str = "") -> Entity:
    """Build an Entity from a row; `prefix` selects the `source_`/`target_` columns of a subgraph row."""
    def col(name: str):
        return row[f"{prefix}{name}"]

    return Entity(
        col("id"),
        col("name"),
        col("archetype"),
        col("summary") or "",
        col("origin"),
        col("confidence"),
        col("access_count"),
        col("last_accessed_at"),
        col("created_at"),
    )


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _first_match(haystack: str, term: str) -> int:
    """Index of the first whole-word occurrence of `term` in `haystack`, else -1.

    Both arguments must already be casefolded. A word boundary is only enforced on
    a side where the term itself starts/ends with a word character, so names like
    "C++" still match before punctuation.
    """
    start = 0
    while (i := haystack.find(term, start)) != -1:
        end = i + len(term)
        before_ok = i == 0 or not _is_word_char(term[0]) or not _is_word_char(haystack[i - 1])
        after_ok = end == len(haystack) or not _is_word_char(term[-1]) or not _is_word_char(haystack[end])
        if before_ok and after_ok:
            return i
        start = i + 1
    return -1


def _aliases(raw_metadata: str | None) -> list[str]:
    """Aliases live in entity metadata as {"aliases": [...]}; tolerate anything else."""
    return aliases_of(raw_metadata)


def _is_active_edge(row: dict) -> bool:
    """Deprecated/severed edges (weight <= 0, confidence < 0.2, metadata.deprecated) are never recalled."""
    if row["weight"] <= 0.0 or row["confidence"] < MIN_EDGE_CONFIDENCE:
        return False
    meta = row.get("metadata")
    return not (isinstance(meta, dict) and meta.get("deprecated") is True)


def _clean(text: object) -> str:
    """Collapse all whitespace so every fact stays on a single line."""
    return clean_text(text)


def _marker(confidence: float) -> str:
    """Provisional-fact indicator for the downstream LLM; empty for established facts."""
    if confidence >= ESTABLISHED_CONFIDENCE:
        return ""
    return f" [inferred, confidence: {confidence:.2f}]"


def format_context_delta(entities: Iterable[Entity], edges: Iterable[Edge], delta: SessionDelta) -> str:
    entities = list(entities)
    names = {e.id: _clean(e.name) for e in entities}
    new_entities = set(delta.entity_ids)
    new_edges = set(delta.edge_ids)

    entity_lines = []
    for entity in entities:
        if entity.id not in new_entities:
            continue
        line = f"- {names[entity.id]} ({_clean(entity.archetype)})"
        summary = _clean(entity.summary)
        line = f"{line}: {summary}" if summary else line
        entity_lines.append(line + _marker(entity.confidence))

    edge_lines = [
        f"- {names[edge.source_id]} -{_clean(edge.relation)}-> {names[edge.target_id]}"
        f"{_marker(edge.confidence)}"
        for edge in edges
        if edge.id in new_edges
    ]

    lines = [_HEADER]
    if entity_lines:
        lines += ["Entities:", *entity_lines]
    if edge_lines:
        lines += ["Relations:", *edge_lines]
    return "\n".join(lines)


_format_delta = format_context_delta


class ContextResolver:
    """Resolves a prompt to the graph neighbourhood the LLM has not seen yet this session."""

    def __init__(
        self,
        db: DatabaseManager,
        ledger: SessionLedger | None = None,
        *,
        max_hops: int = MAX_HOPS_LIMIT,
        edge_limit: int = 10,
        max_entities: int = 5,
        embedder: Embedder | None = None,
        distance_threshold: float = 1.0,
        decay_enabled: bool = False,
        decay_half_life_days: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= max_hops <= MAX_HOPS_LIMIT:
            raise ValueError(f"max_hops must be between 1 and {MAX_HOPS_LIMIT}, got {max_hops}")
        if edge_limit < 1 or max_entities < 1:
            raise ValueError("edge_limit and max_entities must be at least 1")
        if decay_half_life_days <= 0:
            raise ValueError(f"decay_half_life_days must be positive, got {decay_half_life_days}")
        self.db = db
        self.decay_enabled = decay_enabled
        self.decay_half_life_days = float(decay_half_life_days)
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self.ledger = ledger if ledger is not None else SessionLedger()
        self.max_hops = max_hops
        self.edge_limit = edge_limit
        self.max_entities = max_entities
        self.embedder = embedder
        self.distance_threshold = distance_threshold

    def _priority(self, item: Entity | Edge, now: datetime) -> float:
        weight = getattr(item, "weight", 1.0)
        return effective_priority(
            item.confidence,
            weight,
            item.access_count,
            item.last_accessed_at,
            item.created_at,
            now=now,
            decay_enabled=self.decay_enabled,
            half_life_days=self.decay_half_life_days,
        )

    async def match_entities(self, query: str, allow_private: bool = True) -> list[Entity]:
        """Entities whose name or metadata alias appears in `query` (case-insensitive, whole word).

        Results are ordered by first mention in the query and capped at `max_entities`.
        """
        haystack = query.casefold()
        if not haystack.strip():
            return []
        async with self.db.conn.execute(
            "SELECT id, name, archetype, summary, metadata, origin, confidence, "
            "access_count, last_accessed_at, created_at FROM entities ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        if not allow_private:
            rows = [r for r in rows if not is_private(r["archetype"], r["metadata"])]

        hits: list[tuple[int, int, Entity]] = []
        for row in rows:
            terms = (
                t.strip().casefold()
                for t in (row["name"], *_aliases(row["metadata"]))
                if not (row["archetype"] == "concept" and len(t.split()) == 1 and is_stop_word(t))
            )
            positions = [p for t in terms if t and (p := _first_match(haystack, t)) >= 0]
            if positions:
                entity = _entity_from_row(row)
                hits.append((min(positions), entity.id, entity))
        exact_ids = {entity.id for _, _, entity in hits}
        token_hits = [h for h in self._token_hits(haystack, rows) if h[2].id not in exact_ids]
        # Rank: higher confidence first (user facts beat model-inferred noise), then exact
        # before token matches, then first mention in the query, then id.
        ranked = [(0, *h) for h in hits] + [(1, *h) for h in token_hits]
        if self.decay_enabled:
            now = self._clock()
            prio = {r[3].id: self._priority(r[3], now) for r in ranked}
            ranked = [r for r in ranked if prio[r[3].id] >= MIN_EFFECTIVE_PRIORITY]
            ranked.sort(key=lambda r: (-prio[r[3].id], r[0], r[1], r[2]))
        else:
            ranked.sort(key=lambda r: (-r[3].confidence, r[0], r[1], r[2]))
        matched = [entity for *_, entity in ranked[: self.max_entities]]
        if self.embedder is not None and self.db.vec_enabled and len(matched) < self.max_entities:
            matched += await self._semantic_hits(query, rows, {e.id for e in matched})
        return matched[: self.max_entities]

    async def _semantic_hits(self, query: str, rows: Iterable, taken: set[int]) -> list[Entity]:
        """Tier 3: vector KNN over entity embeddings, nearest first, excluding `taken`.

        `rows` is already privacy-filtered, so private entities never surface here
        unless the caller allowed them.
        """
        try:
            vector = await asyncio.to_thread(self.embedder.embed, query)
        except EmbeddingUnavailable:
            return []  # lexical-only fallback; the embedder already logged the warning
        ids = await self.db.find_similar_entities(
            vector, limit=self.max_entities * 4 + len(taken), distance_threshold=self.distance_threshold
        )
        by_id = {r["id"]: r for r in rows}
        hits: list[Entity] = []
        for entity_id in ids:
            row = by_id.get(entity_id)
            if row is None or entity_id in taken:
                continue
            entity = _entity_from_row(row)
            if self.decay_enabled and self._priority(entity, self._clock()) < MIN_EFFECTIVE_PRIORITY:
                continue
            hits.append(entity)
        return hits[: self.max_entities - len(taken)]

    @staticmethod
    def _token_hits(haystack: str, rows: Iterable) -> list[tuple[int, int, Entity]]:
        """Fallback: whole-word (`\\b`) overlap between query words and name/alias tokens."""
        hits: list[tuple[int, int, Entity]] = []
        for row in rows:
            tokens = {
                w.casefold()
                for term in (row["name"], *_aliases(row["metadata"]))
                for w in term.split()
            }
            positions = []
            for token in tokens:
                token = token.strip(" \t\n.,;:!?\"'()[]{}")
                if len(token) < MIN_TOKEN_LENGTH or is_stop_word(token) or token in FUNCTION_WORDS:
                    continue
                if (m := re.search(rf"\b{re.escape(token)}\b", haystack)) is not None:
                    positions.append(m.start())
            if positions:
                entity = _entity_from_row(row)
                hits.append((min(positions), entity.id, entity))
        return hits

    async def _private_ids(self) -> set[int]:
        async with self.db.conn.execute("SELECT id, archetype, metadata FROM entities") as cur:
            rows = await cur.fetchall()
        return {r["id"] for r in rows if is_private(r["archetype"], r["metadata"])}

    @staticmethod
    def _edge_from_row(row) -> Edge:
        return Edge(
            row["edge_id"],
            row["source_id"],
            row["target_id"],
            row["relation"],
            row["origin"],
            row["confidence"],
            row["weight"],
            row["edge_access_count"],
            row["edge_last_accessed_at"],
            row["edge_updated_at"],
        )

    async def gather(self, query: str, allow_private: bool = True) -> tuple[list[Entity], list[Edge]]:
        """Matched entities plus their `max_hops` neighbourhood, de-duplicated across roots.

        Unless `allow_private`, private entities and every edge touching them are omitted.
        """
        roots = await self.match_entities(query, allow_private)
        hidden = set() if allow_private else await self._private_ids()
        entities = {e.id: e for e in roots}
        edges: dict[int, Edge] = {}
        now = self._clock()
        for root in roots:
            rows = await self.db.get_entity_subgraph(
                root.id, max_hops=self.max_hops, limit=self.edge_limit, active_only=True
            )
            rows = [r for r in rows if _is_active_edge(r)]
            if self.decay_enabled:
                scored = [(self._priority(self._edge_from_row(r), now), r) for r in rows]
                scored = [(p, r) for p, r in scored if p >= MIN_EFFECTIVE_PRIORITY]
                scored.sort(key=lambda pr: (pr[1]["hop"], -pr[0], pr[1]["edge_id"]))
                rows = [r for _, r in scored]
            for row in rows:
                if row["source_id"] in hidden or row["target_id"] in hidden:
                    continue
                edges.setdefault(row["edge_id"], self._edge_from_row(row))
                for side in ("source", "target"):
                    entity_id = row[f"{side}_id"]
                    if entity_id not in entities:
                        entities[entity_id] = _entity_from_row(row, f"{side}_")
        return list(entities.values()), list(edges.values())

    async def resolve(self, query: str, session_id: str, allow_private: bool = False) -> str:
        """Markdown block of facts new to `session_id`, or "" if there is nothing to add.

        Facts are marked as seen as soon as they are returned, so callers should
        inject the result into the prompt (or `ledger.reset` the session on failure).
        """
        entities, edges = await self.gather(query, allow_private)
        if not entities:
            return ""
        try:
            await self.db.touch_access([e.id for e in entities], [e.id for e in edges])
        except Exception:
            logger.warning("touch_access failed; recall continues", exc_info=True)
        delta = self.ledger.filter_new(
            session_id, (e.id for e in entities), (e.id for e in edges)
        )
        if delta.is_empty:
            return ""
        return _format_delta(entities, edges, delta)

