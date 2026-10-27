"""Lightweight regex-based secret masking applied before distillation."""

import math
import re

REDACTED = "[REDACTED_SECRET]"

# Order matters: multi-line / composite patterns first so inner tokens don't split them.
_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"-----BEGIN[ A-Z0-9_-]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z0-9_-]*PRIVATE KEY-----",
        r"[a-zA-Z0-9+]+:\/\/[^:\s]+:[^@\s]+@[^\s]+",
        r"eyJ[a-zA-Z0-9_\-]+\.eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+",
        r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}",
        r"github_pat_[a-zA-Z0-9_\-]{50,}",
        r"ghp_[a-zA-Z0-9]{36}",
        r"sk-[a-zA-Z0-9_\-]{20,}",
        r"AIza[0-9A-Za-z\-_]{35}",
        r"AKIA[0-9A-Z]{16}",
    )
)

_MIN_ENTROPY_LEN = 24
_ENTROPY_THRESHOLD = 3.5


def sanitize_text(text: str) -> str:
    """Mask credentials in `text` with [REDACTED_SECRET]."""
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _entropy(token: str) -> float:
    counts = {c: token.count(c) for c in set(token)}
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def is_secret_token(token: str) -> bool:
    """Whether a candidate entity name looks like a credential or high-entropy token."""
    if not isinstance(token, str):
        return False
    token = token.strip()
    if not token:
        return False
    if "REDACTED_SECRET" in token or sanitize_text(token) != token:
        return True
    if len(token) >= _MIN_ENTROPY_LEN and not re.search(r"\s", token):
        has_digit = any(c.isdigit() for c in token)
        has_alpha = any(c.isalpha() for c in token)
        return has_digit and has_alpha and _entropy(token) >= _ENTROPY_THRESHOLD
    return False
