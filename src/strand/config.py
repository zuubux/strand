"""Daemon configuration: environment variables > ~/.config/strand/config.json > defaults."""

import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_UPSTREAM_URL = "https://api.openai.com/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_DB_PATH = "~/.config/strand/memory.db"
DEFAULT_LOG_LEVEL = "INFO"
# sqlite-vec returns L2 distance: unit vectors at cosine distance ~0.35 sit at L2 ~0.83,
# orthogonal (unrelated) ones at >= 1.414.
DEFAULT_DISTANCE_THRESHOLD = 1.0
# Tight L2 cutoff (~cosine similarity 0.90) for merging near-duplicate entities on ingestion.
DEFAULT_DEDUP_DISTANCE_THRESHOLD = 0.45
DEFAULT_DECAY_HALF_LIFE_DAYS = 30.0
DEFAULT_COMPACTION_THRESHOLD_HOURS = 24.0
DEFAULT_CONTINUITY_HORIZON_DAYS = 7.0
DEFAULT_CONTINUITY_MAX_EPISODES = 2

# config.json key -> environment variable
ENV_VARS = {
    "upstream_url": "STRAND_UPSTREAM_URL",
    "upstream_api_key": "STRAND_UPSTREAM_API_KEY",
    "api_key": "STRAND_API_KEY",
    "host": "STRAND_HOST",
    "port": "STRAND_PORT",
    "db_path": "STRAND_DB_PATH",
    "log_level": "STRAND_LOG_LEVEL",
    "socket_path": "STRAND_SOCKET_PATH",
    "extractor_model": "STRAND_EXTRACTOR_MODEL",
    "extractor_url": "STRAND_EXTRACTOR_URL",
    "distance_threshold": "STRAND_DISTANCE_THRESHOLD",
    "dedup_distance_threshold": "STRAND_DEDUP_DISTANCE_THRESHOLD",
    "decay_enabled": "STRAND_DECAY_ENABLED",
    "decay_half_life_days": "STRAND_DECAY_HALF_LIFE_DAYS",
    "compaction_threshold_hours": "STRAND_COMPACTION_THRESHOLD_HOURS",
    "continuity_horizon_days": "STRAND_CONTINUITY_HORIZON_DAYS",
    "continuity_max_episodes": "STRAND_CONTINUITY_MAX_EPISODES",
    "model_default": "STRAND_MODEL_DEFAULT",
    "persona_prompt": "STRAND_PERSONA_PROMPT",
}


def _as_bool(value: Any) -> bool:
    """Parse a config boolean (bool, or 1/0, true/false, yes/no, on/off strings)."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"invalid boolean {value!r}; use true or false")


def _validate_half_life(value: Any) -> float:
    """Return `value` as a finite positive float, else raise a descriptive ValueError."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"decay_half_life_days must be a finite number > 0, got {value!r}") from None
    if not (math.isfinite(number) and number > 0):
        raise ValueError(f"decay_half_life_days must be a finite number > 0, got {value!r}")
    return number


def default_config_path() -> Path:
    return Path.home() / ".config" / "strand" / "config.json"


@dataclass(frozen=True, slots=True)
class Config:
    upstream_url: str = DEFAULT_UPSTREAM_URL
    upstream_api_key: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db_path: str = DEFAULT_DB_PATH
    log_level: str = DEFAULT_LOG_LEVEL
    # Inbound key clients must present to Strand (distinct from upstream_api_key).
    api_key: str = ""
    # UNIX domain socket path; empty means "not explicitly configured".
    socket_path: str = ""
    # Local OpenAI-compatible SLM used for structured extraction; empty model = heuristic.
    extractor_model: str = ""
    extractor_url: str = ""
    # Max L2 distance for a semantic (vector KNN) match in the resolver.
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD
    # Max L2 distance for merging a candidate entity into an existing one at ingestion.
    dedup_distance_threshold: float = DEFAULT_DEDUP_DISTANCE_THRESHOLD
    # Temporal access decay: facts fade by 2^(-days_since_access / half_life); off by default.
    decay_enabled: bool = False
    decay_half_life_days: float = DEFAULT_DECAY_HALF_LIFE_DAYS
    # A session is compactable once its newest uncompacted episode is this many hours old.
    compaction_threshold_hours: float = DEFAULT_COMPACTION_THRESHOLD_HOURS
    # Episodic stitching (COG-3): look-back window and cap for [Recent Continuity].
    continuity_horizon_days: float = DEFAULT_CONTINUITY_HORIZON_DAYS
    continuity_max_episodes: int = DEFAULT_CONTINUITY_MAX_EPISODES
    # Default chat model for clients such as the GUI; empty = client-side fallback.
    model_default: str = ""
    # Optional bedrock persona anchor, injected ahead of the personality matrix.
    persona_prompt: str = ""
    # True when host or port came from the environment or config.json (forces TCP).
    tcp_explicit: bool = field(default=False, compare=False)

    def effective_socket_path(self, env: Mapping[str, str] | None = None) -> str:
        """Socket path to bind, or "" for TCP.

        Explicit `socket_path` wins. Otherwise, with no explicit host/port and
        $XDG_RUNTIME_DIR set, default to $XDG_RUNTIME_DIR/strand/strand.sock.
        """
        if self.socket_path:
            return self.socket_path
        env = os.environ if env is None else env
        runtime_dir = env.get("XDG_RUNTIME_DIR")
        if runtime_dir and not self.tcp_explicit:
            return f"{runtime_dir}/strand/strand.sock"
        return ""

    def is_socket_mode(self, env: Mapping[str, str] | None = None) -> bool:
        return bool(self.effective_socket_path(env))

    def get_bind_target(
        self, env: Mapping[str, str] | None = None
    ) -> tuple[str, str | int]:
        """("uds", path) in socket mode, else ("tcp", host); port is `self.port`."""
        path = self.effective_socket_path(env)
        return ("uds", path) if path else ("tcp", self.host)

    def validate(self) -> "Config":
        """Raise ValueError for settings that would only fail later at runtime; return self."""
        _validate_half_life(self.decay_half_life_days)
        return self

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, file_values: Mapping[str, Any] | None = None
    ) -> "Config":
        """Resolve each field from `env`, then `file_values`, then the default."""
        env = os.environ if env is None else env
        file_values = file_values or {}

        def pick(key: str, default: Any) -> Any:
            # Empty env vars count as unset; file values (incl. 0, 0.0, "") are kept.
            val = env.get(ENV_VARS[key])
            if val is None or val == "":
                val = file_values.get(key)
            return default if val is None else val

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
            dedup_distance_threshold=float(
                pick("dedup_distance_threshold", DEFAULT_DEDUP_DISTANCE_THRESHOLD)
            ),
            decay_enabled=_as_bool(
                env[ENV_VARS["decay_enabled"]]
                if env.get(ENV_VARS["decay_enabled"]) not in (None, "")
                else file_values.get("decay_enabled", False)
            ),
            decay_half_life_days=float(pick("decay_half_life_days", DEFAULT_DECAY_HALF_LIFE_DAYS)),
            compaction_threshold_hours=float(
                pick("compaction_threshold_hours", DEFAULT_COMPACTION_THRESHOLD_HOURS)
            ),
            continuity_horizon_days=float(
                pick("continuity_horizon_days", DEFAULT_CONTINUITY_HORIZON_DAYS)
            ),
            continuity_max_episodes=int(
                pick("continuity_max_episodes", DEFAULT_CONTINUITY_MAX_EPISODES)
            ),
            model_default=str(pick("model_default", "")).strip(),
            persona_prompt=str(pick("persona_prompt", "")).strip(),
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
    env: Mapping[str, str] | None = None, config_path: Path | None = None, strict: bool = False
) -> Config:
    """Load config with precedence env > config.json > defaults.

    A bad file is ignored unless `strict` (used by the daemon), which raises ValueError.
    """
    path = config_path if config_path is not None else default_config_path()
    file_values = _read_file(path, strict=strict)
    if strict:
        environ = os.environ if env is None else env
        raw = environ.get(ENV_VARS["decay_half_life_days"])
        if raw is None or raw == "":
            raw = file_values.get("decay_half_life_days")
        if raw is not None:
            _validate_half_life(raw)
    config = Config.from_env(env, file_values)
    return config.validate() if strict else config


def save_config_value(key: str, value: Any, config_path: Path | None = None) -> None:
    """Persist one setting to config.json (atomic write, mode 0600, parents created)."""
    if key not in ENV_VARS:
        raise KeyError(f"unknown config key {key!r}; valid keys: {', '.join(ENV_VARS)}")
    if key == "decay_half_life_days":
        value = _validate_half_life(value)
    elif key in ("port", "continuity_max_episodes"):
        value = int(value)
    elif key in (
        "distance_threshold",
        "dedup_distance_threshold",
        "compaction_threshold_hours",
        "continuity_horizon_days",
    ):
        value = float(value)
    elif key == "decay_enabled":
        value = _as_bool(value)
    elif key == "log_level":
        value = str(value).upper()
    elif key == "model_default":
        value = str(value).strip()
    path = config_path if config_path is not None else default_config_path()
    data = _read_file(path, strict=True)
    data[key] = value
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        pass
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
