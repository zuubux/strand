"""Shared constants and small helpers used across Cortext modules.

This module must not import from other `cortext` modules (it sits at the bottom of the
dependency graph).
"""

import json
import re
from typing import Any

EMBEDDING_DIM = 384

# Personality Matrix archetypes: interaction rules stored as ordinary entity rows.
PERSONALITY_ARCHETYPES = frozenset({"trait", "directive", "preference"})
FACTUAL_ARCHETYPES = frozenset({"person", "project", "system", "vehicle", "concept"})
VALID_ARCHETYPES = FACTUAL_ARCHETYPES | PERSONALITY_ARCHETYPES
MIN_EDGE_CONFIDENCE = 0.2  # edges below this are treated as deprecated and never recalled
MODEL_CONFIDENCE = 0.6  # default confidence for facts the assistant (not the user) asserted
MAX_SUMMARY_LEN = 300


def clean_text(text: object) -> str:
    """Collapse all whitespace so the value stays on a single line."""
    return " ".join(str(text or "").split())


def truncate(text: object, limit: int = MAX_SUMMARY_LEN) -> str:
    """Whitespace-collapsed `text` cut to at most `limit` characters."""
    return clean_text(text)[: max(limit, 0)]


def extract_json(content: str) -> dict[str, Any] | None:
    """Recover a JSON object from model output.

    Accepts raw JSON, JSON inside a markdown code fence, or JSON surrounded by prose
    (the outermost `{...}` span). Returns None when no JSON object can be recovered.
    """
    text = (content or "").strip()
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
    return data if isinstance(data, dict) else None


# The backreference pairs each opener with its own closer, so `<think>` is never ended by `</thought>`.
_THINK_RE = re.compile(r"<(thinking|think|thought|reasoning)>.*?(?:</\1>|\Z)", re.IGNORECASE | re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>, <thinking>, <thought> and <reasoning> blocks (an unclosed one runs to the end)."""
    return _THINK_RE.sub("", text) if "<" in text else text


def load_metadata(raw: dict | str | bytes | None) -> dict[str, Any]:
    """Metadata as a dict from stored JSON text (or a dict); anything malformed yields {}."""
    if isinstance(raw, (str, bytes)) or raw is None:
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


_FLAG_STRINGS = frozenset({"true", "1"})
_TRIM = " \t\n\r"  # trimmed from archetypes and flag strings; db.PRIVATE_ENTITY_SQL trims the same set


def _reject_constant(name: str) -> Any:
    """json.loads hook: SQLite's json_valid() rejects NaN/Infinity, so the Python side must too."""
    raise ValueError(f"invalid JSON constant {name}")


def _flag_is_set(value: Any) -> bool:
    """Strict truthiness of a metadata flag: True, 1 / 1.0, or the strings "true" / "1" (any case).

    Deliberately not Python truthiness: the string "false" must stay False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return isinstance(value, str) and value.strip(_TRIM).lower() in _FLAG_STRINGS


def is_private(archetype: str | None, metadata: dict | str | bytes | None) -> bool:
    """The single privacy predicate for an entity (its SQL twin is `db.PRIVATE_ENTITY_SQL`).

    Private when the archetype is "private" (case-insensitive) or `metadata["private"]` is
    set (see `_flag_is_set`). Fails closed: metadata that is corrupt JSON, or valid JSON that
    is not an object, counts as private. Only absent or blank metadata means "no flags".
    """
    if isinstance(archetype, str) and archetype.strip(_TRIM).lower() == "private":
        return True
    if metadata is None:
        return False
    if isinstance(metadata, (str, bytes)):
        try:
            text = metadata.decode("utf-8") if isinstance(metadata, bytes) else metadata
        except UnicodeDecodeError:
            return True
        if not text.strip(_TRIM):
            return False
        try:
            metadata = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return True
    if not isinstance(metadata, dict):
        return True
    return _flag_is_set(metadata.get("private"))


def aliases_of(metadata: dict | str | None) -> list[str]:
    """String aliases from entity metadata (dict or JSON text); tolerates malformed data."""
    stored = load_metadata(metadata).get("aliases")
    if isinstance(stored, str):
        stored = [stored]
    return [a for a in stored if isinstance(a, str)] if isinstance(stored, list) else []
