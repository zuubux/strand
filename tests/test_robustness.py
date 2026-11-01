"""Regression tests for Phase 3 (ROB-1..ROB-5): protocol robustness and lifecycle hardening."""

import asyncio
import json
import math
import os
import socket
import stat
from datetime import UTC, datetime

import httpx
import pytest

from strand import daemon
from strand.common import strip_think
from strand.compactor import SessionCompactor
from strand.config import Config, save_config_value
from strand.db import DatabaseManager
from strand.proxy import _inject_delta, _stream_text, create_app
from strand.resolver import ContextResolver, effective_priority
from strand.session import SessionLedger


def run(coro):
    return asyncio.run(coro)


def sse(*events: object, done: bool = True) -> bytes:
    out = b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)
    return out + (b"data: [DONE]\n\n" if done else b"")


def chunk(text: str, index: int = 0, finish: str | None = None) -> dict:
    return {"choices": [{"index": index, "delta": {"content": text}, "finish_reason": finish}]}


# ---------------------------------------------------------------- ROB-1


def test_prepare_socket_refuses_to_unlink_regular_file(tmp_path):
    victim = tmp_path / "run" / "memory.db"
    victim.parent.mkdir()
    victim.write_text("precious")
    with pytest.raises(RuntimeError, match="not a socket"):
        daemon.prepare_socket(str(victim))
    assert victim.read_text() == "precious"


def test_prepare_socket_refuses_live_socket(tmp_path):
    path = str(tmp_path / "live.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    try:
        with pytest.raises(RuntimeError, match="already listening"):
            daemon.prepare_socket(path)
        assert os.path.exists(path)
    finally:
        server.close()


def test_bind_socket_is_created_0600_and_umask_restored(tmp_path):
    path = str(tmp_path / "s.sock")
    before = os.umask(0o022)
    try:
        sock = daemon._bind_socket(path)
        try:
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        finally:
            sock.close()
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


def test_prepare_socket_tolerates_chmod_permission_error(tmp_path, monkeypatch):
    def deny(*_a, **_k):
        raise PermissionError

    monkeypatch.setattr(daemon.os, "chmod", deny)
    daemon.prepare_socket(str(tmp_path / "x" / "c.sock"))


def test_save_config_value_creates_0700_parent(tmp_path):
    path = tmp_path / ".config" / "strand" / "config.json"
    save_config_value("log_level", "debug", path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.parent.chmod(0o755)
    save_config_value("log_level", "info", path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


# ---------------------------------------------------------------- ROB-2 / ROB-3 (proxy)


class RecordingEngine:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    async def process_exchange(self, session_id, query, response):
        self.calls.append((session_id, query, response))


class ExplodingStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes, exc: BaseException):
        self.first, self.exc, self.closed = first, exc, False

    async def __aiter__(self):
        yield self.first
        raise self.exc

    async def aclose(self):
        self.closed = True


async def drive(tmp_path, stream, session="s1"):
    """POST one streaming turn; return (raw_body_or_exc, engine, ledger)."""
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        )
    )
    engine = RecordingEngine()
    async with DatabaseManager(tmp_path / "m.db") as db:
        await db.upsert_entity("Marcus", "person", "Staff engineer")
        ledger = SessionLedger()
        app = create_app(
            Config(upstream_url="http://upstream.test/v1"), ContextResolver(db, ledger), upstream, engine
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://strand") as client:
            try:
                result = await client.post(
                    "/v1/chat/completions",
                    json={
                        "stream": True,
                        "session_id": session,
                        "messages": [{"role": "user", "content": "Tell me about Marcus"}],
                    },
                )
                result = result.content
            except BaseException as exc:  # noqa: BLE001 - cancellation surfaces through the ASGI transport
                result = exc
        for task in list(app.state.background_tasks):
            await task
        await upstream.aclose()
    return result, engine, ledger


def test_clean_stream_still_distills(tmp_path):
    body = sse(chunk("Hello"), chunk(" there", finish="stop"))
    raw, engine, ledger = run(drive(tmp_path, httpx.ByteStream(body)))
    assert raw == body
    assert engine.calls == [("s1", "Tell me about Marcus", "Hello there")]
    assert "s1" in ledger


def test_cancellation_mid_stream_resets_ledger_and_closes_upstream(tmp_path):
    stream = ExplodingStream(sse(chunk("par"), done=False), asyncio.CancelledError())
    result, engine, ledger = run(drive(tmp_path, stream))
    assert isinstance(result, BaseException)  # the transport surfaces the abort, not a body
    assert stream.closed
    assert engine.calls == []
    assert "s1" not in ledger


def test_in_stream_error_event_skips_distillation_and_resets_ledger(tmp_path):
    body = sse(chunk("partial"), {"error": {"message": "overloaded", "type": "server_error"}})
    raw, engine, ledger = run(drive(tmp_path, httpx.ByteStream(body)))
    assert raw == body
    assert engine.calls == []
    assert "s1" not in ledger


def test_stream_without_finish_or_done_is_not_distilled(tmp_path):
    body = sse(chunk("truncated"), done=False)
    _, engine, ledger = run(drive(tmp_path, httpx.ByteStream(body)))
    assert engine.calls == []
    assert "s1" not in ledger


def test_finish_reason_without_done_counts_as_complete(tmp_path):
    body = sse(chunk("All good", finish="stop"), done=False)
    _, engine, _ = run(drive(tmp_path, httpx.ByteStream(body)))
    assert [c[2] for c in engine.calls] == ["All good"]


def test_mid_stream_http_error_emits_sse_error_event(tmp_path):
    stream = ExplodingStream(sse(chunk("par"), done=False), httpx.ReadError("boom"))
    raw, engine, ledger = run(drive(tmp_path, stream))
    assert isinstance(raw, bytes)
    events = [line for line in raw.decode().split("\n") if line.startswith("data:")]
    assert json.loads(events[-1][5:])["error"]["type"] == "upstream_error"
    assert engine.calls == []
    assert "s1" not in ledger
    assert stream.closed


def test_stream_text_ignores_non_zero_choice_indexes():
    raw = sse(
        {"choices": [{"index": 0, "delta": {"content": "A"}}, {"index": 1, "delta": {"content": "X"}}]},
        chunk("Y", index=1),
        chunk("B"),
    )
    assert _stream_text(raw) == "AB"


def test_stream_text_strips_think_blocks():
    raw = sse(chunk("<think>I am sure Bob owns"), chunk(" a jet</think>Marcus uses Linux."))
    assert _stream_text(raw) == "Marcus uses Linux."


def test_strip_think_is_case_insensitive_multiline_and_handles_unclosed():
    assert strip_think("a<THINK>x\ny\n</Think>b") == "ab"
    assert strip_think("keep<think>never closed") == "keep"
    assert strip_think("plain") == "plain"


def test_inject_delta_targets_developer_role():
    out = _inject_delta([{"role": "developer", "content": "be terse"}, {"role": "user", "content": "hi"}], "CTX")
    assert out[0] == {"role": "developer", "content": "CTX\n\nbe terse"}
    assert len(out) == 2


def test_inject_delta_inserts_system_message_when_none_exists():
    out = _inject_delta([{"role": "user", "content": "hi"}], "CTX")
    assert out[0] == {"role": "system", "content": "CTX"}


def test_distillation_semaphore_bounds_concurrency(tmp_path):
    active = peak = 0

    class Slow:
        async def process_exchange(self, *_a):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            app = create_app(
                Config(upstream_url="http://u.test/v1"),
                ContextResolver(db, SessionLedger()),
                httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _r: httpx.Response(
                            200, json={"choices": [{"message": {"content": "a long enough answer"}}]}
                        )
                    )
                ),
                Slow(),
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://c") as client:
                await asyncio.gather(
                    *(
                        client.post(
                            "/v1/chat/completions", json={"messages": [{"role": "user", "content": f"q{i}"}]}
                        )
                        for i in range(12)
                    )
                )
            await asyncio.gather(*app.state.background_tasks)

    run(scenario())
    assert 1 < peak <= 4


def test_overlong_session_id_is_rejected(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            app = create_app(Config(upstream_url="http://u.test/v1"), ContextResolver(db, SessionLedger()))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://c") as client:
                return await client.post(
                    "/v1/chat/completions",
                    json={"session_id": "x" * 129, "messages": [{"role": "user", "content": "hi"}]},
                )

    assert run(scenario()).status_code == 400


# ---------------------------------------------------------------- ROB-4


def test_private_nodes_do_not_consume_subgraph_limit(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            root = await db.upsert_entity("Root", "project", "hub")
            for i in range(3):
                sid = await db.upsert_entity(f"Secret{i}", "private", "hidden")
                await db.upsert_edge(root, sid, "knows", weight=1.0, confidence=1.0)
            flagged = await db.upsert_entity("Flagged", "person", "hidden", metadata={"private": True})
            await db.upsert_edge(root, flagged, "knows", weight=1.0, confidence=1.0)
            public = await db.upsert_entity("Public", "person", "visible")
            await db.upsert_edge(root, public, "knows", weight=0.1, confidence=0.5)
            return await db.get_entity_subgraph(root, max_hops=2, limit=1)

    rows = run(scenario())
    assert [r["target_name"] for r in rows] == ["Public"]


def test_compactor_prompt_excludes_private_entities(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Open", "system", "fine")
            b = await db.upsert_entity("Vault", "private", "secret")
            c = await db.upsert_entity("Diary", "concept", "secret", metadata={"private": True})
            compactor = SessionCompactor(db, "http://slm.test/v1", "tiny")
            return await compactor._entities({a, b, c})

    assert [e["name"] for e in run(scenario())] == ["Open"]


# ---------------------------------------------------------------- ROB-5


def test_session_ledger_lru_eviction_at_max_sessions():
    ledger = SessionLedger(max_sessions=2)
    ledger.filter_new("a", [1], [])
    ledger.filter_new("b", [1], [])
    ledger.filter_new("a", [2], [])  # touch a: b becomes least recently used
    ledger.filter_new("c", [1], [])
    assert len(ledger) == 2
    assert "b" not in ledger
    assert "a" in ledger and "c" in ledger


def test_session_ledger_rejects_bad_limits():
    with pytest.raises(ValueError):
        SessionLedger(max_sessions=0)
    with pytest.raises(ValueError):
        SessionLedger().filter_new("x" * 129, [1], [])
    SessionLedger().filter_new("x" * 128, [1], [])


@pytest.mark.parametrize("half_life", [0.0, -5.0, math.inf, math.nan])
def test_effective_priority_with_degenerate_half_life_is_undecayed(half_life):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    value = effective_priority(
        0.5,
        0.8,
        3,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        now=now,
        decay_enabled=True,
        half_life_days=half_life,
    )
    assert value == pytest.approx(0.4)
