"""Daemon entrypoint: wires the DB, session ledger, resolver and proxy, then serves."""

import uvicorn

from .config import Config
from .db import DatabaseManager
from .proxy import create_app
from .resolver import ContextResolver
from .session import SessionLedger


def build_app(config: Config):
    db = DatabaseManager(config.db_path)
    resolver = ContextResolver(db, SessionLedger())
    return create_app(config, resolver)


def main() -> None:
    """Run the daemon. uvicorn traps SIGINT/SIGTERM and runs the app lifespan shutdown,
    which closes the HTTP client and the SQLite connection cleanly."""
    config = Config.from_env()
    uvicorn.run(build_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
