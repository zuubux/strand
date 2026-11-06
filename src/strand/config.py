"""Daemon configuration: environment variables > ~/.config/strand/config.json > defaults."""

import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .common import (
    DEFAULT_COMPACTION_THRESHOLD_HOURS,
    DEFAULT_CONTINUITY_HORIZON_DAYS,
    DEFAULT_CONTINUITY_MAX_EPISODES,
    DEFAULT_DEDUP_DISTANCE_THRESHOLD,
)
from .perms import secure_parent_dir

DEFAULT_UPSTREAM_URL = "https://api.openai.com/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_DB_PATH = "~/.config/strand/memory.db"
DEFAULT_LOG_LEVEL = "INFO"
# sqlite-vec returns L2 distance: unit vectors at cosine distance ~0.35 sit at L2 ~0.83,
# orthogonal (unrelated) ones at >= 1.414.
DEFAULT_DISTANCE_THRESHOLD = 1.0
DEFAULT_DECAY_HALF_LIFE_DAYS = 30.0
DEFAULT_WORKSPACE_ROOTS = ("~/Projects",)
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
XAI_BASE_URL = "https://api.x.ai/v1"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_URLS = (OLLAMA_BASE_URL, "http://localhost:11434")

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
    "workspace_roots": "STRAND_WORKSPACE_ROOTS",
    "tools_enabled": "STRAND_TOOLS_ENABLED",
    "enable_host_telemetry": "STRAND_ENABLE_HOST_TELEMETRY",
    "web_search_enabled": "STRAND_WEB_SEARCH_ENABLED",
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


def _as_roots(value: Any) -> list[str]:
    """Parse workspace roots: a JSON list, or a string separated by `os.pathsep` or commas."""
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        text = str(value).strip()
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except ValueError:
                raise ValueError(f"invalid workspace_roots {value!r}") from None
            if not isinstance(loaded, list):
                raise ValueError(f"invalid workspace_roots {value!r}")
            items = [str(v) for v in loaded]
        else:
            items = text.replace(",", os.pathsep).split(os.pathsep)
    return [i.strip() for i in items if i.strip()]


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
    upstream_api_key: str = field(default="", repr=False)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db_path: str = DEFAULT_DB_PATH
    log_level: str = DEFAULT_LOG_LEVEL
    # Inbound key clients must present to Strand (distinct from upstream_api_key).
    api_key: str = field(default="", repr=False)
    # UNIX domain socket path; empty means "not explicitly configured".
    socket_path: str = ""
    # Local OpenAI-compatible SLM used for structured extraction; empty model = heuristic.
    extractor_model: str = ""
    extractor_url: str = ""
    # Max L2 distance for a semantic (vector KNN) match in the resolver.
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD
    # Max L2 distance for merging a candidate entity into an existing one at ingestion.
    dedup_distance_threshold: float = DEFAULT_DEDUP_DISTANCE_THRESHOLD
    # Temporal access decay: facts fade by 2^(-days_since_access / half_life); on by default (Balanced, 30d).
    decay_enabled: bool = True
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
    # Ring-0 ambient read: approved workspace roots and the tool-runner master switch.
    workspace_roots: list[str] = field(default_factory=lambda: list(DEFAULT_WORKSPACE_ROOTS))
    tools_enabled: bool = True
    # SEC-PRIV-002: ambient hardware specs (OS, CPU, RAM) may leave the machine only when True.
    enable_host_telemetry: bool = False
    # SEC-PRIV-007: scrub secrets from tool output before it is sent upstream (config.json key).
    redact_secrets: bool = True
    # Ground Google Gemini upstream calls with native Google Search (results stay ephemeral).
    web_search_enabled: bool = False
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

    def resolved_workspace_roots(self) -> list[Path]:
        """Canonical roots (`~` expanded, symlinks resolved) so they cannot be escaped."""
        return [Path(r).expanduser().resolve() for r in self.workspace_roots]

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
                else file_values.get("decay_enabled", True)
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
            workspace_roots=_as_roots(pick("workspace_roots", list(DEFAULT_WORKSPACE_ROOTS))),
            tools_enabled=_as_bool(pick("tools_enabled", True)),
            enable_host_telemetry=_as_bool(pick("enable_host_telemetry", False)),
            redact_secrets=_as_bool(file_values.get("redact_secrets", True)),
            web_search_enabled=_as_bool(pick("web_search_enabled", False)),
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
    elif key in ("decay_enabled", "tools_enabled", "enable_host_telemetry", "web_search_enabled"):
        value = _as_bool(value)
    elif key == "workspace_roots":
        value = _as_roots(value)
    elif key == "log_level":
        value = str(value).upper()
    elif key == "model_default":
        value = str(value).strip()
    save_config({key: value}, config_path)


def save_config(updates: Mapping[str, Any], config_path: Path | None = None) -> None:
    """Merge `updates` into config.json (atomic write, file 0600, parent dir 0700)."""
    path = config_path if config_path is not None else default_config_path()
    data = _read_file(path, strict=True)
    data.update(updates)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure_parent_dir(path.parent)
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


def detect_ollama_model(timeout: float = 1.5) -> str:
    """Name of the first model served by local Ollama, or "" when unreachable or empty."""
    try:
        resp = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=timeout)
        resp.raise_for_status()
        for item in resp.json().get("models", []):
            name = str(item.get("name") or item.get("model") or "").strip()
            if name:
                return name
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return ""


def engine_updates(provider: str, api_key: str = "", model: str = "", endpoint_url: str = "") -> dict[str, str]:
    """config.json values for a provider: "gemini", "openrouter", "xai", "ollama" or "custom".

    A non-empty `endpoint_url` overrides the preset upstream URL (required for "custom").

    Raises ValueError for an unknown provider or missing required input.
    """
    provider = provider.strip().lower()
    api_key, model, url = api_key.strip(), model.strip(), endpoint_url.strip()
    if provider == "gemini":
        if not api_key:
            raise ValueError("a Gemini API key is required")
        return {
            "upstream_url": url or GEMINI_BASE_URL,
            "upstream_api_key": api_key,
            "model_default": model or DEFAULT_GEMINI_MODEL,
        }
    if provider in ("openrouter", "xai"):
        if not api_key or not model:
            raise ValueError("an API key and a model are required")
        preset = OPENROUTER_BASE_URL if provider == "openrouter" else XAI_BASE_URL
        return {"upstream_url": url or preset, "upstream_api_key": api_key, "model_default": model}
    if provider == "ollama":
        model = model or detect_ollama_model()
        if not model:
            raise ValueError(f"no Ollama model found at {OLLAMA_BASE_URL}")
        # A stale cloud key must never be sent to the local engine.
        return {"upstream_url": f"{OLLAMA_BASE_URL}/v1", "upstream_api_key": "", "model_default": model}
    if provider == "custom":
        if not url or not model:
            raise ValueError("a base URL and a model are required")
        return {"upstream_url": url, "upstream_api_key": api_key, "model_default": model}
    raise ValueError(f"unknown provider {provider!r}")


def is_engine_configured(config: "Config") -> bool:
    """True when an upstream API key is set or the upstream is local Ollama."""
    if config.upstream_api_key:
        return True
    return config.upstream_url.rstrip("/").removesuffix("/v1") in OLLAMA_URLS
