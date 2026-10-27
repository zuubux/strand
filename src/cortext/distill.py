"""Asynchronous distillation: turn a finished exchange into entities, edges and an episode."""

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from .db import DatabaseManager
from .logging import get_logger
from .stopwords import is_filler_name, is_stop_word, is_valid_entity_name, name_tokens

logger = get_logger("distill")

# An extraction is a dict: {"entities": [{"name", "archetype"?, "summary"?}],
# "edges": [{"source", "target", "relation", "weight"?}], "summary": str}. Entities and
# edges may also carry "origin" and "confidence"; when absent they are treated as
# model_inferred at MODEL_CONFIDENCE so unattributed extractions never override user facts.
Extraction = dict[str, Any]
Extractor = Callable[[str, str], Extraction | Awaitable[Extraction]]

DEFAULT_ARCHETYPE = "concept"
VALID_ARCHETYPES = frozenset({"person", "project", "system", "vehicle", "concept"})
_MAX_SUMMARY = 300

USER_ORIGIN = "user_explicit"
MODEL_ORIGIN = "model_inferred"
USER_CONFIDENCE = 1.0
MODEL_CONFIDENCE = 0.6  # default confidence for facts the assistant (not the user) asserted

_NAME = r"[A-Z][\w\-]*(?:\s+[A-Z][\w\-]*)*"
_RELATIONS = (
    "works at|works on|works for|reports to|manages|leads|owns|uses|maintains|"
    "drives|lives in|depends on|is part of|belongs to"
)
_EDGE_RE = re.compile(rf"\b({_NAME})\s+({_RELATIONS})\s+({_NAME})")
_IS_RE = re.compile(rf"\b({_NAME})\s+is\s+((?:a|an|the)\s+[^.!?\n]+)")
_NAME_RE = re.compile(rf"\b{_NAME}")
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_HEADING_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t].*)?$", re.MULTILINE)
_BOLD_LABEL_RE = re.compile(r"\*\*[^*\n]+:\*\*|\*\*[^*\n]+\*\*:")
_STOPWORDS = frozenset(
    "a an and are as at be but by can could did do does for from had has have he her his how i if in is it "
    "its just let me my no not of on or our please she so sure that the their then there they this to us "
    "was we what when where which who why will with would yes you your hi hello thanks thank ok okay "
    "here prep give tell make show write help also however".split()
)


_MIN_QUERY_WORDS = 4
_TRIVIAL_WORDS = frozenset(
    "hi hello hey yo hiya thanks thank thx ty cheers you ok okay k kk sure yes yep yeah no nope "
    "sounds good great cool nice fine alright right got it bye goodbye see later cya morning "
    "evening afternoon night there all please very much so lol".split()
)
_FACT_RES = (
    re.compile(r"\b(remember|note|keep in mind|don'?t forget|fyi)\b", re.IGNORECASE),
    re.compile(r"\bmy\s+\w+(?:\s+\w+)?\s+(?:is|are|was|were)\b", re.IGNORECASE),
    re.compile(r"\b\w+\s+(?:is|are)\s+\w+", re.IGNORECASE),
    re.compile(rf"\b{_NAME}\s+(?:{_RELATIONS})\b"),
)
_MY_FACT_RE = re.compile(
    r"\bmy\s+(?P<attr>[\w\- ]+?)\s+(?:is|are)\s+(?:a|an)\s+(?P<value>[^.!?,\n]+?)"
    r"(?:\s+with\s+(?:a|an)\s+(?P<part>[^.!?,\n]+?))?\s*(?=[.!?,\n]|$)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}\s+")
_VEHICLE_ATTR_RE = re.compile(r"\b(car|truck|bike|motorcycle|vehicle|van|suv)\b", re.IGNORECASE)
_ENGINE_RE = re.compile(r"\b(engine|motor|turbo|v\d{1,2}|[ilf]\d|flat[- ]?\d)\b|^[A-Z]\d{2}\b", re.IGNORECASE)
_IS_A_RE = re.compile(r"\b(?:is|are)\s+an?\s+\w", re.IGNORECASE)
_WITH_A_RE = re.compile(r"\bwith\s+an?\s+\w", re.IGNORECASE)
_ASSERTION_RES = (_FACT_RES[0], _FACT_RES[1], _EDGE_RE, _IS_RE, _IS_A_RE, _WITH_A_RE)
_BOILERPLATE_RESPONSES = frozenset(
    {
        "ok", "okay", "sure", "got it", "noted", "understood", "done", "yes", "no",
        "hi", "hello", "hi there", "hello there", "hey", "bye", "goodbye",
        "thanks", "thank you", "youre welcome", "you are welcome", "no problem",
        "sounds good", "great", "you got it", "my pleasure",
        "how can i help", "how can i help you", "how can i help you today",
        "hello how can i help you", "hello how can i help you today",
        "hi how can i help you", "hi how can i help you today",
        "hi there how can i help you today", "hello there how can i help you today",
        "how can i assist you", "how can i assist you today",
        "is there anything else i can help with", "anything else",
    }
)


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", (text or "").lower().replace("'", "")).split()


def is_substantive_exchange(query: str, response: str) -> bool:
    """Gate distillation: True only when the exchange carries real signal.

    Rejects empty input, short queries made solely of greetings/pleasantries/acknowledgments
    (unless they contain an explicit fact assertion) and empty or boilerplate responses.
    """
    if not (query or "").strip() or not (response or "").strip():
        return False
    words = _words(query)
    if not words:
        return False
    if len(words) < _MIN_QUERY_WORDS and all(w in _TRIVIAL_WORDS for w in words):
        if not any(p.search(query) for p in _FACT_RES):
            return False
    reply = _words(response)
    if not reply or " ".join(reply) in _BOILERPLATE_RESPONSES:
        return False
    return True


def _clean_name(raw: str) -> str:
    words = raw.split()
    while words and (words[0].lower() in _STOPWORDS or is_stop_word(words[0])):
        words.pop(0)
    return " ".join(words)


def strip_markdown_structure(text: str) -> str:
    """Remove Markdown heading lines and bold section labels (`**Label:**`) from assistant text.

    Headings are formatting, not facts: their words must never become graph entities.
    """
    return _BOLD_LABEL_RE.sub(" ", _HEADING_LINE_RE.sub("", text or ""))


def _first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    sentence = _SENTENCE_RE.split(text, maxsplit=1)[0] if text else ""
    return sentence[:limit]


def _narrative(query: str, response: str) -> str:
    asked = _first_sentence(query)
    answered = _first_sentence(response)
    parts = []
    if asked:
        parts.append(f"User asked: {asked}")
    if answered:
        parts.append(f"Assistant replied: {answered}")
    return " ".join(parts)


def _is_user_assertion(sentence: str) -> bool:
    """True for sentences where the user states a fact ("remember ...", "my X is ...", "A manages B")."""
    if sentence.rstrip().endswith("?"):
        return False
    return any(p.search(sentence) for p in _ASSERTION_RES)


def _split_user_assertions(query: str) -> tuple[str, str]:
    """Split a user query into (asserted-fact sentences, everything else)."""
    asserted, other = [], []
    for sentence in _SENTENCE_RE.split(query or ""):
        (asserted if _is_user_assertion(sentence) else other).append(sentence)
    return " ".join(asserted), " ".join(other)


def _pattern_extract(
    text: str,
    origin: str,
    confidence: float,
    entities: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Pattern-extract `text` into the shared accumulators, tagging new items with provenance."""
    tag = {"origin": origin, "confidence": confidence}
    local: set[str] = set()

    def add(name: str) -> None:
        local.add(name)
        entities.setdefault(name, {"name": name, **tag})

    for m in list(_MY_FACT_RE.finditer(text)):
        value = _YEAR_RE.sub("", m.group("value").strip())
        name = _clean_name(value)
        if not name or not (name[0].isupper() or name[0].isdigit()) or not is_valid_entity_name(name):
            continue
        attr = m.group("attr").strip()
        part = m.group("part")
        archetype = "vehicle" if _VEHICLE_ATTR_RE.search(attr) else "project" if "project" in attr.lower() else "concept"
        add(name)
        entities[name].setdefault("archetype", archetype)
        entities[name].setdefault("summary", f"My {attr}: {m.group('value').strip()}"[:_MAX_SUMMARY])
        part_name = _clean_name(part.strip()) if part else ""
        if part_name and part_name != name and is_valid_entity_name(part_name):
            add(part_name)
            entities[part_name].setdefault("archetype", "system")
            relation = "has_engine" if _ENGINE_RE.search(part_name) else "has_component"
            edges.append({"source": name, "target": part_name, "relation": relation, "weight": 1.0, **tag})
    # Consumed spans must not be re-scanned by the generic capitalised-phrase pass (no orphans).
    text = _MY_FACT_RE.sub(" ", text)

    for raw in _NAME_RE.findall(text):
        name = _clean_name(raw)
        if is_valid_entity_name(name) and name.lower() not in _STOPWORDS:
            add(name)

    for m in _IS_RE.finditer(text):
        name = _clean_name(m.group(1))
        # Never let a lower-confidence pass retag or re-summarize an already-asserted entity.
        if name in local and entities[name]["confidence"] <= confidence:
            entities[name]["summary"] = m.group(2).strip()[:_MAX_SUMMARY]
            entities[name].update(tag)

    for m in _EDGE_RE.finditer(text):
        source, target = _clean_name(m.group(1)), _clean_name(m.group(3))
        if source != target and is_valid_entity_name(source) and is_valid_entity_name(target):
            add(source)
            add(target)
            edges.append(
                {"source": source, "target": target, "relation": m.group(2).lower().replace(" ", "_"), **tag}
            )


def heuristic_extract(query: str, response: str) -> Extraction:
    """Lightweight fallback extractor.

    Prefers a structured JSON block in the response (```json {...}```); otherwise uses
    patterns: capitalised phrases become entities, "X is a/the ..." becomes a summary and
    "X <relation> Y" (e.g. "Marcus manages Atlas") becomes an edge.
    """
    for match in _JSON_BLOCK_RE.finditer(response):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and ("entities" in data or "edges" in data):
            data.setdefault("summary", _narrative(query, response))
            for item in (*(data.get("entities") or []), *(data.get("edges") or [])):
                if isinstance(item, dict):
                    item.setdefault("origin", MODEL_ORIGIN)
                    item.setdefault("confidence", MODEL_CONFIDENCE)
            return ground_extraction(data, query)

    user_text, other_text = _split_user_assertions(query)
    entities: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    # User assertions first so their provenance wins when the assistant repeats a fact.
    _pattern_extract(user_text, USER_ORIGIN, USER_CONFIDENCE, entities, edges)
    _pattern_extract(f"{other_text}\n{strip_markdown_structure(response)}", MODEL_ORIGIN, MODEL_CONFIDENCE, entities, edges)
    extraction = {"entities": list(entities.values()), "edges": edges, "summary": _narrative(query, response)}
    return ground_extraction(extraction, query)


def _significant_tokens(text: str) -> set[str]:
    """Casefolded significant words of `text` (length >= 2, no stop-words or filler)."""
    tokens = (t.rstrip(".-").casefold() for t in re.findall(r"\w[\w\-.#+]*", text or ""))
    return {t for t in tokens if len(t) >= 2 and t not in _STOPWORDS and not is_filler_name(t)}


def _is_grounded(name: str, query_tokens: set[str]) -> bool:
    """A name is grounded when it is not filler and shares a significant token with the user's turn."""
    return not is_filler_name(name) and bool(_significant_tokens(name) & query_tokens)


def ground_extraction(extraction: Extraction, query: str) -> Extraction:
    """Drop model-inferred noise: filler names and entities the user's turn never touches.

    User-asserted items always survive. A model-inferred entity survives when it is grounded
    in `query`, or when it is linked by a surviving edge to a grounded/user entity (so
    "Marcus manages Atlas" keeps Atlas after "Tell me about Marcus"). Edges need at least one
    grounded endpoint and no filler endpoint; entities left over are discarded.
    """
    query_tokens = _significant_tokens(query)

    def trusted(item: dict[str, Any]) -> bool:
        return item.get("origin") == USER_ORIGIN

    entities = [e for e in extraction.get("entities") or [] if isinstance(e, dict)]
    anchors = {
        str(e.get("name", "")).strip()
        for e in entities
        if trusted(e) or _is_grounded(str(e.get("name", "")).strip(), query_tokens)
    }
    kept_edges = []
    for edge in extraction.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        ends = (str(edge.get("source", "")).strip(), str(edge.get("target", "")).strip())
        if trusted(edge) or (
            not any(is_filler_name(n) for n in ends)
            and any(n in anchors or _is_grounded(n, query_tokens) for n in ends)
        ):
            kept_edges.append(edge)
    linked = {n for e in kept_edges for n in (str(e.get("source", "")).strip(), str(e.get("target", "")).strip())}
    kept_entities = [
        e for e in entities if trusted(e) or str(e.get("name", "")).strip() in anchors | linked
    ]
    kept_entities = [e for e in kept_entities if trusted(e) or not is_filler_name(str(e.get("name", "")))]
    return {**extraction, "entities": kept_entities, "edges": kept_edges}


class DistillationEngine:
    """Persists knowledge learned from a completed exchange. Never raises from `process_exchange`."""

    def __init__(self, db: DatabaseManager, extractor: Extractor | None = None) -> None:
        self.db = db
        self.extractor: Extractor = extractor or heuristic_extract

    async def _extract(self, query: str, response: str) -> Extraction:
        result = self.extractor(query, response)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {}

    async def _existing(self, name: str) -> tuple[str, str] | None:
        async with self.db.conn.execute(
            "SELECT archetype, summary FROM entities WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return (row[0], row[1] or "") if row else None

    @staticmethod
    def _provenance(item: dict[str, Any]) -> tuple[str, float]:
        """(origin, confidence) of an extracted item; unattributed items count as model-inferred."""
        origin = item.get("origin")
        if not isinstance(origin, str) or not origin.strip():
            origin = MODEL_ORIGIN
        default = USER_CONFIDENCE if origin == USER_ORIGIN else MODEL_CONFIDENCE
        try:
            confidence = float(item.get("confidence", default))
        except (TypeError, ValueError):
            confidence = default
        return origin.strip(), min(1.0, max(0.0, confidence))

    async def _upsert(
        self,
        ids: dict[str, int],
        name: str,
        archetype: Any = None,
        summary: Any = None,
        origin: str = MODEL_ORIGIN,
        confidence: float = MODEL_CONFIDENCE,
        ranks: dict[str, float] | None = None,
    ) -> int:
        """Upsert preserving stored archetype/summary unless the extraction supplies new ones.

        The database enforces provenance precedence, so a lower-confidence assertion never
        overwrites a stored higher-confidence one.
        """
        ranks = ranks if ranks is not None else {}
        if name in ids and not (archetype or summary) and confidence <= ranks.get(name, 0.0):
            return ids[name]
        parent_id = await self._consolidate(name)
        if parent_id is not None:
            ids[name] = parent_id
            return parent_id
        ranks[name] = max(confidence, ranks.get(name, 0.0))
        existing = await self._existing(name)
        old_archetype, old_summary = existing if existing else (DEFAULT_ARCHETYPE, "")
        new_archetype = archetype if archetype in VALID_ARCHETYPES else old_archetype
        new_summary = summary.strip() if isinstance(summary, str) and summary.strip() else old_summary
        ids[name] = await self.db.upsert_entity(
            name,
            new_archetype,
            new_summary,
            metadata=await self._metadata_with_aliases(name),
            origin=origin,
            confidence=confidence,
        )
        return ids[name]

    async def _consolidate(self, name: str) -> int | None:
        """Id of the single existing entity that `name` merely refers to, else None.

        A single-word candidate with no entity of its own is a reference when it equals (case-
        insensitively) a stored alias or a constituent token of exactly one multi-word entity
        ("BMW" -> "BMW 540i"). The referent's `updated_at` is bumped instead of creating an
        orphan node. Ambiguous matches (several referents) are not consolidated.
        """
        folded = name.strip().casefold()
        if not folded or len(name.split()) != 1:
            return None
        async with self.db.conn.execute("SELECT id, name, metadata FROM entities") as cur:
            rows = await cur.fetchall()
        if any(row[1].casefold() == folded for row in rows):
            return None  # the entity exists under its own name; normal upsert path
        matches: list[int] = []
        for entity_id, entity_name, raw in rows:
            names = {t.casefold() for t in name_tokens(entity_name)}
            try:
                meta = json.loads(raw or "{}")
            except ValueError:
                meta = {}
            stored = meta.get("aliases") if isinstance(meta, dict) else None
            if isinstance(stored, str):
                stored = [stored]
            if isinstance(stored, list):
                names |= {a.strip().casefold() for a in stored if isinstance(a, str)}
            if folded in names:
                matches.append(int(entity_id))
        if len(matches) != 1:
            return None
        async with self.db._transaction() as conn:
            await conn.execute("UPDATE entities SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (matches[0],))
        logger.info("Consolidated candidate %r into existing entity id=%d", name, matches[0])
        return matches[0]

    async def _metadata_with_aliases(self, name: str) -> dict[str, Any] | None:
        """Stored metadata merged with constituent-token aliases of a multi-word `name`.

        Returns None (leave metadata untouched) when the name yields no aliases.
        """
        tokens = name_tokens(name)
        if not tokens:
            return None
        async with self.db.conn.execute("SELECT metadata FROM entities WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
        try:
            meta = json.loads(row[0] or "{}") if row else {}
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        stored = meta.get("aliases")
        aliases = [a for a in stored if isinstance(a, str)] if isinstance(stored, list) else []
        seen = {a.casefold() for a in aliases}
        aliases += [t for t in tokens if t.casefold() not in seen]
        meta["aliases"] = aliases
        return meta

    @staticmethod
    def _connected_names(extraction: Extraction) -> set[str]:
        """Names that are an endpoint of at least one edge `process_exchange` will persist."""
        names: set[str] = set()
        for edge in extraction.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source", "")).strip()
            target = str(edge.get("target", "")).strip()
            relation = str(edge.get("relation", "")).strip()
            if not (source and target and relation) or source == target:
                continue
            if is_valid_entity_name(source) and is_valid_entity_name(target):
                names |= {source, target}
        return names

    async def process_exchange(self, session_id: str, query: str, response: str) -> int | None:
        """Extract and persist entities, edges and an episode; return the episode id.

        Returns None when there is nothing to record or on failure (failures are logged as
        warnings, never raised).
        """
        try:
            if not is_substantive_exchange(query, response):
                logger.info("Skipped non-substantive exchange session=%r", session_id)
                return None
            logger.info("Substantive exchange session=%r; extracting", session_id)
            extraction = await self._extract(query, response)
            ids: dict[str, int] = {}
            ranks: dict[str, float] = {}
            candidates = [e for e in extraction.get("entities") or []]
            # Multi-word names first so single-token mentions ("BMW") consolidate into them.
            candidates.sort(key=lambda e: len(str(e.get("name", "")).split()) < 2 if isinstance(e, dict) else True)
            connected = self._connected_names(extraction)
            for entity in candidates:
                name = str(entity.get("name", "")).strip() if isinstance(entity, dict) else ""
                archetype = entity.get("archetype") if isinstance(entity, dict) else None
                if name and is_valid_entity_name(name, archetype if archetype in VALID_ARCHETYPES else DEFAULT_ARCHETYPE):
                    origin, confidence = self._provenance(entity)
                    if origin != USER_ORIGIN and name not in connected:
                        logger.info("Dropped edge-free inferred entity %r", name)
                        continue
                    await self._upsert(
                        ids, name, entity.get("archetype"), entity.get("summary"), origin, confidence, ranks
                    )

            edge_count = 0
            for edge in extraction.get("edges") or []:
                if not isinstance(edge, dict):
                    continue
                source = str(edge.get("source", "")).strip()
                target = str(edge.get("target", "")).strip()
                relation = str(edge.get("relation", "")).strip()
                if not (source and target and relation) or source == target:
                    continue
                if not (is_valid_entity_name(source) and is_valid_entity_name(target)):
                    continue
                origin, confidence = self._provenance(edge)
                source_id = await self._upsert(ids, source, origin=origin, confidence=confidence, ranks=ranks)
                target_id = await self._upsert(ids, target, origin=origin, confidence=confidence, ranks=ranks)
                await self.db.upsert_edge(
                    source_id,
                    target_id,
                    relation,
                    float(edge.get("weight", 1.0)),
                    origin=origin,
                    confidence=confidence,
                )
                edge_count += 1

            narrative = str(extraction.get("summary") or "").strip() or _narrative(query, response)
            episode_id = await self.db.record_episode(session_id, narrative, sorted(set(ids.values())))
            logger.info(
                "Recorded episode id=%s session=%r entities=%d edges=%d",
                episode_id,
                session_id,
                len(ids),
                edge_count,
            )
            return episode_id
        except Exception as exc:  # background work must never crash the app
            logger.warning("Distillation failed for session %r: %r", session_id, exc)
            return None

