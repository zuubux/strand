"""Local dense-embedding substrate (fastembed, 384-dim). The model is loaded lazily."""

import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from .logging import get_logger

logger = get_logger("embed")

EMBEDDING_DIM = 384
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


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
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                factory = self._model_factory
                if factory is None:
                    from fastembed import TextEmbedding

                    factory = TextEmbedding
                logger.info("Loading embedding model %s", self.model_name)
                self._model = factory(self.model_name)
        return self._model

    def embed(self, text: str) -> list[float]:
        vector = next(iter(self._load().embed([text])))
        result = [float(x) for x in vector]
        if len(result) != EMBEDDING_DIM:
            raise ValueError(f"embedding must have {EMBEDDING_DIM} dimensions, got {len(result)}")
        return result
