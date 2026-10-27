"""Session compaction: synthesize inactive sessions' episodes into entity summaries.

Uses the local OpenAI-compatible SLM when `extractor_model` is set, otherwise a
heuristic. If the SLM is unreachable or returns unusable output the session is
skipped (episodes stay uncompacted) and a warning is logged; nothing is raised.
"""

import json
import re
from typing import Any

import httpx

from .db import DatabaseManager
from .extractors import DEFAULT_EXTRACTOR_URL
from .logging import get_logger
from .sanitize import sanitize_text

logger = get_logger("compactor")

DEFAULT_THRESHOLD_HOURS = 24.0
COMPACTION_ORIGIN = "model_inferred"
SLM_CONFIDENCE = 0.6
HEURISTIC_CONFIDENCE = 0.5
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_MAX_INPUT_CHARS = 12000
_MAX_SUMMARY = 300
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")

SYSTEM_PROMPT = (
    "You consolidate a finished conversation session into durable knowledge. Given the session's "
    "episode narratives and the known entities, reply with strict JSON only, shaped as "
    '{"entity_summaries": [{"name": str, "summary": str}], "session_summary": str}. '
    "Each summary is one or two sentences combining the entity's existing summary with what the "
    "session added. Only include entities from the provided list. "
    'If nothing is worth keeping return {"entity_summaries": [], "session_summary": ""}.'
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_compaction(content: str) -> dict[str, Any] | None:
    """Parse SLM output; returns None when no JSON object can be recovered."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    summaries = []
    for item in data.get("entity_summaries") or []:
        if isinstance(item, dict) and _text(item.get("name")) and _text(item.get("summary")):
            summaries.append({"name": _text(item["name"]), "summary": _text(item["summary"])})
    return {"entity_summaries": summaries, "session_summary": _text(data.get("session_summary"))}


class SessionCompactor:
    """Compacts inactive sessions' episodes into updated entity summaries."""

    def __init__(
        self,
        db: DatabaseManager,
        extractor_url: str = "",
        extractor_model: str = "",
        threshold_hours: float = DEFAULT_THRESHOLD_HOURS,
        api_key: str = "ollama",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = db
        self.url = (extractor_url or DEFAULT_EXTRACTOR_URL).rstrip("/") + "/chat/completions"
        self.model = extractor_model
        self.threshold_hours = threshold_hours
        self.api_key = api_key
        self._client = client

    async def _entities(self, ids: set[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        async with self.db.conn.execute(
            f"SELECT id, name, summary FROM entities WHERE id IN ({marks}) ORDER BY id", sorted(ids)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def _ask_slm(self, narratives: list[str], entities: list[dict[str, Any]]) -> dict[str, Any] | None:
        known = "\n".join(f"- {e['name']}: {e['summary'] or '(no summary)'}" for e in entities)
        body = "\n\n".join(f"Episode {i}: {n}" for i, n in enumerate(narratives, 1))
        payload = {
            "model": self.model,
            "temperature": 0,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Known entities:\n{known or '(none)'}\n\nSession episodes:\n{body}"[:_MAX_INPUT_CHARS],
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
        except Exception as exc:
            logger.warning("compaction SLM unavailable: %s", exc)
            return None
        parsed = parse_compaction(content if isinstance(content, str) else "")
        if parsed is None:
            logger.warning("compaction SLM returned unparseable output")
        return parsed

    @staticmethod
    def _heuristic(narratives: list[str], entities: list[dict[str, Any]]) -> dict[str, Any]:
        """Summarize each entity from the narrative sentences that mention it."""
        summaries = []
        for entity in entities:
            pattern = re.compile(rf"\b{re.escape(entity['name'])}\b", re.IGNORECASE)
            sentences: list[str] = []
            for narrative in narratives:
                for sentence in _SENTENCE_RE.split(narrative):
                    sentence = sentence.strip()
                    if sentence and pattern.search(sentence) and sentence not in sentences:
                        sentences.append(sentence)
            if sentences:
                summaries.append({"name": entity["name"], "summary": " ".join(sentences)[:_MAX_SUMMARY]})
        return {"entity_summaries": summaries, "session_summary": ""}

    async def compact_session(self, session_id: str) -> dict[str, Any]:
        """Compact one session. Never raises; `skipped` is True when nothing was marked."""
        result: dict[str, Any] = {
            "session_id": session_id,
            "episodes": 0,
            "summaries_updated": 0,
            "session_summary": "",
            "skipped": False,
        }
        try:
            episodes = await self.db.get_session_episodes(session_id, uncompacted_only=True)
            if not episodes:
                return result
            narratives = [sanitize_text(e["narrative"]) for e in episodes]
            ids = {int(i) for e in episodes for i in e["extracted_entities"] if isinstance(i, int)}
            entities = await self._entities(ids)
            if self.model:
                parsed = await self._ask_slm(narratives, entities)
                confidence = SLM_CONFIDENCE
            else:
                parsed = self._heuristic(narratives, entities)
                confidence = HEURISTIC_CONFIDENCE
            if parsed is None:
                result["skipped"] = True
                return result
            updated = 0
            for item in parsed["entity_summaries"]:
                summary = sanitize_text(item["summary"])[:_MAX_SUMMARY]
                if await self.db.update_entity_summary(item["name"], summary, COMPACTION_ORIGIN, confidence):
                    updated += 1
            await self.db.mark_episodes_compacted([e["id"] for e in episodes])
            result.update(
                episodes=len(episodes),
                summaries_updated=updated,
                session_summary=sanitize_text(parsed["session_summary"]),
            )
        except Exception:
            logger.warning("compaction failed for session %r", session_id, exc_info=True)
            result["skipped"] = True
        return result

    async def compact_all(self, older_than_hours: float | None = None) -> int:
        """Compact every eligible session sequentially; returns how many were compacted."""
        hours = self.threshold_hours if older_than_hours is None else older_than_hours
        try:
            sessions = await self.db.get_uncompacted_sessions(hours * 3600.0)
        except Exception:
            logger.warning("could not list sessions for compaction", exc_info=True)
            return 0
        count = 0
        for session_id in sessions:
            result = await self.compact_session(session_id)
            if result["episodes"] and not result["skipped"]:
                count += 1
        return count
