"""Regression tests for the final kernel hardening pass (audit C1-C6, D1). Fully offline."""

import asyncio
import json
import logging

import httpx
import pytest

from strand import daemon, proxy
from strand.common import is_private, strip_think
from strand.compactor import SessionCompactor
from strand.config import Config
from strand.db import PRIVATE_ENTITY_SQL, DatabaseManager
from strand.distill import DistillationEngine
from strand.proxy import _stream_text, create_app
from strand.resolver import ContextResolver
from strand.session import SessionLedger

from .test_embed import unit
from .test_proxy import ok_json, proxy_client, run
from .test_robustness import RecordingEngine


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


@pytest.fixture
def logs():
    """Capture the `strand` logger tree directly (independent of propagation settings)."""
    logger = logging.getLogger("strand")
    handler = _Collector()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(old_level)


# ---- C6: unified privacy predicate ------------------------------------------------------

PRIVATE_CASES = [
    ("private", None),
    ("Private", None),
    ("PRIVATE", "{}"),
    (" private ", None),
    ("person", {"private": True}),
    ("person", '{"private": true}'),
    ("person", '{"private": "true"}'),
    ("person", '{"private": "TRUE"}'),
    ("person", '{"private": 1}'),
    ("person", '{"private": 1.0}'),
    ("person", '{"private": "1"}'),
    ("person", "{corrupt"),  # fails closed
    ("person", '{"private": true} trailing'),  # trailing garbage is corrupt: fails closed
    ("person", "[1, 2]"),  # valid JSON but not an object: fails closed
    ("person", "5"),
    ("person", "null"),
    ("person", '{"a": NaN}'),  # NaN is not JSON (SQLite rejects it too): fails closed
    ("person", b"\xff\xfe"),  # undecodable bytes: fails closed
    ("person", ["not", "a", "dict"]),
]
PUBLIC_CASES = [
    ("person", None),
    ("person", ""),
    ("person", "   "),
    ("person", "{}"),
    ("person", {}),
    ("person", {"aliases": ["x"]}),
    ("person", '{"private": false}'),
    ("person", '{"private": "false"}'),
    ("person", '{"private": 0}'),
    ("person", '{"private": 2}'),
    ("person", '{"private": null}'),
    ("person", '{"private": "no"}'),
    ("privately", None),
    (None, None),
]


@pytest.mark.parametrize(("archetype", "metadata"), PRIVATE_CASES)
def test_is_private_true(archetype, metadata):
    assert is_private(archetype, metadata) is True


@pytest.mark.parametrize(("archetype", "metadata"), PUBLIC_CASES)
def test_is_private_false(archetype, metadata):
    assert is_private(archetype, metadata) is False


def test_resolver_reuses_the_canonical_predicate():
    from strand import resolver

    assert resolver.is_private is is_private


def test_sql_predicate_agrees_with_python_predicate(tmp_path):
    """The recursive-CTE condition and `common.is_private` must classify every row identically."""
    cases = [(a, m) for a, m in PRIVATE_CASES + PUBLIC_CASES if a is not None and not isinstance(m, bytes)]
    rows = [(a, m if isinstance(m, (str, type(None))) else json.dumps(m)) for a, m in cases]
    rows += [("person", '{"private": "  true  "}'), ("person", '{"private": [1]}'), ("person", '{"private": {}}')]

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            async with db.transaction() as conn:
                for i, (archetype, metadata) in enumerate(rows):
                    await conn.execute(
                        "INSERT INTO entities (name, archetype, metadata) VALUES (?, ?, ?)",
                        (f"e{i}", archetype, metadata),
                    )
            async with db.conn.execute(
                f"SELECT archetype, metadata, {PRIVATE_ENTITY_SQL.format(t='en')} FROM entities AS en"
            ) as cur:
                return [tuple(r) for r in await cur.fetchall()]

    result = run(scenario())
    assert len(result) == len(rows)
    for archetype, metadata, sql_private in result:
        assert bool(sql_private) is is_private(archetype, metadata), (archetype, metadata)


def test_subgraph_hides_case_variant_and_corrupt_private_nodes(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            root = await db.upsert_entity("Root", "project", "hub")
            upper = await db.upsert_entity("Upper", "Private", "secret")
            corrupt = await db.upsert_entity("Corrupt", "person", "secret")
            truthy = await db.upsert_entity("Truthy", "person", "secret", metadata={"private": "TRUE"})
            public = await db.upsert_entity("Public", "person", "fine")
            for node in (upper, corrupt, truthy, public):
                await db.upsert_edge(root, node, "knows")
            async with db.transaction() as conn:
                await conn.execute("UPDATE entities SET metadata = '{broken' WHERE id = ?", (corrupt,))
            return await db.get_entity_subgraph(root, max_hops=2, limit=10)

    assert [r["target_name"] for r in run(scenario())] == ["Public"]



def test_resolver_and_compactor_exclude_variants(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Open", "system", "fine")
            b = await db.upsert_entity("Vault", "Private", "secret")
            c = await db.upsert_entity("Diary", "concept", "secret", metadata={"private": "true"})
            d = await db.upsert_entity("Broken", "concept", "secret")
            async with db.transaction() as conn:
                await conn.execute("UPDATE entities SET metadata = '{oops' WHERE id = ?", (d,))
            resolver = ContextResolver(db, SessionLedger())
            found = await resolver.match_entities("Open Vault Diary Broken", allow_private=False)
            compactor = SessionCompactor(db, "http://slm.test/v1", "tiny")
            return [e.name for e in found], [e["name"] for e in await compactor._entities({a, b, c, d})]

    names, compactable = run(scenario())
    assert names == ["Open"]
    assert compactable == ["Open"]


def test_distillation_never_unprivatizes_or_rewrites_private_entity(tmp_path):
    async def extractor(query, response):
        return {
            "entities": [{"name": "Vault", "archetype": "system", "summary": "now public"}],
            "edges": [{"source": "Vault", "target": "Atlas", "relation": "guards"}],
            "summary": "talked about Vault",
        }

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Vault", "private", "secret vault code", origin="model_inferred", confidence=0.3)
            await DistillationEngine(db, extractor).process_exchange(
                "s", "Tell me about the Vault and Atlas", "The Vault guards Atlas, a billing project."
            )
            async with db.conn.execute("SELECT archetype, summary FROM entities WHERE name = 'Vault'") as cur:
                return tuple(await cur.fetchone())

    assert run(scenario()) == ("private", "secret vault code")



# ---- D1: reasoning tags and list-format stream deltas -----------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<think>a</think>keep", "keep"),
        ("<thought>a</thought>keep", "keep"),
        ("<thinking>a</thinking>keep", "keep"),
        ("<reasoning>a</reasoning>keep", "keep"),
        ("<THOUGHT>a\nb</Thought>keep", "keep"),
        ("x<reasoning>a</reasoning>y<thought>b</thought>z", "xyz"),
        ("keep<thought>unclosed to the end", "keep"),
        ("<think>a<thought>b</thought>c</think>d", "d"),  # closers pair with their own opener
        ("<think>a</thought>still hidden", ""),
        ("<thoughtful>x</thoughtful>", "<thoughtful>x</thoughtful>"),
        ("1 < 2 and 3 > 2", "1 < 2 and 3 > 2"),
    ],
)
def test_strip_think_removes_all_reasoning_tags(text, expected):
    assert strip_think(text) == expected


def _sse(*contents, finish=True):
    events = [{"choices": [{"index": 0, "delta": {"content": c}}]} for c in contents]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    if finish:
        body += "data: [DONE]\n\n"
    return body.encode()


def test_stream_text_handles_list_content_parts():
    raw = _sse(
        [{"type": "text", "text": "Hel"}],
        [{"type": "text", "text": "lo "}, {"type": "image_url", "image_url": {}}, {"type": "text", "text": "world"}],
        "!",
        None,
        [],
        [7, {"type": "text"}, {"type": "text", "text": 5}],
    )
    assert _stream_text(raw) == "Hello world!"


def test_stream_text_strips_thought_blocks_across_list_chunks():
    raw = _sse([{"type": "text", "text": "<thought>secret "}], [{"type": "text", "text": "plan</thought>Answer"}])
    assert _stream_text(raw) == "Answer"



# ---- C1: private context must never be laundered into public memory ---------------------

QUERY = "Tell me about Marcus and the Vault"
REPLY = "The Vault holds the secret vault code."


def _upstream(stream: bool):
    if stream:
        chunk = {"choices": [{"index": 0, "delta": {"content": REPLY}, "finish_reason": "stop"}]}
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        return lambda _r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    return lambda _r: httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": REPLY}}]})


async def _exchange(tmp_path, headers, stream=False):
    """POST one exchange through a proxy with a recording engine; return (upstream_request, engine)."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return _upstream(stream)(request)

    engine = RecordingEngine()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with DatabaseManager(tmp_path / "m.db") as db:
        await db.upsert_entity("Marcus", "person", "Staff engineer")
        await db.upsert_entity("Vault", "private", "Secret vault code")
        app = create_app(Config(upstream_url="http://upstream.test/v1"), ContextResolver(db, SessionLedger()), upstream, engine)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://strand") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"stream": stream, "messages": [{"role": "user", "content": QUERY}]},
                headers=headers,
            )
            assert response.status_code == 200
        await asyncio.gather(*app.state.background_tasks)
        await upstream.aclose()
    return seen[0], engine


@pytest.mark.parametrize("stream", [False, True])
def test_allow_private_exchange_is_never_distilled(tmp_path, stream, logs):
    request, engine = run(_exchange(tmp_path, {"X-Strand-Allow-Private": "true"}, stream))
    assert "Secret vault code" in request.content.decode()  # private context really was injected
    assert engine.calls == []
    assert "Skipping distillation" in logs.text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("headers", [{}, {"X-Strand-Allow-Private": "false"}])
def test_default_exchange_is_still_distilled(tmp_path, stream, headers):
    request, engine = run(_exchange(tmp_path, headers, stream))
    assert "Secret vault code" not in request.content.decode()
    assert engine.calls == [("default", QUERY, REPLY)]



# ---- C2: ingress size cap and TCP exposure warning --------------------------------------


def _small_cap(monkeypatch, limit=2048):
    monkeypatch.setattr(proxy, "MAX_REQUEST_BYTES", limit)
    return limit


def _post(tmp_path, **kwargs):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
            response = await client.post("/v1/chat/completions", **kwargs)
            return response, seen

    return run(scenario())


def _body(size):
    return json.dumps({"messages": [{"role": "user", "content": "x" * size}]}).encode()


def test_oversized_body_is_rejected_with_413(tmp_path, monkeypatch):
    limit = _small_cap(monkeypatch)
    response, seen = _post(tmp_path, content=_body(limit), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert seen == []  # nothing reached the upstream


def test_chunked_body_without_content_length_is_capped(tmp_path, monkeypatch):
    limit = _small_cap(monkeypatch)
    body = _body(limit)

    async def chunks():
        for i in range(0, len(body), 512):
            yield body[i : i + 512]

    response, seen = _post(tmp_path, content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert seen == []


def test_body_just_under_the_cap_is_accepted(tmp_path, monkeypatch):
    limit = _small_cap(monkeypatch, 4096)
    body = _body(100)
    assert len(body) < limit
    response, seen = _post(tmp_path, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 200 and len(seen) == 1


def test_default_cap_is_ten_mebibytes():
    assert proxy.MAX_REQUEST_BYTES == 10 * 1024 * 1024


def test_deeply_nested_json_is_400_not_500(tmp_path):
    body = b"[" * 200_000 + b"]" * 200_000
    response, seen = _post(tmp_path, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert seen == []


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com"])
def test_exposed_bind_without_api_key_warns(host, logs):
    assert daemon.warn_if_exposed(Config(host=host, port=8088)) is True
    assert "non-loopback" in logs.text and host in logs.text and "api_key" in logs.text


@pytest.mark.parametrize(
    "config",
    [
        Config(host="127.0.0.1"),
        Config(host="localhost"),
        Config(host="::1"),
        Config(host="127.0.0.2"),
        Config(host="0.0.0.0", api_key="secret"),
    ],
)
def test_safe_bind_does_not_warn(config, logs):
    assert daemon.warn_if_exposed(config) is False
    assert logs.records == []


def test_main_warns_before_serving_tcp_exposed(monkeypatch, tmp_path, logs):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("XDG_RUNTIME_DIR", "STRAND_API_KEY", "STRAND_SOCKET_PATH", "STRAND_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRAND_HOST", "0.0.0.0")
    monkeypatch.setenv("STRAND_LOG_PATH", str(tmp_path / "strand.log"))
    served = []
    monkeypatch.setattr(daemon, "build_app", lambda config: object())
    monkeypatch.setattr(daemon.uvicorn, "run", lambda app, **kw: served.append(kw))
    try:
        daemon.main()
    finally:
        from strand.logging import shutdown_logging

        shutdown_logging()
    assert served == [{"host": "0.0.0.0", "port": 8088}]
    assert "non-loopback" in logs.text



# ---- C3: the query preview in logs is sanitized -----------------------------------------

SECRET = "sk-" + "A1b2C3d4E5" * 3


def _logged_query_lines(tmp_path, query, logs):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, _):
            await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": query}]})

    run(scenario())
    return [r.getMessage() for r in logs.records if r.getMessage().startswith("Incoming query")]


def test_query_preview_redacts_secrets(tmp_path, logs):
    (line,) = _logged_query_lines(tmp_path, f"my key is {SECRET} please remember it", logs)
    assert SECRET not in line and "sk-A1b2" not in line
    assert "[REDACTED_SECRET]" in line
    assert f"chars={len(f'my key is {SECRET} please remember it')}" in line  # length is still reported


def test_query_preview_cannot_leak_a_secret_prefix_cut_by_truncation(tmp_path, logs):
    # The secret straddles the 80-char cut: truncating first would leave a fragment too short to redact.
    query = "a" * 70 + SECRET
    (line,) = _logged_query_lines(tmp_path, query, logs)
    assert "sk-" not in line and "A1b2C3" not in line
    assert "[REDACTED" in line  # the whole secret was masked first; the 80-char cut then lands inside the marker


def test_query_preview_is_bounded(tmp_path, logs):
    (line,) = _logged_query_lines(tmp_path, "word " * 500, logs)
    assert len(line) < 200
    assert "chars=2500" in line


# ---- C4: orphan vectors are swept on connect --------------------------------------------


async def _vector_ids(db):
    async with db.conn.execute("SELECT entity_id FROM entity_embeddings ORDER BY entity_id") as cur:
        return [r[0] for r in await cur.fetchall()]


def test_orphan_vectors_are_swept_on_connect_and_live_ones_survive(tmp_path):
    import sqlite_vec

    async def scenario():
        path = tmp_path / "m.db"
        async with DatabaseManager(path) as db:
            if not db.vec_enabled:
                return None
            live = await db.upsert_entity("Live", "person", "kept", embedding=unit(0))
            dropped = await db.upsert_entity("Dropped", "person", "gone", embedding=unit(1))
            async with db.transaction() as conn:
                # Simulate a historical drop that removed the entity but left its vector behind.
                await conn.execute("DELETE FROM entities WHERE id = ?", (dropped,))
                await conn.execute(
                    "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                    (9999, sqlite_vec.serialize_float32(unit(2))),
                )
            before = await _vector_ids(db)
        async with DatabaseManager(path) as db:  # reconnect: run_migrations sweeps
            return live, dropped, before, await _vector_ids(db)

    result = run(scenario())
    assert result is not None, "sqlite-vec must be available for this test"
    live, dropped, before, after = result
    assert sorted(before) == sorted([live, dropped, 9999])
    assert after == [live]


def test_sweep_is_skipped_when_vec_is_disabled(tmp_path, monkeypatch):
    import sqlite_vec

    monkeypatch.setattr(sqlite_vec, "loadable_path", lambda: "/nonexistent/vec0")

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            return db.vec_enabled, await db.upsert_entity("Alice", "person", "ok")

    enabled, entity_id = run(scenario())  # must not try to touch the (absent) vec0 table
    assert enabled is False and entity_id == 1


def test_drop_entity_leaves_no_vector_behind(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Alice", "person", "x", embedding=unit(0))
            await db.drop_entity(a)
            return await _vector_ids(db)

    assert run(scenario()) == []

