"""Ring-0 ambient read tools: list and read files strictly inside approved workspace roots.

Dependency-free and JIT: nothing is indexed or cached. Every failure is returned as an
`{"error": ...}` dictionary; nothing here raises to the caller.
"""

import fnmatch
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 8 * 1024
MAX_READ_LINES = 200
DEFAULT_READ_LIMIT = 120
MAX_LIST_ENTRIES = 500

# SEC-PRIV-007: file-name globs (matched case-insensitively) that read_file never serves.
DENIED_NAME_PATTERNS = (
    ".env*",
    "*.pem",
    "*.key",
    "*.pkcs12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    "*token*",
    "*credential*",
    "*secret*",
)
DENIED_RELATIVE_SUFFIXES = (".git/config",)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": code, "message": message}


def is_safe_path(target: str | Path, roots: list[Path]) -> tuple[bool, Path | str]:
    """Return (True, resolved_path) when `target` resolves inside a root, else (False, reason)."""
    try:
        text = str(target)
        if not text.strip():
            return False, "empty path"
        if "\x00" in text:
            return False, "invalid path"
        target_str = os.path.expanduser(text)
        is_absolute = Path(target_str).is_absolute()
        resolved = Path(target_str).resolve() if is_absolute else None
    except (OSError, RuntimeError, ValueError) as exc:
        return False, f"invalid path: {exc}"
    canonical_roots: list[Path] = []
    for root in roots:
        try:
            canonical_roots.append(Path(root).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    if resolved is not None:
        for canonical in canonical_roots:
            if resolved.is_relative_to(canonical):
                return True, resolved
        return False, "path is outside the approved workspace roots"
    # Relative: try each root first (cwd is $HOME under systemd), then the process cwd, which
    # is accepted only if the result still resolves inside an approved root.
    candidates: list[tuple[Path, Path]] = []  # (candidate, bounding root)
    for canonical in canonical_roots:
        try:
            candidates.append(((canonical / target_str).resolve(), canonical))
        except (OSError, RuntimeError, ValueError):
            continue
    try:
        cwd_candidate = (Path(os.getcwd()) / target_str).resolve()
        for canonical in canonical_roots:
            if cwd_candidate.is_relative_to(canonical):
                candidates.append((cwd_candidate, canonical))
                break
    except (OSError, RuntimeError, ValueError):
        pass
    first_valid: Path | None = None
    for candidate, canonical in candidates:
        if not candidate.is_relative_to(canonical):
            continue
        if candidate.exists():
            return True, candidate
        if first_valid is None:
            first_valid = candidate
    if first_valid is not None:
        return True, first_valid
    return False, "path is outside the approved workspace roots"


def is_denied_file(path: Path | str) -> bool:
    """True when `path` names a sensitive file (secrets, keys, tokens, `.git/config`)."""
    posix = str(path).replace("\\", "/").casefold().rstrip("/")
    name = posix.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in DENIED_NAME_PATTERNS):
        return True
    return any(posix == suffix or posix.endswith("/" + suffix) for suffix in DENIED_RELATIVE_SUFFIXES)


def _check(path: Any, roots: list[Path]) -> tuple[Path | None, dict[str, Any] | None]:
    if not isinstance(path, str):
        return None, _error("invalid_arguments", "path must be a string")
    ok, result = is_safe_path(path, roots)
    if not ok:
        return None, _error("access_denied", str(result))
    return Path(result), None


def list_directory(path: str, roots: list[Path]) -> dict[str, Any]:
    """Single-level listing sorted by name: [{name, type, size}] (size in bytes)."""
    resolved, err = _check(path, roots)
    if err or resolved is None:
        return err or _error("access_denied", "invalid path")
    if not resolved.exists():
        return _error("not_found", f"no such path: {path}")
    if not resolved.is_dir():
        return _error("not_a_directory", f"not a directory: {path}")
    try:
        children = sorted(resolved.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        return _error("os_error", str(exc.strerror or exc))
    entries: list[dict[str, Any]] = []
    for child in children[:MAX_LIST_ENTRIES]:
        try:
            is_dir = child.is_dir()
            size = 0 if is_dir else child.stat().st_size
        except OSError:
            continue
        entries.append({"name": child.name, "type": "directory" if is_dir else "file", "size": size})
    return {
        "path": str(resolved),
        "entries": entries,
        "truncated": len(children) > MAX_LIST_ENTRIES,
    }


def read_file(
    path: str, roots: list[Path], offset: int = 1, limit: int = DEFAULT_READ_LIMIT
) -> dict[str, Any]:
    """Read lines [offset, offset+limit) (1-based), capped at 200 lines and 8 KB per call."""
    resolved, err = _check(path, roots)
    if err or resolved is None:
        return err or _error("access_denied", "invalid path")
    if is_denied_file(path) or is_denied_file(resolved):
        return _error("Access denied: sensitive file policy", f"Access denied: '{path}' matches sensitive file deny-list")
    if isinstance(offset, bool) or isinstance(limit, bool):
        return _error("invalid_arguments", "offset and limit must be integers")
    try:
        offset, limit = int(offset), int(limit)
    except (TypeError, ValueError):
        return _error("invalid_arguments", "offset and limit must be integers")
    if offset < 1 or limit < 1:
        return _error("invalid_arguments", "offset and limit must be >= 1")
    limit = min(limit, MAX_READ_LINES)
    if not resolved.exists():
        return _error("not_found", f"no such path: {path}")
    if not resolved.is_file():
        return _error("not_a_file", f"not a regular file: {path}")
    lines: list[str] = []
    used = 0
    byte_truncated = False
    more = False
    try:
        with open(resolved, "rb") as fh:
            if b"\x00" in fh.read(1024):
                return _error("binary_file", f"binary file not readable: {path}")
            fh.seek(0)
            lineno = 0
            while True:
                # Bounded read: never buffer more than MAX_READ_BYTES + 1 bytes of one line (ERG-IO-006).
                raw = fh.readline(MAX_READ_BYTES + 1)
                if not raw:
                    break
                partial = len(raw) > MAX_READ_BYTES and not raw.endswith(b"\n")
                lineno += 1
                if lineno < offset:
                    while partial:  # skip the rest of an over-long line in bounded chunks
                        chunk = fh.readline(MAX_READ_BYTES)
                        partial = bool(chunk) and not chunk.endswith(b"\n")
                    continue
                if len(lines) >= limit:
                    more = True
                    break
                if used + len(raw) > MAX_READ_BYTES:
                    byte_truncated = more = True
                    raw = raw[: MAX_READ_BYTES - used]
                    if raw:
                        lines.append(raw.decode("utf-8", errors="ignore"))
                    break
                used += len(raw)
                lines.append(raw.decode("utf-8", errors="replace"))
    except OSError as exc:
        return _error("os_error", str(exc.strerror or exc))
    return {
        "path": str(resolved),
        "offset": offset,
        "lines_returned": len(lines),
        "content": "".join(lines),
        "truncated": more,
        "byte_truncated": byte_truncated,
        "next_offset": offset + len(lines) if more else None,
    }


WORKSPACE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List one directory level inside an approved workspace root "
            "(names, file/directory type, size in bytes).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a window of lines from a text file inside an approved workspace "
            f"root. Max {MAX_READ_LINES} lines or {MAX_READ_BYTES} bytes per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path."},
                    "offset": {"type": "integer", "description": "1-based first line."},
                    "limit": {"type": "integer", "description": "Max lines to return."},
                },
                "required": ["path"],
            },
        },
    },
]

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_directory": list_directory,
    "read_file": read_file,
}
_ALLOWED_ARGS = {"list_directory": ("path",), "read_file": ("path", "offset", "limit")}


def dispatch_tool_call(name: str, arguments: dict[str, Any], roots: list[Path]) -> dict[str, Any]:
    """Run a named tool; unknown tools and bad arguments yield error dictionaries."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error("unknown_tool", f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _error("invalid_arguments", "arguments must be an object")
    if "path" not in arguments:
        return _error("invalid_arguments", "missing required argument: path")
    kwargs = {k: arguments[k] for k in _ALLOWED_ARGS[name] if k in arguments}
    try:
        return handler(roots=roots, **kwargs)
    except Exception as exc:  # fail closed: never leak a traceback into the model loop
        return _error("tool_failure", f"{type(exc).__name__}: {exc}")

