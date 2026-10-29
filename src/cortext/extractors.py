"""Model B: structured extraction through a local OpenAI-compatible SLM (Ollama, vLLM).

`LLMExtractor` matches the async `Extractor` signature used by `DistillationEngine`
and never raises: any transport, status or JSON problem yields an empty extraction,
so distillation falls back to storing just the episode.
"""

from typing import Any

import httpx

from .common import extract_json, strip_think
from .distill import Extraction
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
    "'I no longer work at Y'); name the same source, target and relation the relationship had. "
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
    'If nothing is worth keeping return {"entities": [], "edges": [], "deprecated_edges": [], '
    '"forgotten_entities": []}.'
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_extraction(content: str) -> Extraction:
    """Parse model output into an extraction dict; malformed input yields empty lists.

    Only whitelisted fields are copied, so any model-emitted `origin`/`confidence` is dropped
    and the engine applies the model defaults (model_inferred at MODEL_CONFIDENCE).
    """
    empty: Extraction = {
        "entities": [], "edges": [], "deprecated_edges": [], "forgotten_entities": [], "summary": ""
    }
    data = extract_json(content)
    if data is None:
        return empty
    entities = []
    for item in data.get("entities") or []:
        if isinstance(item, dict) and _text(item.get("name")):
            entities.append(
                {
                    "name": _text(item["name"]),
                    "archetype": _text(item.get("archetype")),
                    "summary": _text(item.get("summary")),
                }
            )
    edges = []
    for item in data.get("edges") or []:
        if isinstance(item, dict) and all(_text(item.get(k)) for k in ("source", "target", "relation")):
            edges.append({k: _text(item[k]) for k in ("source", "target", "relation")})
    deprecated = []
    for item in data.get("deprecated_edges") or []:
        if isinstance(item, dict) and all(_text(item.get(k)) for k in ("source", "target", "relation")):
            deprecated.append({k: _text(item.get(k)) for k in ("source", "target", "relation", "reason")})
    raw_forgotten = data.get("forgotten_entities")
    forgotten = list(
        dict.fromkeys(_text(n) for n in raw_forgotten if _text(n)) if isinstance(raw_forgotten, list) else ()
    )
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
        payload = {
            "model": self.model_name,
            "temperature": 0,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"User: {query[:_MAX_INPUT_CHARS]}\n\nAssistant: {response[:_MAX_INPUT_CHARS]}",
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
            logger.warning("local extractor failed: %s", exc)
            return parse_extraction("")
