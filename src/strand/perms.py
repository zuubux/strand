"""Scoped POSIX permission enforcement for Strand's storage and socket directories (SEC-PERM-001).

Strand only ever tightens a directory to 0700 when it is a dedicated, Strand-managed directory
(its own name is `strand`: `~/.config/strand`, `~/.local/share/strand`, `$XDG_RUNTIME_DIR/strand`).
Any other parent (`$HOME`, `/tmp`, an arbitrary directory the operator pointed a path at) is never
mutated: the leaf files and socket are secured at 0600 by their owners and a warning is logged
when the unmanaged parent is group/world accessible.

This module must only depend on the standard library and `strand.logging`.
"""

import os
import stat
from pathlib import Path

from .logging import get_logger

logger = get_logger("perms")

MANAGED_DIR_NAME = "strand"


def is_managed_dir(path: Path) -> bool:
    """True when `path` is a dedicated Strand directory (never `$HOME`, `/tmp` or any other root)."""
    try:
        if path.resolve() == Path.home().resolve():
            return False
    except (OSError, RuntimeError):
        pass
    return path.name == MANAGED_DIR_NAME


def secure_parent_dir(path: Path) -> None:
    """Enforce 0700 on a managed directory; for an unmanaged one only warn, never chmod it."""
    if is_managed_dir(path):
        try:
            os.chmod(path, 0o700)
        except FileNotFoundError:
            pass
        except PermissionError:
            logger.debug("Cannot chmod %s to 700 (permission denied)", path)
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if mode & 0o077:
        logger.warning(
            "Parent directory %s is not Strand-managed and has permissive mode %o; leaving it "
            "unchanged (only Strand's own files are restricted to 0600). Use a dedicated "
            "'strand' directory for full isolation.",
            path,
            mode,
        )
