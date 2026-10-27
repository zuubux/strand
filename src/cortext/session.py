"""In-memory session ledger: remembers which graph facts the LLM has already been shown."""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

DEFAULT_TTL_SECONDS = 3600.0


@dataclass(slots=True)
class SessionState:
    seen_entity_ids: set[int] = field(default_factory=set)
    seen_edge_ids: set[int] = field(default_factory=set)
    last_active: float = 0.0


@dataclass(frozen=True, slots=True)
class SessionDelta:
    """Ids not yet shown in a session, in first-seen candidate order."""

    entity_ids: tuple[int, ...] = ()
    edge_ids: tuple[int, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.entity_ids or self.edge_ids)


def _novel(candidates: Iterable[int], seen: set[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(i for i in candidates if i not in seen))


class SessionLedger:
    """Tracks per-session working memory so only novel context is injected each turn.

    Entity ids and edge ids are separate id spaces and are tracked independently.
    A session expires once it has been idle for `ttl_seconds` (every `filter_new`
    call counts as activity). Methods never await, so this is safe on a single
    asyncio event loop; it is not designed for multi-threaded use.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, SessionState] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._sessions

    def filter_new(
        self, session_id: str, entity_ids: Iterable[int], edge_ids: Iterable[int]
    ) -> SessionDelta:
        """Return only the candidates this session has not seen, and mark them as seen."""
        now = self._clock()
        self.evict_expired(now)
        state = self._sessions.get(session_id)
        if state is None:
            state = self._sessions[session_id] = SessionState()
        state.last_active = now

        new_entities = _novel(entity_ids, state.seen_entity_ids)
        new_edges = _novel(edge_ids, state.seen_edge_ids)
        state.seen_entity_ids.update(new_entities)
        state.seen_edge_ids.update(new_edges)
        return SessionDelta(new_entities, new_edges)

    def reset(self, session_id: str) -> bool:
        """Forget a session entirely. Returns True if it existed."""
        return self._sessions.pop(session_id, None) is not None

    def evict_entity(self, entity_id: int) -> int:
        """Remove `entity_id` from every session's seen set. Returns how many sessions held it."""
        count = 0
        for state in self._sessions.values():
            if entity_id in state.seen_entity_ids:
                state.seen_entity_ids.discard(entity_id)
                count += 1
        return count

    def evict_expired(self, now: float | None = None) -> int:
        """Drop sessions idle for at least `ttl_seconds`. Returns how many were evicted."""
        now = self._clock() if now is None else now
        stale = [
            sid for sid, state in self._sessions.items() if now - state.last_active >= self.ttl_seconds
        ]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)
