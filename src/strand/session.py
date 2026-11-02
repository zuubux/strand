"""In-memory session ledger: remembers which graph facts the LLM has already been shown."""

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_MAX_SESSIONS = 1000
MAX_SESSION_ID_LEN = 128


MAX_REPLAY_CHARS = 12000  # cap on stored context blocks per session (oldest dropped first)


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """One injected context delta, kept so it can be replayed when the client drops it."""

    text: str
    entity_ids: frozenset[int]
    edge_ids: frozenset[int]


@dataclass(slots=True)
class SessionState:
    seen_entity_ids: set[int] = field(default_factory=set)
    seen_edge_ids: set[int] = field(default_factory=set)
    seen_episode_ids: set[int] = field(default_factory=set)
    last_active: float = 0.0
    # SEC-PRIV-002: in-memory opt-in to ambient hardware telemetry; dies with the session.
    allow_host_telemetry: bool = False
    # ERG-CTX-005: injected blocks, replayed when the client does not echo them back.
    context_blocks: list[ContextBlock] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionDelta:
    """Ids not yet shown in a session, in first-seen candidate order."""

    entity_ids: tuple[int, ...] = ()
    edge_ids: tuple[int, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.entity_ids or self.edge_ids)


TOOL_EVICT_MIN_CHARS = 512  # earlier-turn tool outputs longer than this are compacted
_EVICTED_MEDIA_TYPES = ("image_url", "input_audio", "input_image")


def _tool_placeholder(size: int) -> str:
    return f"[tool output evicted: {size} chars]"


def _evict_message(message: dict[str, Any]) -> dict[str, Any]:
    """Compact one earlier-turn message: bulky tool output and inline media become placeholders."""
    content = message.get("content")
    if message.get("role") == "tool":
        text = content if isinstance(content, str) else json.dumps(content, default=str)
        if len(text) > TOOL_EVICT_MIN_CHARS:
            return {**message, "content": _tool_placeholder(len(text))}
        return message
    if isinstance(content, list):
        parts = [
            {"type": "text", "text": "[media evicted]"}
            if isinstance(part, dict) and part.get("type") in _EVICTED_MEDIA_TYPES
            else part
            for part in content
        ]
        if parts != content:
            return {**message, "content": parts}
    return message


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
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        if max_sessions < 1:
            raise ValueError(f"max_sessions must be at least 1, got {max_sessions}")
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, SessionState] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._sessions

    def _touch(self, session_id: str) -> SessionState:
        """Fetch or create the session, mark it most recently used and evict stale/LRU ones."""
        if len(session_id) > MAX_SESSION_ID_LEN:
            raise ValueError(f"session_id longer than {MAX_SESSION_ID_LEN} characters")
        now = self._clock()
        self.evict_expired(now)
        state = self._sessions.pop(session_id, None)
        if state is None:
            state = SessionState()
            while len(self._sessions) >= self.max_sessions:
                # Dicts keep insertion order; re-inserting on use makes the first key the LRU one.
                del self._sessions[next(iter(self._sessions))]
        self._sessions[session_id] = state
        state.last_active = now
        return state

    def allow_host_telemetry(self, session_id: str) -> bool:
        """True when `session_id` opted in to host hardware telemetry (False if unknown)."""
        state = self._sessions.get(session_id)
        return state is not None and state.allow_host_telemetry

    def set_allow_host_telemetry(self, session_id: str, allowed: bool) -> None:
        """Toggle the session-level host telemetry override without restarting the daemon."""
        self._touch(session_id).allow_host_telemetry = bool(allowed)

    def seen_episode_ids(self, session_id: str) -> frozenset[int]:
        """Episode ids already injected into `session_id` (empty for an unknown session)."""
        state = self._sessions.get(session_id)
        return frozenset(state.seen_episode_ids) if state is not None else frozenset()

    def mark_episodes_seen(self, session_id: str, episode_ids: Iterable[int]) -> None:
        """Record episode ids as injected into `session_id`."""
        self._touch(session_id).seen_episode_ids.update(episode_ids)

    def remember_context(
        self,
        session_id: str,
        text: str,
        entity_ids: Iterable[int],
        edge_ids: Iterable[int],
    ) -> None:
        """Store an injected block (ERG-CTX-005) so later turns can replay it.

        Past `MAX_REPLAY_CHARS` the oldest blocks are dropped and their ids are un-seen, so the
        facts they carried are re-injected fresh the next time they are recalled.
        """
        state = self._touch(session_id)
        state.context_blocks.append(ContextBlock(text, frozenset(entity_ids), frozenset(edge_ids)))
        while len(state.context_blocks) > 1 and sum(len(b.text) for b in state.context_blocks) > MAX_REPLAY_CHARS:
            self._unsee(state, state.context_blocks.pop(0))

    def replay_context(self, session_id: str, present_text: str, exclude: str = "") -> list[str]:
        """Stored blocks that are not already in `present_text` (the client's system prompt).

        `exclude` is the block being injected this turn. Oldest first; unknown session -> [].
        """
        state = self._sessions.get(session_id)
        if state is None:
            return []
        return [b.text for b in state.context_blocks if b.text != exclude and b.text not in present_text]

    @staticmethod
    def _unsee(state: SessionState, block: ContextBlock) -> None:
        state.seen_entity_ids -= block.entity_ids
        state.seen_edge_ids -= block.edge_ids

    def filter_new(
        self, session_id: str, entity_ids: Iterable[int], edge_ids: Iterable[int]
    ) -> SessionDelta:
        """Return only the candidates this session has not seen, and mark them as seen."""
        state = self._touch(session_id)
        new_entities = _novel(entity_ids, state.seen_entity_ids)
        new_edges = _novel(edge_ids, state.seen_edge_ids)
        state.seen_entity_ids.update(new_entities)
        state.seen_edge_ids.update(new_edges)
        return SessionDelta(new_entities, new_edges)

    @staticmethod
    def compact_history(messages: list[Any]) -> list[Any]:
        """Evict transient payloads from turns before the latest user message.

        Raw tool outputs and inline media are runtime streams: the current turn keeps them
        intact, earlier turns keep only the assistant's syntheses plus a short placeholder.
        Returns a new list; the input is not mutated.
        """
        last_user = max(
            (i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"),
            default=-1,
        )
        return [
            _evict_message(m) if i < last_user and isinstance(m, dict) else m
            for i, m in enumerate(messages)
        ]

    def reset(self, session_id: str) -> bool:
        """Forget a session entirely. Returns True if it existed."""
        return self._sessions.pop(session_id, None) is not None

    def evict_entity(self, entity_id: int) -> int:
        """Remove `entity_id` from every session's seen set. Returns how many sessions held it."""
        count = 0
        for state in self._sessions.values():
            held = entity_id in state.seen_entity_ids
            state.seen_entity_ids.discard(entity_id)
            # The block text still names the entity: drop it and un-see its companions.
            dropped = [b for b in state.context_blocks if entity_id in b.entity_ids]
            if dropped:
                state.context_blocks = [b for b in state.context_blocks if entity_id not in b.entity_ids]
                for block in dropped:
                    self._unsee(state, block)
            if held or dropped:
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
