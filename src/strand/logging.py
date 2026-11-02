"""Structured rotating file logging for the `strand` logger tree.

Format: `YYYY-MM-DD HH:MM:SS [LEVEL] [component] message`, where the component is the
last part of the logger name (e.g. `strand.proxy` -> `proxy`).
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "strand"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 2
FORMAT = "%(asctime)s [%(levelname)s] [%(component)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Silent until setup_logging() is called (no stderr noise from library use or tests).
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


def default_log_path() -> Path:
    override = os.environ.get("STRAND_LOG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "strand" / "strand.log"


def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (FileNotFoundError, PermissionError):
        pass


class _SecureRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler whose log file is always 0600 (also after each rollover)."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        _chmod_quiet(Path(self.baseFilename), 0o600)
        return stream


_LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """Formatter that colors the whole line by level (used for the diagnostic stream on a TTY)."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = _LEVEL_COLORS.get(record.levelno)
        return f"{color}{text}{_RESET}" if color else text


class _ComponentFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn" or record.name.startswith("uvicorn."):
            # uvicorn.error is just uvicorn's general logger, not an error marker.
            record.component = "server"
        else:
            record.component = record.name.rsplit(".", 1)[-1]
        return True


def _remove_managed_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_strand_managed", False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(
    level: str | int = "INFO", path: Path | str | None = None, debug: bool | None = None
) -> logging.Logger:
    """Attach a rotating file handler to the `strand` logger (idempotent).

    Also streams to stderr when `debug` is true; by default that means DEBUG level on a TTY.
    """
    logger = logging.getLogger(LOGGER_NAME)
    numeric = level if isinstance(level, int) else logging.getLevelName(str(level).upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    logger.setLevel(numeric)
    logger.propagate = False
    _remove_managed_handlers(logger)

    log_path = Path(path).expanduser() if path is not None else default_log_path()
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    from . import perms  # local import: perms depends on this module (SEC-PERM-002)

    perms.secure_parent_dir(log_path.parent)
    handlers: list[logging.Handler] = [
        _SecureRotatingFileHandler(log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    ]
    if debug is None:
        debug = numeric <= logging.DEBUG and sys.stderr.isatty()
    if debug:
        handlers.append(logging.StreamHandler(sys.stderr))

    formatter = logging.Formatter(FORMAT, DATE_FORMAT)
    for handler in handlers:
        if isinstance(handler, _SecureRotatingFileHandler) or not sys.stderr.isatty():
            handler.setFormatter(formatter)
        else:
            handler.setFormatter(_ColorFormatter(FORMAT, DATE_FORMAT))
        handler.addFilter(_ComponentFilter())
        handler._strand_managed = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


def shutdown_logging() -> None:
    """Close and detach the handlers installed by setup_logging()."""
    _remove_managed_handlers(logging.getLogger(LOGGER_NAME))


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{component}")


def uvicorn_log_handler() -> logging.Handler:
    """Handler factory for uvicorn's dictConfig: the managed strand file handler (or a no-op)."""
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        if isinstance(handler, _SecureRotatingFileHandler):
            return handler
    return logging.NullHandler()
