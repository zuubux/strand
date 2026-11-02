"""Model B: structured extraction through a local OpenAI-compatible SLM (Ollama, vLLM).

`LLMExtractor` matches the async `Extractor` signature used by `DistillationEngine`
and never raises: any transport, status or JSON problem yields an empty extraction,
so distillation falls back to storing just the episode.
"""

import re
from typing import Any

import httpx

from .common import extract_json, safe_exc, strip_think
from .distill import USER_CONFIDENCE, USER_ORIGIN, Extraction
from .host import get_host_context
from .logging import get_logger

logger = get_logger("extractors")

DEFAULT_EXTRACTOR_URL = "http://127.0.0.1:11434/v1"
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_MAX_INPUT_CHARS = 6000

SYSTEM_PROMPT = (
    "You extract a knowledge graph from a conversation turn. Reply with strict JSON only, "
    'shaped as {"entities": [{"name": str, "archetype": one of person|project|system|vehicle|concept|trait|directive|preference, '
    '"summary": str}], "edges": [{"source": str, "target": str, "relation": str}], '
    '"deprecated_edges": [{"source": str, "target": str, "relation": str, "reason": str}]}. '
    "Only include durable facts; edge endpoints must be entity names. "
    "Use deprecated_edges only when the user explicitly states an existing relationship has ended, "
    "been replaced, sold, stopped or invalidated (e.g. 'I sold X', 'I switched from A to B', "
    "'I no longer work at Y', 'I no longer use Z'); state transitions matter: replacement, disposal or "
    "discontinuation (sold, switched from A to B, got rid of, quit, no longer using) must emit a "
    "deprecated_edges entry for the superseded relationship, plus the new fact as a normal edge. "
    "Name the same source, target and relation the relationship had. "
    "Strict boundary for deprecated_edges: emit an edge there only when the user explicitly states that "
    "a fact or preference was WRONG, cancelled or should be forgotten, or that a current relationship "
    "has ended. Biographical history is never a deprecation: past possessions, previous employment and "
    "earlier life facts (e.g. 'I used to own an W34', 'My previous car was a Cruiser') must be emitted "
    "as an ACTIVE edge (previously_owned or owned) at normal weight, e.g. (User) --[previously_owned]--> "
    "(W34), and NEVER placed in deprecated_edges. "
    "Optionally add forgotten_entities: [entity_name, ...]. Use it only when the user explicitly "
    "commands the system to forget, delete, scrub or erase an entity, concept or record "
    "(e.g. 'Forget my old address', 'Delete the entity for Project Titan', 'Forget X'); list the "
    "target entity names. Never also list a forgotten entity in entities or as an edge endpoint. "
    "Also capture interaction updates: conversational adjustments, behavioral critiques and style "
    "instructions the user gives about how the assistant should behave (e.g. 'be more terse', "
    "'stop apologizing', 'give me unified diffs') are never physical concepts. "
    'Tone or character critiques become archetype "trait"; hard rules and invariants become '
    'archetype "directive"; formatting or style requests become archetype "preference". '
    "Name each one with a short descriptive identifier and put the full rule in its summary. "
    "Link each to the entity User with an edge (source User, target the rule) using relation "
    "prefers for a preference, demands for a directive, or enforces for a trait or a standing rule. "
    "If the user changes or contradicts an earlier preference, rule or trait (e.g. 'actually, "
    "stop using diffs, send full files'), emit a deprecated_edges entry naming the same source, "
    "target and relation of the old rule so it is cleanly deprecated, and add the new rule as "
    "a new entity and edge. "
    "Permanence guardrail: distinguish PERMANENT DIRECTIVES from TRANSIENT STEERING. A permanent "
    "directive needs an explicit permanence marker such as 'from now on', 'always', 'never', "
    "'stop doing X', 'switch to Y' or 'going forward'. Transient steering is immediate or one-off "
    "context such as 'right now', 'just for this' or 'give me the diff' with no marker; it must NOT "
    "create a permanent directive or preference and must NOT deprecate any standing edge, so return "
    "nothing for it. When a permanent behavioral correction is detected (e.g. 'Stop summarizing "
    "diffs from now on'): 1) identify the obsolete directive, preference or trait and emit it in "
    "deprecated_edges with the operator as source and the old rule's target and relation; 2) emit the "
    "replacement behavior as an active entity with archetype \"directive\" or \"preference\" and "
    "connect it to the operator with an edge (the model_inferred origin is applied to it automatically). "
    "Deprecation only severs edges; never list entities for deletion because of a critique. "
    "Attach traits, concerns and preferences directly between the subject and the concrete "
    "noun or entity through the edge relation, e.g. (User) --[prefers]--> (AWD) or "
    "(User) --[concerned_about]--> (Battery Life). Never create generic intermediate container "
    "entities such as \"User's Preference\", \"User's Concern\" or \"User Settings\"; no two-hop "
    "chains like (User) --[has]--> (User's Preference) --[includes]--> (AWD). "
    "Operator identity: when the exchange identifies or confirms the operator's preferred conversational "
    "name (for example an answer to 'What should I call you?' such as 'Alice', 'Just Alice', 'Alice works', "
    "'Alicia is fine', or a statement like 'Call me Alice' or 'I'm Alice'), emit an entity with name set to "
    'the declared name, archetype "person", origin "user_explicit" and confidence 1.0, and emit the edge '
    '(<Declared Name>) --[operates_host]--> (<Hostname>) using the hostname given in the Host line. '
    "Use the declared name instead of User as the source of the operator's other edges. "
    "Questions, refusals and non-names (e.g. 'why?', 'nothing', 'stop') declare no name. "
    "Host telemetry preference: when the operator answers a question about whether the assistant may "
    "inspect their hardware specs (CPU/RAM/OS), in onboarding or later chat, emit one edge from the "
    "operator (the declared name, else User) with relation prefers: agreement (e.g. 'yeah, check it "
    "out', 'tune into the iron') is (<Operator>) --[prefers]--> (Host Telemetry Enabled); a decline "
    "or on-demand answer (e.g. 'keep it on-demand', 'leave it local') is (<Operator>) --[prefers]--> "
    "(Host Telemetry On-Demand). Use archetype \"preference\" for that entity. Only deprecate the "
    "opposite Host Telemetry edge when the operator reverses this choice; this preference never "
    "deprecates or replaces unrelated directives, traits, preferences or person attributes, and a "
    "turn without a clear hardware answer emits no Host Telemetry edge. "
    'If nothing is worth keeping return {"entities": [], "edges": [], "deprecated_edges": [], '
    '"forgotten_entities": []}.'
)


OPERATES_HOST = "operates_host"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


_CONTAINER_RE = re.compile(
    r"^(?P<owner>the user|user|operator|me|my|i)(?:['’]s|s)?\s+"
    r"(?P<kind>preferences?|concerns?|traits?|settings?)$",
    re.IGNORECASE,
)
_KIND_RELATION = {
    "preference": "prefers",
    "setting": "prefers",
    "concern": "concerned_about",
    "trait": "enforces",
}
_GENERIC_RELATIONS = frozenset(
    {"has", "have", "includes", "include", "contains", "contain", "is", "are", "has_a", "has_preference"}
)


def _container(name: str) -> tuple[str, str] | None:
    """(owner, singular kind) when `name` is a synthetic grouping node like "User's Preference"."""
    match = _CONTAINER_RE.match(name.strip())
    if match is None:
        return None
    owner = "User" if match["owner"].lower() in ("the user", "user", "me", "my", "i") else "Operator"
    return owner, match["kind"].lower().rstrip("s")


def collapse_containers(
    entities: list[dict[str, str]], edges: list[dict[str, str]], deprecated: list[dict[str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Collapse `(S) -> (User's Preference) -> (T)` chains into direct `(S) -> (T)` edges.

    Synthetic grouping entities are dropped along with every edge touching them; each
    outgoing container edge is re-attached to the container's subject (the incoming edge
    source, else the owner named in the pattern). Generic relations (has/includes/...)
    are replaced by one derived from the container kind (prefers, concerned_about, enforces).
    """
    containers = {e["name"].lower(): _container(e["name"]) for e in entities}
    for edge in edges + deprecated:
        for key in ("source", "target"):
            containers.setdefault(edge[key].lower(), _container(edge[key]))
    containers = {name: info for name, info in containers.items() if info is not None}
    if not containers:
        return entities, edges, deprecated

    subjects: dict[str, str] = {}
    for edge in edges:
        target = edge["target"].lower()
        if target in containers and edge["source"].lower() not in containers:
            subjects.setdefault(target, edge["source"])

    def rewrite(items: list[dict[str, str]]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in items:
            source, target = item["source"].lower(), item["target"].lower()
            if target in containers:
                continue  # edge into a container: replaced by the direct edge below
            if source in containers:
                owner, kind = containers[source]
                relation = item["relation"]
                if relation.lower() in _GENERIC_RELATIONS:
                    relation = _KIND_RELATION[kind]
                item = {**item, "source": subjects.get(source, owner), "relation": relation}
            if item not in out:
                out.append(item)
        return out

    kept = [e for e in entities if e["name"].lower() not in containers]
    return kept, rewrite(edges), rewrite(deprecated)


USER_ANCHORED_RELATIONS = frozenset({"owns", "owned", "previously_owned", "prefers", "demands"})
_OPERATOR_ALIASES = frozenset({"user", "operator", "me", "i"})


def _is_user_anchored(edge: dict[str, str], operators: set[str]) -> bool:
    """True for a first-person declaration: (Operator) -[owns|previously_owned|prefers|demands]-> (...)."""
    source = edge["source"]
    return edge["relation"].casefold().replace(" ", "_") in USER_ANCHORED_RELATIONS and (
        source in operators or source.casefold() in _OPERATOR_ALIASES
    )


def parse_extraction(content: str) -> Extraction:
    """Parse model output into an extraction dict; malformed input yields empty lists.

    Only whitelisted fields are copied. Model-emitted `origin`/`confidence` are dropped (the engine
    applies model_inferred at MODEL_CONFIDENCE) except for the declared operator: a `person` with
    `origin: user_explicit` keeps user_explicit at 1.0, as does its `operates_host` edge. Direct
    first-person declarations anchored to the operator (`owns`, `owned`, `previously_owned`,
    `prefers`, `demands`) are tagged user_explicit at 1.0 along with their target entity; every
    other item stays untagged and is clamped to model_inferred by the engine.
    """
    empty: Extraction = {
        "entities": [], "edges": [], "deprecated_edges": [], "forgotten_entities": [], "summary": ""
    }
    data = extract_json(content)
    if data is None:
        return empty
    entities = []
    operators: set[str] = set()
    for item in data.get("entities") or []:
        if isinstance(item, dict) and _text(item.get("name")):
            entity = {
                "name": _text(item["name"]),
                "archetype": _text(item.get("archetype")),
                "summary": _text(item.get("summary")),
            }
            if entity["archetype"] == "person" and _text(item.get("origin")) == USER_ORIGIN:
                entity.update(origin=USER_ORIGIN, confidence=USER_CONFIDENCE)  # declared operator (Model B)
                operators.add(entity["name"])
            entities.append(entity)
    edges = []
    for item in data.get("edges") or []:
        if isinstance(item, dict) and all(_text(item.get(k)) for k in ("source", "target", "relation")):
            edge = {k: _text(item[k]) for k in ("source", "target", "relation")}
            if edge["relation"] == OPERATES_HOST and edge["source"] in operators:
                edge.update(origin=USER_ORIGIN, confidence=USER_CONFIDENCE)
            elif _is_user_anchored(edge, operators):
                edge.update(origin=USER_ORIGIN, confidence=USER_CONFIDENCE)  # direct user declaration
            edges.append(edge)
    declared = {e["target"] for e in edges if e.get("origin") == USER_ORIGIN and e["relation"] != OPERATES_HOST}
    for entity in entities:
        if entity["name"] in declared and "origin" not in entity:
            entity.update(origin=USER_ORIGIN, confidence=USER_CONFIDENCE)
    deprecated = []
    for item in data.get("deprecated_edges") or []:
        if isinstance(item, dict) and all(_text(item.get(k)) for k in ("source", "target", "relation")):
            deprecated.append({k: _text(item.get(k)) for k in ("source", "target", "relation", "reason")})
    raw_forgotten = data.get("forgotten_entities")
    forgotten = list(
        dict.fromkeys(_text(n) for n in raw_forgotten if _text(n)) if isinstance(raw_forgotten, list) else ()
    )
    entities, edges, deprecated = collapse_containers(entities, edges, deprecated)
    return {
        "entities": entities,
        "edges": edges,
        "deprecated_edges": deprecated,
        "forgotten_entities": forgotten,
        "summary": "",
    }


class LLMExtractor:
    """Async extractor calling a local `/chat/completions` endpoint."""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str = "ollama",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model_name = model_name
        self.api_key = api_key
        self._client = client

    async def __call__(self, query: str, response: str) -> Extraction:
        response = strip_think(response)
        hostname = str(get_host_context().get("hostname") or "localhost")
        payload = {
            "model": self.model_name,
            "temperature": 0,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Host: {hostname}\n\nUser: {query[:_MAX_INPUT_CHARS]}"
                        f"\n\nAssistant: {response[:_MAX_INPUT_CHARS]}"
                    ),
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
            try:
                reply = await client.post(self.url, json=payload, headers=headers)
            finally:
                if self._client is None:
                    await client.aclose()
            reply.raise_for_status()
            content = reply.json()["choices"][0]["message"]["content"]
            return parse_extraction(content if isinstance(content, str) else "")
        except Exception as exc:
            logger.warning("local extractor failed: %s", safe_exc(exc))
            return parse_extraction("")
