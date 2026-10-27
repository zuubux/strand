"""Local dense-embedding substrate (fastembed, 384-dim). The model is loaded lazily."""

import importlib.util
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .logging import get_logger

logger = get_logger("embed")

EMBEDDING_DIM = 384
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
UNAVAILABLE_MESSAGE = "fastembed unavailable; falling back to lexical-only matching."


class EmbeddingUnavailable(RuntimeError):
    """The embedding backend is missing, corrupted or cannot load its weights."""


def fastembed_available() -> bool:
    """True if `fastembed` is installed (checked without importing it)."""
    try:
        return importlib.util.find_spec("fastembed") is not None
    except (ImportError, ValueError):
        return False


def model_cache_dir(model_name: str = DEFAULT_MODEL) -> Path | None:
    """Best-effort lookup of a cached copy of `model_name` in the fastembed cache.

    The cache root is `$FASTEMBED_CACHE_PATH` or `<tmp>/fastembed_cache`; a model counts
    as cached when a directory there contains the model's short name.
    """
    root = Path(os.environ.get("FASTEMBED_CACHE_PATH") or Path(tempfile.gettempdir()) / "fastembed_cache")
    needle = model_name.rsplit("/", 1)[-1].casefold()
    try:
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and needle in entry.name.casefold():
                return entry
    except OSError:
        pass
    return None


def create_embedder(model_name: str = DEFAULT_MODEL) -> "EmbeddingManager | None":
    """A lazy `EmbeddingManager`, or None (with a warning) when fastembed is not installed."""
    if not fastembed_available():
        logger.warning(UNAVAILABLE_MESSAGE)
        return None
    return EmbeddingManager(model_name)


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into a 384-float vector (inject a fake in tests)."""

    def embed(self, text: str) -> list[float]: ...


class EmbeddingManager:
    """fastembed-backed embedder; `fastembed` is imported and the model built on first use."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self._model_factory = model_factory
        self._model: Any = None
        self._failed = False
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                if self._failed:
                    raise EmbeddingUnavailable(UNAVAILABLE_MESSAGE)
                try:
                    factory = self._model_factory
                    if factory is None:
                        from fastembed import TextEmbedding

                        factory = TextEmbedding
                    logger.info("Loading embedding model %s", self.model_name)
                    self._model = factory(self.model_name)
                except Exception as exc:
                    # Warn once; later calls fail fast without retrying the load.
                    self._failed = True
                    logger.warning(UNAVAILABLE_MESSAGE)
                    logger.debug("fastembed load error: %r", exc, exc_info=True)
                    raise EmbeddingUnavailable(UNAVAILABLE_MESSAGE) from exc
        return self._model

    def embed(self, text: str) -> list[float]:
        vector = next(iter(self._load().embed([text])))
        result = [float(x) for x in vector]
        if len(result) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have {EMBEDDING_DIM} dimensions, got {len(result)}")
        return result
