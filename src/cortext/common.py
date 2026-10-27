"""Shared constants and small helpers used across Cortext modules.

This module must not import from other `cortext` modules (it sits at the bottom of the
dependency graph).
"""

import json
import re
from typing import Any

EMBEDDING_DIM = 384
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


_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.IGNORECASE | re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (an unclosed one runs to the end)."""
    return _THINK_RE.sub("", text) if "<" in text else text


def load_metadata(raw: dict | str | bytes | None) -> dict[str, Any]:
    """Metadata as a dict from stored JSON text (or a dict); anything malformed yields {}."""
    if isinstance(raw, (str, bytes)) or raw is None:
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def aliases_of(metadata: dict | str | None) -> list[str]:
    """String aliases from entity metadata (dict or JSON text); tolerates malformed data."""
    stored = load_metadata(metadata).get("aliases")
    if isinstance(stored, str):
        stored = [stored]
    return [a for a in stored if isinstance(a, str)] if isinstance(stored, list) else []
