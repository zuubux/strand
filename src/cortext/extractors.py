"""Model B: structured extraction through a local OpenAI-compatible SLM (Ollama, vLLM).

`LLMExtractor` matches the async `Extractor` signature used by `DistillationEngine`
and never raises: any transport, status or JSON problem yields an empty extraction,
so distillation falls back to storing just the episode.
"""

import json
from typing import Any

import httpx

from .distill import Extraction
from .logging import get_logger

logger = get_logger("extractors")

DEFAULT_EXTRACTOR_URL = "http://127.0.0.1:11434/v1"
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_MAX_INPUT_CHARS = 6000

SYSTEM_PROMPT = (
    "You extract a knowledge graph from a conversation turn. Reply with strict JSON only, "
    'shaped as {"entities": [{"name": str, "archetype": one of person|project|system|vehicle|concept, '
    '"summary": str}], "edges": [{"source": str, "target": str, "relation": str}]}. '
    "Only include durable facts; edge endpoints must be entity names. "
    'If nothing is worth keeping return {"entities": [], "edges": []}.'
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_extraction(content: str) -> Extraction:
    """Parse model output into an extraction dict; malformed input yields empty lists."""
    empty: Extraction = {"entities": [], "edges": [], "summary": ""}
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return empty
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            return empty
    if not isinstance(data, dict):
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
    return {"entities": entities, "edges": edges, "summary": ""}


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
            return {"entities": [], "edges": [], "summary": ""}
