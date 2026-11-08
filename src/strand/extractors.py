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
from .prompts import EXTRACTOR_SYSTEM_PROMPT

logger = get_logger("extractors")

DEFAULT_EXTRACTOR_URL = "http://127.0.0.1:11434/v1"
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_MAX_INPUT_CHARS = 6000

SYSTEM_PROMPT = EXTRACTOR_SYSTEM_PROMPT  # backwards-compatible alias (prompt lives in prompts.py)


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
