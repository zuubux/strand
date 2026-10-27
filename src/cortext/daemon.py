"""Daemon entrypoint: wires the DB, session ledger, resolver and proxy, then serves."""

import asyncio
import os
from pathlib import Path

import uvicorn

from .config import Config, load_config
from .db import DatabaseManager
from .distill import DistillationEngine
from .embed import create_embedder
from .extractors import DEFAULT_EXTRACTOR_URL, LLMExtractor
from .logging import get_logger, setup_logging
from .proxy import create_app
from .resolver import ContextResolver
from .session import SessionLedger


def build_app(config: Config):
    # Lazy: the model loads on first embed. None (lexical-only) when fastembed is missing.
    embedder = create_embedder()
    db = DatabaseManager(config.db_path, embedder=embedder)
    resolver = ContextResolver(
        db,
        SessionLedger(),
        embedder=embedder,
        distance_threshold=config.distance_threshold,
        decay_enabled=config.decay_enabled,
        decay_half_life_days=config.decay_half_life_days,
    )
    extractor = None
    if config.extractor_model:
        extractor = LLMExtractor(
            config.extractor_url or DEFAULT_EXTRACTOR_URL, config.extractor_model
        )
    engine = DistillationEngine(
        db, extractor=extractor, dedup_distance_threshold=config.dedup_distance_threshold
    )
    return create_app(config, resolver, distill_engine=engine)


def prepare_socket(socket_path: str) -> None:
    """Create the 0700 parent dir and remove any stale socket file."""
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.unlink(missing_ok=True)


async def _secure_socket(server: uvicorn.Server, socket_path: str) -> None:
    """Wait for the socket to be bound, then restrict it to 0600."""
    while not server.started:
        if server.should_exit:
            return
        await asyncio.sleep(0.01)
    os.chmod(socket_path, 0o600)


async def _serve_uds(app, socket_path: str) -> None:
    server = uvicorn.Server(uvicorn.Config(app, uds=socket_path))
    chmod_task = asyncio.create_task(_secure_socket(server, socket_path))
    try:
        await server.serve()
    finally:
        chmod_task.cancel()
        Path(socket_path).unlink(missing_ok=True)


def main() -> None:
    """Run the daemon. uvicorn traps SIGINT/SIGTERM and runs the app lifespan shutdown,
    which closes the HTTP client and the SQLite connection cleanly."""
    config = load_config()
    setup_logging(config.log_level)
    log = get_logger("daemon")
    app = build_app(config)
    if config.is_socket_mode():
        socket_path = config.effective_socket_path()
        prepare_socket(socket_path)
        log.info("Starting Cortext on unix:%s", socket_path)
        try:
            asyncio.run(_serve_uds(app, socket_path))
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Uvicorn re-raises the captured SIGINT after its shutdown completes;
            # _serve_uds's finally block has already unlinked the socket.
            pass
    else:
        log.info("Starting Cortext on %s:%d", config.host, config.port)
        uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
