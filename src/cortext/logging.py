"""Structured rotating file logging for the `cortext` logger tree.

Format: `YYYY-MM-DD HH:MM:SS [LEVEL] [component] message`, where the component is the
last part of the logger name (e.g. `cortext.proxy` -> `proxy`).
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "cortext"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 2
FORMAT = "%(asctime)s [%(levelname)s] [%(component)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Silent until setup_logging() is called (no stderr noise from library use or tests).
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


def default_log_path() -> Path:
    override = os.environ.get("CORTEXT_LOG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "cortext" / "cortext.log"


class _ComponentFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.component = record.name.rsplit(".", 1)[-1]
        return True


def _remove_managed_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_cortext_managed", False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(
    level: str | int = "INFO", path: Path | str | None = None, debug: bool | None = None
) -> logging.Logger:
    """Attach a rotating file handler to the `cortext` logger (idempotent).

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
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    ]
    if debug is None:
        debug = numeric <= logging.DEBUG and sys.stderr.isatty()
    if debug:
        handlers.append(logging.StreamHandler(sys.stderr))

    formatter = logging.Formatter(FORMAT, DATE_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_ComponentFilter())
        handler._cortext_managed = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


def shutdown_logging() -> None:
    """Close and detach the handlers installed by setup_logging()."""
    _remove_managed_handlers(logging.getLogger(LOGGER_NAME))


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{component}")
