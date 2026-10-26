"""Environment-driven daemon configuration."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_UPSTREAM_URL = "https://api.openai.com/v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_DB_PATH = "~/.config/cortext/memory.db"


@dataclass(frozen=True, slots=True)
class Config:
    upstream_url: str = DEFAULT_UPSTREAM_URL
    upstream_api_key: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db_path: str = DEFAULT_DB_PATH

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            upstream_url=env.get("CORTEXT_UPSTREAM_URL") or DEFAULT_UPSTREAM_URL,
            upstream_api_key=env.get("CORTEXT_UPSTREAM_API_KEY", ""),
            host=env.get("CORTEXT_HOST") or DEFAULT_HOST,
            port=int(env.get("CORTEXT_PORT") or DEFAULT_PORT),
            db_path=env.get("CORTEXT_DB_PATH") or DEFAULT_DB_PATH,
        )
