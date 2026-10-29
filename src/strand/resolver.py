"""Context resolver: maps a prompt onto the entity graph and emits a compact, session-aware delta."""

import asyncio
import math
import re
from collections.abc import Iterable
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from strand.common import (
    DEFAULT_CONTINUITY_HORIZON_DAYS,
    DEFAULT_CONTINUITY_MAX_EPISODES,
    MIN_EDGE_CONFIDENCE,
    PERSONALITY_ARCHETYPES,
    aliases_of,
    clean_text,
    is_private,
    load_metadata,
)
from strand.db import DatabaseManager
from strand.embed import Embedder, EmbeddingUnavailable
from strand.logging import get_logger
from strand.session import SessionDelta, SessionLedger
from strand.stopwords import FUNCTION_WORDS, is_stop_word

MAX_HOPS_LIMIT = 2
MIN_TOKEN_LENGTH = 3  # shortest whole-word token allowed in the token-overlap fallback
_HEADER = "## Strand Memory"
ESTABLISHED_CONFIDENCE = 0.8  # facts at or above this are presented as established
MIN_EFFECTIVE_PRIORITY = 0.15  # with decay enabled, candidates below this are pruned
MAX_FREQUENCY_FACTOR = 1.5  # cap on the access-frequency boost
FREQUENCY_STEP = 0.05  # boost per recorded access
MAX_PERSONALITY_ENTITIES = 20  # cap on always-on personality nodes recalled per turn
PERSONA_ANCHOR_ID = -1  # sentinel ledger id: the configured persona prompt is injected once per session
# Personality sections in presentation order, hierarchically above working memory.
_PERSONALITY_SECTIONS = (
    ("trait", "[Persona Stance]"),
    ("directive", "[Operational Directives]"),
    ("preference", "[Interaction Preferences]"),
)
_WORKING_MEMORY = "[Working Memory]"
_CONTINUITY = "[Recent Continuity]"
CONTINUITY_OVERFETCH = 20  # spare candidates so seen/private episodes cannot starve the result
MAX_CONTINUITY_CHARS = 240  # cap on one episode narrative rendered into the prompt
_REALITY_CHECK = "[Reality Check - Attribute Discrepancy]"
_ATTRIBUTE_KEYWORDS = frozenset(
    {"equipped", "runs", "model", "spec", "color", "type", "engine", "transmission", "os", "arch", "version"}
)
# COG-1 contradiction categories: (regex, canonical class of a match). Matches are case-insensitive
# and token-bounded, so "v8" never fires inside "rev8" and "auto" only counts as a gearbox when
# followed by transmission/gearbox (a bare "auto repair" is not a spec claim).
_SPEC_CATEGORIES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"(?<!\w)(\d{1,2})[- ]speed(?!\w)", lambda m: m.group(1)),
        (
            r"(?<!\w)(?:manual|automatic|auto(?=[- ](?:transmission|gearbox|trans)\b))(?!\w)",
            lambda m: "manual" if m.group(0).casefold() == "manual" else "automatic",
        ),
        (r"(?<!\w)(?:[vi]|inline[- ]?)(6|8|10|12)(?!\w)", lambda m: m.group(1)),
        (r"(?<!\w)(intel|amd|arm)(?!\w)", lambda m: m.group(1).casefold()),
        (r"(?<!\w)(fedora|ubuntu|debian)(?!\w)", lambda m: m.group(1).casefold()),
        (
            r"(?<!\w)(diesel|petrol|gasoline)(?!\w)",
            lambda m: "diesel" if m.group(1).casefold() == "diesel" else "petrol",
        ),
    )
)
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
    try:
        half_life_ok = math.isfinite(half_life_days) and half_life_days > 0
    except TypeError:
        half_life_ok = False
    if not decay_enabled or not half_life_ok:
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


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """A query token that contradicts a stored attribute edge of a matched entity."""

    entity: str
    relation: str
    target: str
    token: str


def _spec_classes(text: str) -> list[tuple[int, str, str]]:
    """Per category index: (category, class label, matched text) for the first match of each class."""
    found: list[tuple[int, str, str]] = []
    for index, (pattern, label) in enumerate(_SPEC_CATEGORIES):
        seen: set[str] = set()
        for m in pattern.finditer(text):
            klass = label(m)
            if klass not in seen:
                seen.add(klass)
                found.append((index, klass, m.group(0)))
    return found


def _is_attribute_relation(relation: str) -> bool:
    rel = relation.casefold()
    return rel.startswith("has_") or bool(_ATTRIBUTE_KEYWORDS & set(re.split(r"[^a-z0-9]+", rel)))


def detect_discrepancies(entities: Iterable[Entity], edges: Iterable[Edge], query: str) -> list[Discrepancy]:
    """COG-1: attribute edges whose target (name + summary) contradicts a spec claimed in `query`.

    A category conflicts only when the stored fact and the query both state a value in it and
    share none ("manual transmission fluid" vs a stored 6-speed Manual is consistent).
    """
    by_id = {e.id: e for e in entities if e.archetype not in PERSONALITY_ARCHETYPES}
    claimed = _spec_classes(query)
    if not claimed:
        return []
    found: list[Discrepancy] = []
    for edge in edges:
        source, target = by_id.get(edge.source_id), by_id.get(edge.target_id)
        if source is None or target is None or not _is_attribute_relation(edge.relation):
            continue
        stored = _spec_classes(f"{target.name} {target.summary}")
        for index in dict.fromkeys(i for i, _, _ in stored):
            held = {k for i, k, _ in stored if i == index}
            asked = [(k, text) for i, k, text in claimed if i == index]
            if asked and not held & {k for k, _ in asked}:
                item = Discrepancy(_clean(source.name), _clean(edge.relation), _clean(target.name), _clean(asked[0][1]))
                if item not in found:
                    found.append(item)
    return found


def format_context_delta(
    entities: Iterable[Entity],
    edges: Iterable[Edge],
    delta: SessionDelta,
    persona_prompt: str = "",
    discrepancies: Iterable[Discrepancy] = (),
    continuity: Iterable[tuple[str, str]] = (),
) -> str:
    """Render a session delta.

    Personality nodes (trait/directive/preference) are grouped under `[Persona Stance]`,
    `[Operational Directives]` and `[Interaction Preferences]`, above the factual graph. When
    any personality section is present the factual part is labelled `[Working Memory]`; with
    none, the output is the plain `Entities:`/`Relations:` block. Empty sections are omitted.
    """
    entities = list(entities)
    names = {e.id: _clean(e.name) for e in entities}
    new_entities = set(delta.entity_ids)
    new_edges = set(delta.edge_ids)
    personality_ids = {e.id for e in entities if e.archetype in PERSONALITY_ARCHETYPES}

    def describe(entity: Entity, with_archetype: bool) -> str:
        line = f"- {names[entity.id]}"
        if with_archetype:
            line += f" ({_clean(entity.archetype)})"
        summary = _clean(entity.summary)
        line = f"{line}: {summary}" if summary else line
        return line + _marker(entity.confidence)

    entity_lines = [
        describe(e, True) for e in entities if e.id in new_entities and e.id not in personality_ids
    ]
    edge_lines = [
        f"- {names[edge.source_id]} -{_clean(edge.relation)}-> {names[edge.target_id]}"
        f"{_marker(edge.confidence)}"
        for edge in edges
        if edge.id in new_edges
        and edge.source_id not in personality_ids
        and edge.target_id not in personality_ids
    ]

    sections: list[str] = []
    anchor = _clean(persona_prompt) if PERSONA_ANCHOR_ID in new_entities else ""
    for archetype, title in _PERSONALITY_SECTIONS:
        body = [describe(e, False) for e in entities if e.id in new_entities and e.archetype == archetype]
        if archetype == "trait" and anchor:
            body.insert(0, anchor)
        if body:
            sections += [title, *body]

    lines = [_HEADER]
    alerts = list(discrepancies)
    if alerts:
        lines.append(_REALITY_CHECK)
        for d in alerts:
            lines += [
                f"- Discrepancy on {d.entity}:",
                f"  * Stored fact: [{d.relation}] -> {d.target}",
                f"  * Query phrasing: mentions '{d.token}'",
            ]
        lines.append("- Clarify or challenge this discrepancy rather than assuming the contradictory spec.")
    lines += sections
    recent = [f"- {day}: {text}" for day, text in continuity]
    if recent:
        lines += [_CONTINUITY, *recent]
    if (sections or recent) and (entity_lines or edge_lines):
        lines.append(_WORKING_MEMORY)
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
        persona_prompt: str = "",
        continuity_horizon_days: float = DEFAULT_CONTINUITY_HORIZON_DAYS,
        continuity_max_episodes: int = DEFAULT_CONTINUITY_MAX_EPISODES,
    ) -> None:
        if not 1 <= max_hops <= MAX_HOPS_LIMIT:
            raise ValueError(f"max_hops must be between 1 and {MAX_HOPS_LIMIT}, got {max_hops}")
        if edge_limit < 1 or max_entities < 1:
            raise ValueError("edge_limit and max_entities must be at least 1")
        if decay_half_life_days <= 0:
            raise ValueError(f"decay_half_life_days must be positive, got {decay_half_life_days}")
        self.db = db
        self.persona_prompt = persona_prompt.strip() if isinstance(persona_prompt, str) else ""
        self.continuity_horizon_days = float(continuity_horizon_days)
        self.continuity_max_episodes = int(continuity_max_episodes)
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

    async def match_entities(self, query: str, allow_private: bool = False) -> list[Entity]:
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

    async def gather(self, query: str, allow_private: bool = False) -> tuple[list[Entity], list[Edge]]:
        """Matched entities plus their `max_hops` neighbourhood, de-duplicated across roots.

        Unless `allow_private`, private entities and every edge touching them are omitted.
        """
        entities, edges, _ = await self._gather_with_roots(query, allow_private)
        return entities, edges

    async def _gather_with_roots(
        self, query: str, allow_private: bool
    ) -> tuple[list[Entity], list[Edge], list[Entity]]:
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
        return list(entities.values()), list(edges.values()), roots

    async def recent_continuity(
        self, roots: list[Entity], session_id: str, allow_private: bool
    ) -> tuple[list[int], list[tuple[str, str]]]:
        """Novel recent episodes for the matched `roots`: (episode ids, (date, narrative) pairs).

        Episodes already shown to `session_id` and, unless `allow_private`, episodes that touch
        any private entity are dropped. Fails open to "no continuity" on a database error.
        """
        if not roots or self.continuity_max_episodes < 1 or self.continuity_horizon_days <= 0:
            return [], []
        seen = self.ledger.seen_episode_ids(session_id)
        try:
            rows = await self.db.get_recent_episodes_for_entities(
                [e.id for e in roots],
                max_age_days=self.continuity_horizon_days,
                limit=self.continuity_max_episodes + len(seen) + CONTINUITY_OVERFETCH,
                exclude_session_id=session_id,
            )
            hidden = set() if allow_private else await self._private_ids()
        except Exception:
            logger.warning("episodic lookup failed; continuity skipped", exc_info=True)
            return [], []
        ids: list[int] = []
        lines: list[tuple[str, str]] = []
        for row in rows:
            if row["id"] in seen or hidden.intersection(i for i in row["entities"] if isinstance(i, int)):
                continue
            text = _clean(row["narrative"])
            if len(text) > MAX_CONTINUITY_CHARS:
                text = text[: MAX_CONTINUITY_CHARS - 3].rstrip() + "..."
            ids.append(row["id"])
            lines.append((str(row["created_at"])[:10], text))
            if len(ids) >= self.continuity_max_episodes:
                break
        return ids, lines

    async def personality_entities(self, allow_private: bool = False) -> list[Entity]:
        """Active trait/directive/preference nodes, recalled on every turn regardless of the query.

        A node is active when it has no edges (seeded directly) or at least one live edge;
        a node whose every edge was deprecated has been superseded and is omitted.
        """
        marks = ",".join("?" for _ in PERSONALITY_ARCHETYPES)
        async with self.db.conn.execute(
            "SELECT id, name, archetype, summary, metadata, origin, confidence, "
            "access_count, last_accessed_at, created_at FROM entities "
            f"WHERE archetype IN ({marks}) ORDER BY confidence DESC, id",
            tuple(sorted(PERSONALITY_ARCHETYPES)),
        ) as cur:
            rows = await cur.fetchall()
        if not allow_private:
            rows = [r for r in rows if not is_private(r["archetype"], r["metadata"])]
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ",".join("?" for _ in ids)
        async with self.db.conn.execute(
            "SELECT source_id, target_id, weight, confidence, metadata FROM edges "
            f"WHERE source_id IN ({marks}) OR target_id IN ({marks})",
            (*ids, *ids),
        ) as cur:
            edge_rows = await cur.fetchall()
        has_edge: set[int] = set()
        live: set[int] = set()
        for e in edge_rows:
            active = _is_active_edge(
                {"weight": e["weight"], "confidence": e["confidence"], "metadata": load_metadata(e["metadata"])}
            )
            for end in (e["source_id"], e["target_id"]):
                has_edge.add(end)
                if active:
                    live.add(end)
        keep = [r for r in rows if r["id"] not in has_edge or r["id"] in live]
        return [_entity_from_row(r) for r in keep[:MAX_PERSONALITY_ENTITIES]]

    async def resolve(self, query: str, session_id: str, allow_private: bool = False) -> str:
        """Markdown block of facts new to `session_id`, or "" if there is nothing to add.

        Facts are marked as seen as soon as they are returned, so callers should
        inject the result into the prompt (or `ledger.reset` the session on failure).
        """
        entities, edges, roots = await self._gather_with_roots(query, allow_private)
        discrepancies = detect_discrepancies(entities, edges, query)
        known = {e.id for e in entities}
        entities += [e for e in await self.personality_entities(allow_private) if e.id not in known]
        if not entities and not self.persona_prompt:
            return ""
        if entities:
            try:
                await self.db.touch_access([e.id for e in entities], [e.id for e in edges])
            except Exception:
                logger.warning("touch_access failed; recall continues", exc_info=True)
        anchor = [PERSONA_ANCHOR_ID] if self.persona_prompt else []
        delta = self.ledger.filter_new(
            session_id, [*anchor, *(e.id for e in entities)], (e.id for e in edges)
        )
        episode_ids, continuity = await self.recent_continuity(roots, session_id, allow_private)
        if episode_ids:
            self.ledger.mark_episodes_seen(session_id, episode_ids)
        if delta.is_empty and not discrepancies and not continuity:
            return ""  # a discrepancy is re-flagged whenever the query repeats it, ledger unchanged
        return format_context_delta(
            entities, edges, delta, self.persona_prompt, discrepancies, continuity
        )
