"""Workspace tools: Ring-0 ambient read (list, read, grep) and Ring-1 guarded mutation.

Dependency-free and JIT: nothing is indexed or cached. Every failure is returned as an
`{"error": ...}` dictionary; nothing here raises to the caller. Mutation (`write_file`,
`patch_file`) is dual-track: files in a git repository must be clean in `git status`; loose files
are snapshotted to `~/.local/share/strand/backups` (dir 0700, files 0600) and can be reverted with
`restore_backup`. File bodies are returned to the caller only and never persisted by Strand.
"""

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..perms import secure_parent_dir

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

GREP_MAX_RESULTS = 20
GREP_MAX_BYTES = 4 * 1024
GREP_MAX_LINE_CHARS = 200
GREP_MAX_PATTERN_CHARS = 256
GREP_MAX_FILE_BYTES = 2 * 1024 * 1024
GREP_TIMEOUT_SECONDS = 10
GREP_SKIP_DIRS = frozenset({"node_modules", "__pycache__"})
MAX_WRITE_BYTES = 256 * 1024
GIT_TIMEOUT_SECONDS = 10
# Ring-1 write deny-list (on top of `is_denied_file`): never touch config, secrets or keys.
WRITE_DENIED_NAME_PATTERNS = (".env*", "*.key", "id_rsa*")


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


# --------------------------------------------------------------------------- grep (Ring 0)


def _grep_match(path: str, line_number: int, text: str) -> dict[str, Any]:
    text = text.rstrip("\r\n")
    if len(text) > GREP_MAX_LINE_CHARS:
        text = text[:GREP_MAX_LINE_CHARS] + "..."
    return {"path": path, "line_number": line_number, "line_text": text}


def _rg_matches(rg: str, pattern: str, target: Path):
    """Yield (path, line_number, text) from ripgrep's JSON stream; kills rg when abandoned."""
    cmd = [rg, "--json", "--no-config", "--max-filesize", "2M", "-e", pattern, "--", str(target)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            path = data.get("path", {}).get("text")
            text = data.get("lines", {}).get("text")
            if path is None or text is None:
                continue
            yield path, int(data.get("line_number") or 0), text
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


def _python_matches(regex: "re.Pattern[str]", target: Path):
    """Pure-Python fallback scan: skips hidden/vendored dirs, symlinks, binaries and big files."""
    if target.is_file():
        files = [target]
    else:
        def walk():
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in GREP_SKIP_DIRS)
                for name in sorted(filenames):
                    yield Path(dirpath) / name
        files = walk()
    for file in files:
        try:
            if file.is_symlink() or not file.is_file() or file.stat().st_size > GREP_MAX_FILE_BYTES:
                continue
            with open(file, "rb") as fh:
                if b"\x00" in fh.read(1024):
                    continue
            with open(file, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line[: GREP_MAX_LINE_CHARS * 5]):
                        yield str(file), lineno, line
        except OSError:
            continue


def _find_rg() -> str | None:
    return shutil.which("rg")


def grep_workspace(
    pattern: str, roots: list[Path], path: str = ".", max_results: int = GREP_MAX_RESULTS
) -> dict[str, Any]:
    """Regex search under `path` (inside a root). Max 20 matches / 4 KB; sensitive files skipped."""
    if not isinstance(pattern, str) or not pattern:
        return _error("invalid_arguments", "pattern must be a non-empty string")
    if len(pattern) > GREP_MAX_PATTERN_CHARS:
        return _error("invalid_arguments", f"pattern longer than {GREP_MAX_PATTERN_CHARS} characters")
    if isinstance(max_results, bool):
        return _error("invalid_arguments", "max_results must be an integer")
    try:
        limit = max(1, min(int(max_results), GREP_MAX_RESULTS))
    except (TypeError, ValueError):
        return _error("invalid_arguments", "max_results must be an integer")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return _error("invalid_arguments", f"invalid regex: {exc}")
    resolved, err = _check(path, roots)
    if err or resolved is None:
        return err or _error("access_denied", "invalid path")
    if not resolved.exists():
        return _error("not_found", f"no such path: {path}")
    rg = _find_rg()
    try:
        stream = _rg_matches(rg, pattern, resolved) if rg else _python_matches(regex, resolved)
        matches: list[dict[str, Any]] = []
        used = 0
        truncated = False
        for file, lineno, text in stream:
            if is_denied_file(file):
                continue
            match = _grep_match(file, lineno, text)
            size = len(json.dumps(match))
            if len(matches) >= limit or used + size > GREP_MAX_BYTES:
                truncated = True
                break
            used += size
            matches.append(match)
        stream.close()  # type: ignore[attr-defined]
    except OSError as exc:
        return _error("os_error", str(exc.strerror or exc))
    return {"pattern": pattern, "engine": "rg" if rg else "python", "matches": matches, "truncated": truncated}


# ------------------------------------------------------------------ mutation (Ring 1)


def _backup_dir() -> Path:
    return Path.home() / ".local" / "share" / "strand" / "backups"


def _protected_dirs() -> list[Path]:
    home = Path.home()
    config = os.environ.get("XDG_CONFIG_HOME")
    dirs = [home / ".config" / "strand", home / ".local" / "share" / "strand"]
    if config:
        dirs.append(Path(config) / "strand")
    out: list[Path] = []
    for d in dirs:
        try:
            out.append(d.resolve())
        except (OSError, RuntimeError):
            out.append(d)
    return out


def _write_denied(requested: str, resolved: Path) -> str | None:
    """Reason a mutation target is forbidden, or None when allowed."""
    for candidate in (requested, str(resolved)):
        name = candidate.replace("\\", "/").casefold().rstrip("/").rsplit("/", 1)[-1]
        if is_denied_file(candidate) or any(fnmatch.fnmatchcase(name, p) for p in WRITE_DENIED_NAME_PATTERNS):
            return f"'{name}' matches the sensitive file deny-list"
    for protected in _protected_dirs():
        if resolved.is_relative_to(protected):
            return "Strand's own configuration and data directories are read-only to tools"
    if ".git" in resolved.parts:
        return "git internals are read-only to tools"
    return None


def _git_root(path: Path) -> Path | None:
    probe = path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    for candidate in (probe, *probe.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_clean(repo: Path, path: Path) -> str | None:
    """None when `path` is clean in `git status`; otherwise a descriptive failure reason."""
    git = shutil.which("git")
    if git is None:
        return "file is inside a git repository but git is not installed"
    try:
        proc = subprocess.run(
            [git, "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git status failed: {type(exc).__name__}"
    if proc.returncode != 0:
        return "git status failed for this repository"
    if proc.stdout.strip():
        return "file has uncommitted changes in git; commit or stash them first"
    return None


def _atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".strand-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is None:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _snapshot(path: Path) -> str:
    """Journal the pre-mutation state of `path`; return the backup id (file name)."""
    directory = _backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(directory.parent)
    os.chmod(directory, 0o700)
    existed = path.exists()
    data = path.read_bytes() if existed else b""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup_id = f"{stamp}_{path.name}"
    _atomic_write(directory / backup_id, data, 0o600)
    meta = json.dumps({"original": str(path), "existed": existed}).encode()
    _atomic_write(directory / (backup_id + ".meta"), meta, 0o600)
    return backup_id


def _prepare_mutation(path: Any, roots: list[Path]) -> tuple[Path | None, dict[str, Any] | None]:
    resolved, err = _check(path, roots)
    if err or resolved is None:
        return None, err or _error("access_denied", "invalid path")
    reason = _write_denied(path, resolved)
    if reason:
        return None, _error("access_denied", f"write denied: {reason}")
    if resolved.exists() and not resolved.is_file():
        return None, _error("not_a_file", f"not a regular file: {path}")
    return resolved, None


def _commit_mutation(resolved: Path, data: bytes) -> dict[str, Any]:
    """Apply the dual-track safety gate, then write `data` atomically."""
    repo = _git_root(resolved)
    backup: str | None = None
    if repo is not None:
        if resolved.exists():
            reason = _git_clean(repo, resolved)
            if reason:
                return _error("git_dirty", f"refusing to modify {resolved.name}: {reason}")
    else:
        try:
            backup = _snapshot(resolved)
        except OSError as exc:
            return _error("backup_failed", f"snapshot failed, file untouched: {exc.strerror or exc}")
    try:
        existing_mode = resolved.stat().st_mode & 0o777 if resolved.exists() else None
        _atomic_write(resolved, data, existing_mode)
    except OSError as exc:
        return _error("os_error", str(exc.strerror or exc))
    return {
        "path": str(resolved),
        "bytes_written": len(data),
        "track": "git" if repo is not None else "snapshot",
        "backup": backup,
    }


def write_file(path: str, content: str, roots: list[Path]) -> dict[str, Any]:
    """Create or overwrite a text file (snapshotted when outside git, clean-gated inside git)."""
    if not isinstance(content, str):
        return _error("invalid_arguments", "content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return _error("too_large", f"content exceeds {MAX_WRITE_BYTES} bytes")
    resolved, err = _prepare_mutation(path, roots)
    if err or resolved is None:
        return err or _error("access_denied", "invalid path")
    return _commit_mutation(resolved, data)


def patch_file(path: str, search_block: str, replace_block: str, roots: list[Path]) -> dict[str, Any]:
    """Replace one exact, unique occurrence of `search_block`; disk untouched on any failure."""
    if not isinstance(search_block, str) or not isinstance(replace_block, str):
        return _error("invalid_arguments", "search_block and replace_block must be strings")
    if not search_block:
        return _error("invalid_arguments", "search_block must not be empty")
    resolved, err = _prepare_mutation(path, roots)
    if err or resolved is None:
        return err or _error("access_denied", "invalid path")
    if not resolved.exists():
        return _error("not_found", f"no such file: {path}")
    try:
        if resolved.stat().st_size > MAX_WRITE_BYTES:
            return _error("too_large", f"file exceeds {MAX_WRITE_BYTES} bytes")
        original = resolved.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return _error("binary_file", f"file is not valid UTF-8 text: {path}")
    except OSError as exc:
        return _error("os_error", str(exc.strerror or exc))
    count = original.count(search_block)
    if count == 0:
        return _error("search_not_found", "search_block does not occur in the file; nothing changed")
    if count > 1:
        return _error(
            "search_ambiguous",
            f"search_block matches {count} times; add surrounding context to make it unique. Nothing changed",
        )
    updated = original.replace(search_block, replace_block, 1).encode("utf-8")
    if len(updated) > MAX_WRITE_BYTES:
        return _error("too_large", f"result exceeds {MAX_WRITE_BYTES} bytes")
    return _commit_mutation(resolved, updated)


def restore_backup(backup: str, roots: list[Path]) -> dict[str, Any]:
    """Revert a file to a journaled snapshot (id or path inside the backup directory).

    The current state is journaled first, so a restore can itself be undone.
    """
    if not isinstance(backup, str) or not backup.strip():
        return _error("invalid_arguments", "backup must be a backup id or path")
    directory = _backup_dir().resolve()
    try:
        candidate = Path(os.path.expanduser(backup))
        snapshot = (candidate if candidate.is_absolute() else directory / candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return _error("invalid_arguments", "invalid backup reference")
    if snapshot.parent != directory or snapshot.name.endswith(".meta"):
        return _error("access_denied", "backup must live in the Strand backup directory")
    meta_path = snapshot.with_name(snapshot.name + ".meta")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        original = Path(meta["original"])
        existed = bool(meta["existed"])
        data = snapshot.read_bytes()
    except FileNotFoundError:
        return _error("not_found", f"no such backup: {backup}")
    except (OSError, ValueError, KeyError, TypeError):
        return _error("invalid_backup", f"backup metadata unreadable: {backup}")
    resolved, err = _prepare_mutation(str(original), roots)
    if err or resolved is None:
        return err or _error("access_denied", "invalid path")
    try:
        redo = _snapshot(resolved)
        if existed:
            mode = resolved.stat().st_mode & 0o777 if resolved.exists() else None
            _atomic_write(resolved, data, mode)
        else:
            resolved.unlink(missing_ok=True)
    except OSError as exc:
        return _error("os_error", str(exc.strerror or exc))
    return {"path": str(resolved), "restored_from": snapshot.name, "deleted": not existed, "redo_backup": redo}


def _schema(name: str, description: str, props: dict[str, str], required: list[str]) -> dict[str, Any]:
    properties = {
        key: {"type": "integer" if key == "max_results" else "string", "description": text}
        for key, text in props.items()
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
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
    _schema(
        "grep_workspace",
        f"Regex search for text inside approved workspace roots (ripgrep when available). Returns at "
        f"most {GREP_MAX_RESULTS} matches / {GREP_MAX_BYTES} bytes as path, line_number, line_text.",
        {
            "pattern": "Regular expression to find.",
            "path": "File or directory to search (default: workspace root).",
            "max_results": f"Max matches (up to {GREP_MAX_RESULTS}).",
        },
        ["pattern"],
    ),
    _schema(
        "write_file",
        "Create or overwrite a text file inside an approved workspace root. Files outside git are "
        "snapshotted first (undo with restore_backup); files in git must be clean. Config, .env, "
        "key and credential files are denied.",
        {"path": "File path.", "content": "Complete new file content."},
        ["path", "content"],
    ),
    _schema(
        "patch_file",
        "Replace one exact, unique block of text in an existing file. Fails without changes when "
        "search_block is missing or matches more than once.",
        {
            "path": "File path.",
            "search_block": "Exact text to find (must occur once).",
            "replace_block": "Replacement text.",
        },
        ["path", "search_block", "replace_block"],
    ),
    _schema(
        "restore_backup",
        "Undo a write_file/patch_file by restoring the snapshot id returned in its `backup` field.",
        {"backup": "Backup id (or path in the Strand backup directory)."},
        ["backup"],
    ),
]

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_directory": list_directory,
    "read_file": read_file,
    "grep_workspace": grep_workspace,
    "write_file": write_file,
    "patch_file": patch_file,
    "restore_backup": restore_backup,
}
_ALLOWED_ARGS = {
    "list_directory": ("path",),
    "read_file": ("path", "offset", "limit"),
    "grep_workspace": ("pattern", "path", "max_results"),
    "write_file": ("path", "content"),
    "patch_file": ("path", "search_block", "replace_block"),
    "restore_backup": ("backup",),
}
_REQUIRED_ARGS = {
    "list_directory": ("path",),
    "read_file": ("path",),
    "grep_workspace": ("pattern",),
    "write_file": ("path", "content"),
    "patch_file": ("path", "search_block", "replace_block"),
    "restore_backup": ("backup",),
}


def dispatch_tool_call(name: str, arguments: dict[str, Any], roots: list[Path]) -> dict[str, Any]:
    """Run a named tool; unknown tools and bad arguments yield error dictionaries."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error("unknown_tool", f"unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _error("invalid_arguments", "arguments must be an object")
    for required in _REQUIRED_ARGS[name]:
        if required not in arguments:
            return _error("invalid_arguments", f"missing required argument: {required}")
    kwargs = {k: arguments[k] for k in _ALLOWED_ARGS[name] if k in arguments}
    try:
        return handler(roots=roots, **kwargs)
    except Exception as exc:  # fail closed: never leak a traceback into the model loop
        return _error("tool_failure", f"{type(exc).__name__}: {exc}")
