"""Daemon entrypoint: wires the DB, session ledger, resolver and proxy, then serves."""

import asyncio
import os
import socket
import stat
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
        db,
        extractor=extractor,
        dedup_distance_threshold=config.dedup_distance_threshold,
        ledger=resolver.ledger,
    )
    return create_app(config, resolver, distill_engine=engine)


def _socket_is_live(path: str) -> bool:
    """True if something accepts connections on the unix socket at `path`."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _remove_stale_socket(path: Path) -> None:
    """Unlink `path` only if it is a dead unix socket; never touch regular files."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"refusing to replace {path}: it exists and is not a socket")
    if _socket_is_live(str(path)):
        raise RuntimeError(f"another process is already listening on {path}")
    path.unlink(missing_ok=True)


def prepare_socket(socket_path: str) -> None:
    """Create the 0700 parent dir and remove any stale (dead) socket file."""
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        pass  # e.g. a directory we do not own such as /tmp
    _remove_stale_socket(path)


def _bind_socket(socket_path: str) -> socket.socket:
    """Bind a listening unix socket created under umask 0177 (never world-accessible)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(socket_path)
        sock.listen(2048)
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(old_umask)
    sock.setblocking(False)
    return sock


async def _secure_socket(server: uvicorn.Server, socket_path: str) -> None:
    """Belt and braces: once serving, make sure the socket is 0600."""
    while not server.started:
        if server.should_exit:
            return
        await asyncio.sleep(0.01)
    os.chmod(socket_path, 0o600)


async def _serve_uds(app, socket_path: str) -> None:
    sock = _bind_socket(socket_path)
    server = uvicorn.Server(uvicorn.Config(app, uds=socket_path))
    chmod_task = asyncio.create_task(_secure_socket(server, socket_path))
    try:
        await server.serve(sockets=[sock])
    finally:
        chmod_task.cancel()
        sock.close()
        try:
            if stat.S_ISSOCK(os.stat(socket_path).st_mode):
                Path(socket_path).unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def main() -> None:
    """Run the daemon. uvicorn traps SIGINT/SIGTERM and runs the app lifespan shutdown,
    which closes the HTTP client and the SQLite connection cleanly."""
    try:
        config = load_config(strict=True)
    except ValueError as exc:
        raise SystemExit(f"cortext: configuration error: {exc}") from exc
    setup_logging(config.log_level)
    log = get_logger("daemon")
    app = build_app(config)
    if config.is_socket_mode():
        socket_path = config.effective_socket_path()
        try:
            prepare_socket(socket_path)
        except RuntimeError as exc:
            raise SystemExit(f"cortext: {exc}") from exc
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
