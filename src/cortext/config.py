"""Daemon configuration: environment variables > ~/.config/cortext/config.json > defaults."""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_UPSTREAM_URL = "https://api.openai.com/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_DB_PATH = "~/.config/cortext/memory.db"
DEFAULT_LOG_LEVEL = "INFO"
# sqlite-vec returns L2 distance: unit vectors at cosine distance ~0.35 sit at L2 ~0.83,
# orthogonal (unrelated) ones at >= 1.414.
DEFAULT_DISTANCE_THRESHOLD = 1.0

# config.json key -> environment variable
ENV_VARS = {
    "upstream_url": "CORTEXT_UPSTREAM_URL",
    "upstream_api_key": "CORTEXT_UPSTREAM_API_KEY",
    "api_key": "CORTEXT_API_KEY",
    "host": "CORTEXT_HOST",
    "port": "CORTEXT_PORT",
    "db_path": "CORTEXT_DB_PATH",
    "log_level": "CORTEXT_LOG_LEVEL",
    "socket_path": "CORTEXT_SOCKET_PATH",
    "extractor_model": "CORTEXT_EXTRACTOR_MODEL",
    "extractor_url": "CORTEXT_EXTRACTOR_URL",
    "distance_threshold": "CORTEXT_DISTANCE_THRESHOLD",
}


def default_config_path() -> Path:
    return Path.home() / ".config" / "cortext" / "config.json"


@dataclass(frozen=True, slots=True)
class Config:
    upstream_url: str = DEFAULT_UPSTREAM_URL
    upstream_api_key: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db_path: str = DEFAULT_DB_PATH
    log_level: str = DEFAULT_LOG_LEVEL
    # Inbound key clients must present to Cortext (distinct from upstream_api_key).
    api_key: str = ""
    # UNIX domain socket path; empty means "not explicitly configured".
    socket_path: str = ""
    # Local OpenAI-compatible SLM used for structured extraction; empty model = heuristic.
    extractor_model: str = ""
    extractor_url: str = ""
    # Max L2 distance for a semantic (vector KNN) match in the resolver.
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD
    # True when host or port came from the environment or config.json (forces TCP).
    tcp_explicit: bool = field(default=False, compare=False)

    def effective_socket_path(self, env: Mapping[str, str] | None = None) -> str:
        """Socket path to bind, or "" for TCP.

        Explicit `socket_path` wins. Otherwise, with no explicit host/port and
        $XDG_RUNTIME_DIR set, default to $XDG_RUNTIME_DIR/cortext/cortext.sock.
        """
        if self.socket_path:
            return self.socket_path
        env = os.environ if env is None else env
        runtime_dir = env.get("XDG_RUNTIME_DIR")
        if runtime_dir and not self.tcp_explicit:
            return f"{runtime_dir}/cortext/cortext.sock"
        return ""

    def is_socket_mode(self, env: Mapping[str, str] | None = None) -> bool:
        return bool(self.effective_socket_path(env))

    def get_bind_target(
        self, env: Mapping[str, str] | None = None
    ) -> tuple[str, str | int]:
        """("uds", path) in socket mode, else ("tcp", host); port is `self.port`."""
        path = self.effective_socket_path(env)
        return ("uds", path) if path else ("tcp", self.host)

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, file_values: Mapping[str, Any] | None = None
    ) -> "Config":
        """Resolve each field from `env`, then `file_values`, then the default."""
        env = os.environ if env is None else env
        file_values = file_values or {}

        def pick(key: str, default: Any) -> Any:
            return env.get(ENV_VARS[key]) or file_values.get(key) or default

        return cls(
            upstream_url=str(pick("upstream_url", DEFAULT_UPSTREAM_URL)),
            upstream_api_key=str(pick("upstream_api_key", "")),
            api_key=str(pick("api_key", "")),
            host=str(pick("host", DEFAULT_HOST)),
            port=int(pick("port", DEFAULT_PORT)),
            db_path=str(pick("db_path", DEFAULT_DB_PATH)),
            log_level=str(pick("log_level", DEFAULT_LOG_LEVEL)).upper(),
            socket_path=str(pick("socket_path", "")),
            extractor_model=str(pick("extractor_model", "")),
            extractor_url=str(pick("extractor_url", "")),
            distance_threshold=float(pick("distance_threshold", DEFAULT_DISTANCE_THRESHOLD)),
            tcp_explicit=bool(pick("host", "") or pick("port", "")),
        )


def _read_file(path: Path, strict: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if strict:
            raise ValueError(f"cannot read {path}: {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"{path} must contain a JSON object")
        return {}
    return data


def load_config(
    env: Mapping[str, str] | None = None, config_path: Path | None = None
) -> Config:
    """Load config with precedence env > config.json > defaults. A bad file is ignored."""
    path = config_path if config_path is not None else default_config_path()
    return Config.from_env(env, _read_file(path))


def save_config_value(key: str, value: Any, config_path: Path | None = None) -> None:
    """Persist one setting to config.json (atomic write, mode 0600, parents created)."""
    if key not in ENV_VARS:
        raise KeyError(f"unknown config key {key!r}; valid keys: {', '.join(ENV_VARS)}")
    if key == "port":
        value = int(value)
    elif key == "distance_threshold":
        value = float(value)
    elif key == "log_level":
        value = str(value).upper()
    path = config_path if config_path is not None else default_config_path()
    data = _read_file(path, strict=True)
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
