"""Context resolver: maps a prompt onto the entity graph and emits a compact, session-aware delta."""

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from cortext.db import DatabaseManager
from cortext.embed import Embedder
from cortext.session import SessionDelta, SessionLedger
from cortext.stopwords import FUNCTION_WORDS, is_stop_word

MAX_HOPS_LIMIT = 2
MIN_TOKEN_LENGTH = 3  # shortest whole-word token allowed in the token-overlap fallback
_HEADER = "## Cortext Memory"
ESTABLISHED_CONFIDENCE = 0.8  # facts at or above this are presented as established


@dataclass(frozen=True, slots=True)
class Entity:
    id: int
    name: str
    archetype: str
    summary: str
    origin: str = "user_explicit"
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class Edge:
    id: int
    source_id: int
    target_id: int
    relation: str
    origin: str = "user_explicit"
    confidence: float = 1.0


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
    if not raw_metadata or "aliases" not in raw_metadata:
        return []
    try:
        meta = json.loads(raw_metadata)
    except ValueError:
        return []
    aliases = meta.get("aliases") if isinstance(meta, dict) else None
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list):
        return []
    return [a for a in aliases if isinstance(a, str)]


def is_private(archetype: str | None, raw_metadata: str | None) -> bool:
    """Private entities have archetype "private" or metadata {"private": true}."""
    if archetype == "private":
        return True
    if not raw_metadata or "private" not in raw_metadata:
        return False
    try:
        meta = json.loads(raw_metadata)
    except ValueError:
        return False
    return isinstance(meta, dict) and meta.get("private") is True


def _clean(text: object) -> str:
    """Collapse all whitespace so every fact stays on a single line."""
    return " ".join(str(text or "").split())


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
    ) -> None:
        if not 1 <= max_hops <= MAX_HOPS_LIMIT:
            raise ValueError(f"max_hops must be between 1 and {MAX_HOPS_LIMIT}, got {max_hops}")
        if edge_limit < 1 or max_entities < 1:
            raise ValueError("edge_limit and max_entities must be at least 1")
        self.db = db
        self.ledger = ledger if ledger is not None else SessionLedger()
        self.max_hops = max_hops
        self.edge_limit = edge_limit
        self.max_entities = max_entities
        self.embedder = embedder
        self.distance_threshold = distance_threshold

    async def match_entities(self, query: str, allow_private: bool = True) -> list[Entity]:
        """Entities whose name or metadata alias appears in `query` (case-insensitive, whole word).

        Results are ordered by first mention in the query and capped at `max_entities`.
        """
        haystack = query.casefold()
        if not haystack.strip():
            return []
        async with self.db.conn.execute(
            "SELECT id, name, archetype, summary, metadata, origin, confidence FROM entities ORDER BY id"
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
                entity = Entity(
                    row["id"], row["name"], row["archetype"], row["summary"] or "", row["origin"], row["confidence"]
                )
                hits.append((min(positions), entity.id, entity))
        exact_ids = {entity.id for _, _, entity in hits}
        token_hits = [h for h in self._token_hits(haystack, rows) if h[2].id not in exact_ids]
        # Rank: higher confidence first (user facts beat model-inferred noise), then exact
        # before token matches, then first mention in the query, then id.
        ranked = [(0, *h) for h in hits] + [(1, *h) for h in token_hits]
        ranked.sort(key=lambda r: (-r[3].confidence, r[0], r[1], r[2]))
        matched = [entity for *_, entity in ranked[: self.max_entities]]
        if self.embedder is not None and len(matched) < self.max_entities:
            matched += await self._semantic_hits(query, rows, {e.id for e in matched})
        return matched[: self.max_entities]

    async def _semantic_hits(self, query: str, rows: Iterable, taken: set[int]) -> list[Entity]:
        """Tier 3: vector KNN over entity embeddings, nearest first, excluding `taken`.

        `rows` is already privacy-filtered, so private entities never surface here
        unless the caller allowed them.
        """
        vector = await asyncio.to_thread(self.embedder.embed, query)
        ids = await self.db.find_similar_entities(
            vector, limit=self.max_entities * 4 + len(taken), distance_threshold=self.distance_threshold
        )
        by_id = {r["id"]: r for r in rows}
        hits: list[Entity] = []
        for entity_id in ids:
            row = by_id.get(entity_id)
            if row is None or entity_id in taken:
                continue
            hits.append(
                Entity(row["id"], row["name"], row["archetype"], row["summary"] or "", row["origin"], row["confidence"])
            )
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
                entity = Entity(
                    row["id"], row["name"], row["archetype"], row["summary"] or "", row["origin"], row["confidence"]
                )
                hits.append((min(positions), entity.id, entity))
        return hits

    async def _private_ids(self) -> set[int]:
        async with self.db.conn.execute("SELECT id, archetype, metadata FROM entities") as cur:
            rows = await cur.fetchall()
        return {r["id"] for r in rows if is_private(r["archetype"], r["metadata"])}

    async def gather(self, query: str, allow_private: bool = True) -> tuple[list[Entity], list[Edge]]:
        """Matched entities plus their `max_hops` neighbourhood, de-duplicated across roots.

        Unless `allow_private`, private entities and every edge touching them are omitted.
        """
        roots = await self.match_entities(query, allow_private)
        hidden = set() if allow_private else await self._private_ids()
        entities = {e.id: e for e in roots}
        edges: dict[int, Edge] = {}
        for root in roots:
            rows = await self.db.get_entity_subgraph(
                root.id, max_hops=self.max_hops, limit=self.edge_limit
            )
            for row in rows:
                if row["source_id"] in hidden or row["target_id"] in hidden:
                    continue
                edges.setdefault(
                    row["edge_id"],
                    Edge(
                        row["edge_id"],
                        row["source_id"],
                        row["target_id"],
                        row["relation"],
                        row["origin"],
                        row["confidence"],
                    ),
                )
                for side in ("source", "target"):
                    entity_id = row[f"{side}_id"]
                    if entity_id not in entities:
                        entities[entity_id] = Entity(
                            entity_id,
                            row[f"{side}_name"],
                            row[f"{side}_archetype"],
                            row[f"{side}_summary"] or "",
                            row[f"{side}_origin"],
                            row[f"{side}_confidence"],
                        )
        return list(entities.values()), list(edges.values())

    async def resolve(self, query: str, session_id: str, allow_private: bool = False) -> str:
        """Markdown block of facts new to `session_id`, or "" if there is nothing to add.

        Facts are marked as seen as soon as they are returned, so callers should
        inject the result into the prompt (or `ledger.reset` the session on failure).
        """
        entities, edges = await self.gather(query, allow_private)
        if not entities:
            return ""
        delta = self.ledger.filter_new(
            session_id, (e.id for e in entities), (e.id for e in edges)
        )
        if delta.is_empty:
            return ""
        return _format_delta(entities, edges, delta)

