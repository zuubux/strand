"""Dependency-free inspection of the local workstation (imports nothing from `strand`)."""

import getpass
import os
import platform
import socket
from pathlib import Path
from typing import Any

PROC_CPUINFO = "/proc/cpuinfo"
PROC_MEMINFO = "/proc/meminfo"


def _user() -> str:
    try:
        return getpass.getuser() or "operator"
    except Exception:  # noqa: BLE001 - getuser raises OSError/KeyError without a passwd entry
        return os.environ.get("USER") or "operator"


def _hostname() -> str:
    return platform.node() or socket.gethostname() or "localhost"


def _os_release() -> str:
    try:
        release = platform.freedesktop_os_release()
        pretty = release.get("PRETTY_NAME") or release.get("NAME")
        if pretty:
            return str(pretty)
    except (OSError, AttributeError):
        pass
    return platform.platform()


def _cpu_model(path: str = PROC_CPUINFO) -> str:
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "model name" and value.strip():
                return " ".join(value.split())
    except OSError:
        pass
    return ""


def _total_ram_gb(path: str = PROC_MEMINFO) -> float:
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / (1024 * 1024), 1)  # kB -> GB
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def get_host_context() -> dict[str, Any]:
    """Snapshot of the operator's environment; unavailable values are '' (or 0.0 for RAM)."""
    return {
        "user": _user(),
        "hostname": _hostname(),
        "os": _os_release(),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "cpu": _cpu_model(),
        "ram_gb": _total_ram_gb(),
    }


def format_host_context(info: dict[str, Any]) -> str:
    """One-line, human-readable rendering of `get_host_context()` output (empty fields skipped)."""
    desktop = " / ".join(p for p in (info.get("desktop"), info.get("session")) if p)
    ram = info.get("ram_gb") or 0
    parts = [
        f"user {info['user']}" if info.get("user") else "",
        f"host {info['hostname']}" if info.get("hostname") else "",
        str(info.get("os") or ""),
        f"desktop {desktop}" if desktop else "",
        f"CPU {info['cpu']}" if info.get("cpu") else "",
        f"{ram:g} GB RAM" if ram else "",
    ]
    return ", ".join(p for p in parts if p) or "unknown host"
