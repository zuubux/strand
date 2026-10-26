"""Daemon configuration: environment variables > ~/.config/cortext/config.json > defaults."""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_UPSTREAM_URL = "https://api.openai.com/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_DB_PATH = "~/.config/cortext/memory.db"
DEFAULT_LOG_LEVEL = "INFO"

# config.json key -> environment variable
ENV_VARS = {
    "upstream_url": "CORTEXT_UPSTREAM_URL",
    "upstream_api_key": "CORTEXT_UPSTREAM_API_KEY",
    "host": "CORTEXT_HOST",
    "port": "CORTEXT_PORT",
    "db_path": "CORTEXT_DB_PATH",
    "log_level": "CORTEXT_LOG_LEVEL",
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
            host=str(pick("host", DEFAULT_HOST)),
            port=int(pick("port", DEFAULT_PORT)),
            db_path=str(pick("db_path", DEFAULT_DB_PATH)),
            log_level=str(pick("log_level", DEFAULT_LOG_LEVEL)).upper(),
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
