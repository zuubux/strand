import asyncio
import json
from contextlib import asynccontextmanager

import httpx

from cortext.config import Config
from cortext.db import DatabaseManager
from cortext.proxy import create_app
from cortext.resolver import ContextResolver
from cortext.session import SessionLedger

SSE_BODY = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'


def run(coro):
    return asyncio.run(coro)


@asynccontextmanager
async def proxy_client(db_path, upstream_handler):
    """Yield (client, seen_upstream_requests); ASGITransport skips lifespan, so manage the DB here."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return upstream_handler(request)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with DatabaseManager(db_path) as db:
        await db.upsert_entity("Marcus", "person", "Staff engineer")
        resolver = ContextResolver(db, SessionLedger())
        config = Config(upstream_url="http://upstream.test/v1", upstream_api_key="sk-test")
        app = create_app(config, resolver, upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://cortext") as client:
            try:
                yield client, seen
            finally:
                await upstream.aclose()


def ok_json(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_health_returns_ok(tmp_path):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, _):
            return await client.get("/health")

    response = run(scenario())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_injects_context_as_new_system_message_and_forwards_auth(tmp_path):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": "Prep my 1-on-1 with Marcus"}]},
            )
            return response, seen

    response, seen = run(scenario())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    (request,) = seen
    assert str(request.url) == "http://upstream.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    messages = body_of(request)["messages"]
    assert messages[0]["role"] == "system"
    assert "Marcus (person): Staff engineer" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Prep my 1-on-1 with Marcus"}


def test_prepends_to_existing_system_message_and_strips_session_id(tmp_path):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
            await client.post(
                "/v1/chat/completions",
                json={
                    "session_id": "abc",
                    "messages": [
                        {"role": "system", "content": "Be terse."},
                        {"role": "user", "content": "Tell me about Marcus"},
                    ],
                },
            )
            return seen

    (request,) = run(scenario())
    payload = body_of(request)
    assert "session_id" not in payload
    system = payload["messages"][0]["content"]
    assert system.startswith("## Cortext Memory")
    assert system.endswith("Be terse.")
    assert len(payload["messages"]) == 2


def test_second_turn_does_not_reinject_and_header_sessions_are_isolated(tmp_path):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
            body = {"messages": [{"role": "user", "content": "Marcus again"}]}
            await client.post("/v1/chat/completions", json=body, headers={"X-Session-ID": "a"})
            await client.post("/v1/chat/completions", json=body, headers={"X-Session-ID": "a"})
            await client.post("/v1/chat/completions", json=body, headers={"X-Session-ID": "b"})
            return [body_of(r)["messages"] for r in seen]

    turn1, turn2, other = run(scenario())
    assert turn1[0]["role"] == "system"
    assert turn2 == [{"role": "user", "content": "Marcus again"}]
    assert other[0]["role"] == "system"


def test_streaming_sse_passes_through(tmp_path):
    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(SSE_BODY), headers={"content-type": "text/event-stream"})

    async def scenario():
        async with proxy_client(tmp_path / "m.db", upstream) as (client, seen):
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"stream": True, "messages": [{"role": "user", "content": "Marcus"}]},
            ) as response:
                raw = b"".join([chunk async for chunk in response.aiter_bytes()])
                return response, raw, seen

    response, raw, seen = run(scenario())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert raw == SSE_BODY
    assert body_of(seen[0])["stream"] is True


def test_upstream_error_is_passed_through_and_does_not_taint_session(tmp_path):
    state = {"fail": True}

    def upstream(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return ok_json(request)

    async def scenario():
        async with proxy_client(tmp_path / "m.db", upstream) as (client, seen):
            body = {"messages": [{"role": "user", "content": "Marcus"}]}
            failed = await client.post("/v1/chat/completions", json=body)
            state["fail"] = False
            retry = await client.post("/v1/chat/completions", json=body)
            return failed, retry, seen

    failed, retry, seen = run(scenario())
    assert failed.status_code == 429
    assert failed.json() == {"error": {"message": "rate limited"}}
    assert retry.status_code == 200
    # The failed turn never reached the model, so the retry must replay the context.
    assert body_of(seen[1])["messages"][0]["role"] == "system"


def test_upstream_connection_failure_returns_502(tmp_path):
    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async def scenario():
        async with proxy_client(tmp_path / "m.db", upstream) as (client, _):
            return await client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": "Marcus"}]}
            )

    response = run(scenario())
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


def test_invalid_requests_return_400(tmp_path):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
            bad_json = await client.post("/v1/chat/completions", content=b"{nope")
            no_messages = await client.post("/v1/chat/completions", json={"model": "m"})
            return bad_json, no_messages, seen

    bad_json, no_messages, seen = run(scenario())
    assert bad_json.status_code == 400 and no_messages.status_code == 400
    assert seen == []


def test_config_from_env_defaults_and_overrides():
    defaults = Config.from_env({})
    assert (defaults.host, defaults.port) == ("127.0.0.1", 8088)
    assert defaults.upstream_url == "https://api.openai.com/v1"
    assert defaults.db_path == "~/.config/cortext/memory.db"

    custom = Config.from_env(
        {
            "CORTEXT_UPSTREAM_URL": "http://x/v1",
            "CORTEXT_UPSTREAM_API_KEY": "k",
            "CORTEXT_PORT": "9000",
            "CORTEXT_HOST": "0.0.0.0",
            "CORTEXT_DB_PATH": "/tmp/db",
        }
    )
    assert custom == Config("http://x/v1", "k", "0.0.0.0", 9000, "/tmp/db")
