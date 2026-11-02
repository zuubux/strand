"""Session compaction: synthesize inactive sessions' episodes into entity summaries.

Uses the local OpenAI-compatible SLM when `extractor_model` is set, otherwise a
heuristic. If the SLM is unreachable or returns unusable output the session is
skipped (episodes stay uncompacted) and a warning is logged; nothing is raised.
"""

import re
from typing import Any

import httpx

from .common import (
    DEFAULT_COMPACTION_THRESHOLD_HOURS,
    MAX_SUMMARY_LEN,
    MODEL_CONFIDENCE,
    PERSONALITY_ARCHETYPES,
    extract_json,
    is_private,
    safe_exc,
)
from .db import DatabaseManager
from .extractors import DEFAULT_EXTRACTOR_URL
from .logging import get_logger
from .onboarding import OPERATES_HOST, OPERATOR_ALIAS, count_operators
from .sanitize import sanitize_text

logger = get_logger("compactor")

DEFAULT_THRESHOLD_HOURS = DEFAULT_COMPACTION_THRESHOLD_HOURS
COMPACTION_ORIGIN = "model_inferred"
SLM_CONFIDENCE = MODEL_CONFIDENCE
HEURISTIC_CONFIDENCE = 0.5
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_MAX_INPUT_CHARS = 12000
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")

SYSTEM_PROMPT = (
    "You consolidate a finished conversation session into durable, multi-horizon knowledge. Given the "
    "session's episode narratives and the known entities, reply with strict JSON only, shaped as "
    '{"entity_summaries": [{"name": str, "summary": str}], "session_summary": str, '
    '"discovered_aliases": [{"canonical_name": str, "alias": str}], '
    '"operator_profile": {"focus_summary": str, "inferred_traits": [{"name": str, '
    '"archetype": "preference"|"trait", "relation": "prefers"|"enforces", "summary": str}]}}. '
    "entity_summaries: one or two sentences per touched entity, combining its existing summary with what "
    "the session added; only include entities from the provided list. "
    "discovered_aliases: recurring nicknames or conversational shorthand that refer to a known entity "
    '(e.g. "KK" for "Karen", "the box" for a host or server); canonical_name must be a provided entity. '
    "operator_profile.focus_summary: a concise one or two sentence rolling summary of the operator's "
    "recent work and topics. operator_profile.inferred_traits: sustained interaction preferences "
    "(e.g. terse diffs, specific test flags, a technical tone). Ignore one-off conversational steering "
    "and transient requests; only emit a trait or preference that reflects a sustained pattern across "
    "the provided episodes. "
    'If nothing is worth keeping return {"entity_summaries": [], "session_summary": "", '
    '"discovered_aliases": [], "operator_profile": {"focus_summary": "", "inferred_traits": []}}.'
)

TRAIT_RELATIONS = frozenset({"prefers", "enforces"})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_compaction(content: str) -> dict[str, Any] | None:
    """Parse SLM output; returns None when no JSON object can be recovered."""
    data = extract_json(content)
    if data is None:
        return None
    summaries = []
    for item in data.get("entity_summaries") or []:
        if isinstance(item, dict) and _text(item.get("name")) and _text(item.get("summary")):
            summaries.append({"name": _text(item["name"]), "summary": _text(item["summary"])})
    return {"entity_summaries": summaries, "session_summary": _text(data.get("session_summary"))}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [i for i in value if isinstance(i, dict)] if isinstance(value, list) else []


def parse_compaction_output(raw_text: str) -> dict[str, Any]:
    """Defensively parse multi-horizon compaction output; never raises, never returns None.

    Missing or malformed keys fall back to empty lists/strings. Traits with an archetype outside
    `PERSONALITY_ARCHETYPES` or a relation outside `TRAIT_RELATIONS` are dropped. Inferred traits and
    the operator focus summary (`operator_profile["origin"]`/`["confidence"]`) are always tagged
    `model_inferred` at 0.60, regardless of anything the model claimed.
    """
    empty_profile = {"focus_summary": "", "inferred_traits": [], "origin": COMPACTION_ORIGIN, "confidence": SLM_CONFIDENCE}
    result: dict[str, Any] = {
        "entity_summaries": [],
        "session_summary": "",
        "discovered_aliases": [],
        "operator_profile": empty_profile,
    }
    try:
        data = extract_json(raw_text if isinstance(raw_text, str) else "")
    except Exception:
        data = None
    if data is None:
        return result
    for item in _dicts(data.get("entity_summaries")):
        if _text(item.get("name")) and _text(item.get("summary")):
            result["entity_summaries"].append({"name": _text(item["name"]), "summary": _text(item["summary"])})
    result["session_summary"] = _text(data.get("session_summary"))
    for item in _dicts(data.get("discovered_aliases")):
        canonical, alias = _text(item.get("canonical_name")), _text(item.get("alias"))
        if canonical and alias and canonical.lower() != alias.lower():
            result["discovered_aliases"].append({"canonical_name": canonical, "alias": alias})
    profile = data.get("operator_profile")
    if isinstance(profile, dict):
        empty_profile["focus_summary"] = _text(profile.get("focus_summary"))[:MAX_SUMMARY_LEN]
        for item in _dicts(profile.get("inferred_traits")):
            name, summary = _text(item.get("name")), _text(item.get("summary"))
            archetype, relation = _text(item.get("archetype")).lower(), _text(item.get("relation")).lower()
            if name and summary and archetype in PERSONALITY_ARCHETYPES and relation in TRAIT_RELATIONS:
                empty_profile["inferred_traits"].append(
                    {
                        "name": name,
                        "archetype": archetype,
                        "relation": relation,
                        "summary": summary[:MAX_SUMMARY_LEN],
                        "origin": COMPACTION_ORIGIN,
                        "confidence": SLM_CONFIDENCE,
                    }
                )
    return result


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
            f"SELECT id, name, summary, archetype, metadata FROM entities WHERE id IN ({marks}) ORDER BY id",
            sorted(ids),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r["id"], "name": r["name"], "summary": r["summary"]}
            for r in rows
            if not is_private(r["archetype"], r["metadata"])
        ]

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
            logger.warning("compaction SLM unavailable: %s", safe_exc(exc))
            return None
        text = content if isinstance(content, str) else ""
        if extract_json(text) is None:
            logger.warning("compaction SLM returned unparseable output")
            return None
        return parse_compaction_output(text)

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
                summaries.append({"name": entity["name"], "summary": " ".join(sentences)[:MAX_SUMMARY_LEN]})
        return {"entity_summaries": summaries, "session_summary": ""}

    async def _operator(self) -> tuple[int, str] | None:
        """(id, name) of the primary operator: declared person with `operates_host`, else `User`."""
        if await count_operators(self.db):
            async with self.db.conn.execute(
                "SELECT e.id, e.name FROM entities e JOIN edges d ON d.source_id = e.id "
                "WHERE e.archetype = 'person' AND e.origin = 'user_explicit' AND d.relation = ? "
                "ORDER BY e.updated_at DESC, e.id DESC LIMIT 1",
                (OPERATES_HOST,),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                return int(row[0]), row[1]
        return await self.db.find_entity(OPERATOR_ALIAS)

    async def _is_private_name(self, name: str) -> bool:
        """True when any entity named `name` (case-insensitive) is private (SEC-MEM-010)."""
        async with self.db.conn.execute(
            "SELECT archetype, metadata FROM entities WHERE name = ? COLLATE NOCASE", (name,)
        ) as cur:
            return any(is_private(a, m) for a, m in await cur.fetchall())

    async def _apply_aliases(self, aliases: list[dict[str, str]]) -> None:
        for item in aliases:
            found = await self.db.find_entity(item["canonical_name"])
            if found is None:
                continue
            async with self.db.conn.execute(
                "SELECT archetype, metadata FROM entities WHERE id = ?", (found[0],)
            ) as cur:
                row = await cur.fetchone()
            if row is None or is_private(row[0], row[1]):
                continue
            alias = sanitize_text(item["alias"])
            if alias:
                await self.db.merge_entity_alias(
                    found[0], alias, summary="", origin=COMPACTION_ORIGIN, confidence=SLM_CONFIDENCE
                )

    async def _apply_operator_profile(self, profile: dict[str, Any]) -> None:
        traits, focus = profile.get("inferred_traits") or [], profile.get("focus_summary") or ""
        if not traits and not focus:
            return
        operator = await self._operator()
        if operator is None:
            return
        operator_id, operator_name = operator
        for trait in traits:
            existing = await self.db.find_entity(trait["name"])
            name = existing[1] if existing else sanitize_text(trait["name"])
            trait_id = await self.db.upsert_entity(
                name,
                trait["archetype"],
                sanitize_text(trait["summary"])[:MAX_SUMMARY_LEN],
                origin=COMPACTION_ORIGIN,
                confidence=SLM_CONFIDENCE,
            )
            async with self.db.conn.execute(
                "SELECT json_extract(metadata, '$.deprecated') FROM edges "
                "WHERE source_id = ? AND target_id = ? AND relation = ?",
                (operator_id, trait_id, trait["relation"]),
            ) as cur:
                edge = await cur.fetchone()
            if edge is not None and edge[0]:
                continue  # a deprecated stance is never revived by inference
            await self.db.upsert_edge(
                operator_id,
                trait_id,
                trait["relation"],
                weight=1.0,
                origin=COMPACTION_ORIGIN,
                confidence=SLM_CONFIDENCE,
            )
        if focus and not await self._is_private_name(operator_name):
            # Provenance precedence in the DB keeps user_explicit summaries intact.
            await self.db.update_entity_summary(
                operator_name, sanitize_text(focus)[:MAX_SUMMARY_LEN], COMPACTION_ORIGIN, SLM_CONFIDENCE
            )

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
                if await self._is_private_name(item["name"]):
                    continue
                summary = sanitize_text(item["summary"])[:MAX_SUMMARY_LEN]
                if await self.db.update_entity_summary(item["name"], summary, COMPACTION_ORIGIN, confidence):
                    updated += 1
            await self._apply_aliases(parsed.get("discovered_aliases") or [])
            await self._apply_operator_profile(parsed.get("operator_profile") or {})
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
