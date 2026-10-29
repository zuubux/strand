"""Daemon entrypoint: wires the DB, session ledger, resolver and proxy, then serves."""

import argparse
import asyncio
import contextlib
import copy
import os
import socket
import stat
import sys
from collections.abc import Sequence
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
        persona_prompt=config.persona_prompt,
        continuity_horizon_days=config.continuity_horizon_days,
        continuity_max_episodes=config.continuity_max_episodes,
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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback_host(host: str) -> bool:
    host = host.strip().lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def warn_if_exposed(config: Config, log=None) -> bool:
    """Warn loudly when TCP mode binds beyond loopback without an inbound `api_key` (C2).

    Returns True when the warning was emitted.
    """
    if config.api_key or _is_loopback_host(config.host):
        return False
    (log or get_logger("daemon")).warning(
        "SECURITY: binding to non-loopback host %s:%d with no api_key configured; anyone who can "
        "reach this port can read your memory graph and use your upstream credentials. "
        "Set STRAND_API_KEY or bind to 127.0.0.1.",
        config.host,
        config.port,
    )
    return True


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m strand.daemon", description="Run the Strand daemon.")
    parser.add_argument(
        "--diag",
        "--debug",
        dest="diag",
        action="store_true",
        help="diagnostic mode: verbose colored logs on stderr/stdout (default: quiet, log file only)",
    )
    return parser.parse_args(argv)


@contextlib.contextmanager
def _quiet_uvicorn_logging():
    """Route uvicorn's own logs into the strand log file instead of the terminal."""
    original = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    handler = {"()": "strand.logging.uvicorn_log_handler"}
    quiet = copy.deepcopy(original)
    quiet["formatters"] = {}
    quiet["handlers"] = {"default": handler, "access": handler}
    uvicorn.config.LOGGING_CONFIG.clear()
    uvicorn.config.LOGGING_CONFIG.update(quiet)
    try:
        yield
    finally:
        uvicorn.config.LOGGING_CONFIG.clear()
        uvicorn.config.LOGGING_CONFIG.update(original)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the daemon. uvicorn traps SIGINT/SIGTERM and runs the app lifespan shutdown,
    which closes the HTTP client and the SQLite connection cleanly.

    Quiet by default (logs go to the strand log file only); `--diag`/`--debug` streams
    verbose colored logs to the terminal as well."""
    args = _parse_args([] if argv is None else argv)
    if args.diag:
        _run(args)
    else:
        with _quiet_uvicorn_logging():
            _run(args)


def _run(args: argparse.Namespace) -> None:
    try:
        config = load_config(strict=True)
    except ValueError as exc:
        raise SystemExit(f"strand: configuration error: {exc}") from exc
    if args.diag:
        setup_logging("DEBUG", debug=True)
    else:
        setup_logging(config.log_level, debug=False)
    log = get_logger("daemon")
    app = build_app(config)
    if config.is_socket_mode():
        socket_path = config.effective_socket_path()
        try:
            prepare_socket(socket_path)
        except RuntimeError as exc:
            raise SystemExit(f"strand: {exc}") from exc
        log.info("Starting Strand on unix:%s", socket_path)
        try:
            asyncio.run(_serve_uds(app, socket_path))
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Uvicorn re-raises the captured SIGINT after its shutdown completes;
            # _serve_uds's finally block has already unlinked the socket.
            pass
    else:
        warn_if_exposed(config, log)
        log.info("Starting Strand on %s:%d", config.host, config.port)
        uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main(sys.argv[1:])
