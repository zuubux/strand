"""Context resolver: maps a prompt onto the entity graph and emits a compact, session-aware delta."""

import json
from collections.abc import Iterable
from dataclasses import dataclass

from cortext.db import DatabaseManager
from cortext.session import SessionDelta, SessionLedger

MAX_HOPS_LIMIT = 2
_HEADER = "## Cortext Memory"


@dataclass(frozen=True, slots=True)
class Entity:
    id: int
    name: str
    archetype: str
    summary: str


@dataclass(frozen=True, slots=True)
class Edge:
    id: int
    source_id: int
    target_id: int
    relation: str


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


def _clean(text: object) -> str:
    """Collapse all whitespace so every fact stays on a single line."""
    return " ".join(str(text or "").split())


def _format_delta(entities: Iterable[Entity], edges: Iterable[Edge], delta: SessionDelta) -> str:
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
        entity_lines.append(f"{line}: {summary}" if summary else line)

    edge_lines = [
        f"- {names[edge.source_id]} -{_clean(edge.relation)}-> {names[edge.target_id]}"
        for edge in edges
        if edge.id in new_edges
    ]

    lines = [_HEADER]
    if entity_lines:
        lines += ["Entities:", *entity_lines]
    if edge_lines:
        lines += ["Relations:", *edge_lines]
    return "\n".join(lines)


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

    async def match_entities(self, query: str) -> list[Entity]:
        """Entities whose name or metadata alias appears in `query` (case-insensitive, whole word).

        Results are ordered by first mention in the query and capped at `max_entities`.
        """
        haystack = query.casefold()
        if not haystack.strip():
            return []
        async with self.db.conn.execute(
            "SELECT id, name, archetype, summary, metadata FROM entities ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()

        hits: list[tuple[int, int, Entity]] = []
        for row in rows:
            terms = (t.strip().casefold() for t in (row["name"], *_aliases(row["metadata"])))
            positions = [p for t in terms if t and (p := _first_match(haystack, t)) >= 0]
            if positions:
                entity = Entity(row["id"], row["name"], row["archetype"], row["summary"] or "")
                hits.append((min(positions), entity.id, entity))
        hits.sort(key=lambda hit: hit[:2])
        return [entity for _, _, entity in hits[: self.max_entities]]

    async def gather(self, query: str) -> tuple[list[Entity], list[Edge]]:
        """Matched entities plus their `max_hops` neighbourhood, de-duplicated across roots."""
        roots = await self.match_entities(query)
        entities = {e.id: e for e in roots}
        edges: dict[int, Edge] = {}
        for root in roots:
            rows = await self.db.get_entity_subgraph(
                root.id, max_hops=self.max_hops, limit=self.edge_limit
            )
            for row in rows:
                edges.setdefault(
                    row["edge_id"],
                    Edge(row["edge_id"], row["source_id"], row["target_id"], row["relation"]),
                )
                for side in ("source", "target"):
                    entity_id = row[f"{side}_id"]
                    if entity_id not in entities:
                        entities[entity_id] = Entity(
                            entity_id,
                            row[f"{side}_name"],
                            row[f"{side}_archetype"],
                            row[f"{side}_summary"] or "",
                        )
        return list(entities.values()), list(edges.values())

    async def resolve(self, query: str, session_id: str) -> str:
        """Markdown block of facts new to `session_id`, or "" if there is nothing to add.

        Facts are marked as seen as soon as they are returned, so callers should
        inject the result into the prompt (or `ledger.reset` the session on failure).
        """
        entities, edges = await self.gather(query)
        if not entities:
            return ""
        delta = self.ledger.filter_new(
            session_id, (e.id for e in entities), (e.id for e in edges)
        )
        if delta.is_empty:
            return ""
        return _format_delta(entities, edges, delta)

