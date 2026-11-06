import asyncio
import json
from contextlib import asynccontextmanager

import httpx

from strand.config import Config
from strand.db import DatabaseManager
from strand.proxy import create_app
from strand.resolver import ContextResolver
from strand.session import SessionLedger

SSE_BODY = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'


def run(coro):
    return asyncio.run(coro)


@asynccontextmanager
async def proxy_client(db_path, upstream_handler, **config_kwargs):
    """Yield (client, seen_upstream_requests); ASGITransport skips lifespan, so manage the DB here."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return upstream_handler(request)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with DatabaseManager(db_path) as db:
        await db.upsert_entity("Marcus", "person", "Staff engineer")
        resolver = ContextResolver(db, SessionLedger())
        config_kwargs.setdefault("upstream_url", "http://upstream.test/v1")
        config = Config(upstream_api_key="sk-test", **config_kwargs)
        app = create_app(config, resolver, upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://strand") as client:
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
    assert system.startswith("## Strand Memory")
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
    # ERG-CTX-005: the client dropped our system prompt, so the same block is replayed (not re-resolved).
    assert turn2[0] == turn1[0] and turn2[1:] == [{"role": "user", "content": "Marcus again"}]
    assert other[0]["role"] == "system"


def test_three_turn_user_assistant_history_keeps_working_memory(tmp_path):
    """ERG-CTX-005: clients resend only user/assistant turns; memory must stay in the system prompt."""

    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
            history = [{"role": "user", "content": "Tell me about Marcus"}]
            for reply, follow_up in (("Staff engineer.", "And Marcus again?"), ("Still him.", "One more on Marcus")):
                await client.post("/v1/chat/completions", json={"messages": history})
                history = [*history, {"role": "assistant", "content": reply}, {"role": "user", "content": follow_up}]
            await client.post("/v1/chat/completions", json={"messages": history})
            return [body_of(r)["messages"] for r in seen]

    turn1, turn2, turn3 = run(scenario())
    for messages in (turn1, turn2, turn3):
        assert messages[0]["role"] == "system"
        assert "Marcus" in messages[0]["content"] and "Staff engineer" in messages[0]["content"]
        assert [m["role"] for m in messages[1:] if m["role"] == "system"] == []
    assert turn3[0]["content"].count("## Strand Memory") == 1
    assert [m["role"] for m in turn3[1:]] == ["user", "assistant", "user", "assistant", "user"]


def test_client_preserved_system_prompt_is_not_duplicated(tmp_path):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
            user = {"role": "user", "content": "Marcus again"}
            await client.post("/v1/chat/completions", json={"messages": [user]})
            echoed = body_of(seen[0])["messages"]
            await client.post("/v1/chat/completions", json={"messages": [*echoed, user]})
            return body_of(seen[1])["messages"], echoed

    turn2, echoed = run(scenario())
    assert turn2 == [*echoed, {"role": "user", "content": "Marcus again"}]


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
            bad_json = await client.post(
                "/v1/chat/completions", content=b"{nope", headers={"Content-Type": "application/json"}
            )
            no_messages = await client.post("/v1/chat/completions", json={"model": "m"})
            return bad_json, no_messages, seen

    bad_json, no_messages, seen = run(scenario())
    assert bad_json.status_code == 400 and no_messages.status_code == 400
    assert seen == []


def test_config_from_env_defaults_and_overrides():
    defaults = Config.from_env({})
    assert (defaults.host, defaults.port) == ("127.0.0.1", 8088)
    assert defaults.upstream_url == "https://api.openai.com/v1"
    assert defaults.db_path == "~/.config/strand/memory.db"

    custom = Config.from_env(
        {
            "STRAND_UPSTREAM_URL": "http://x/v1",
            "STRAND_UPSTREAM_API_KEY": "k",
            "STRAND_PORT": "9000",
            "STRAND_HOST": "0.0.0.0",
            "STRAND_DB_PATH": "/tmp/db",
        }
    )
    assert custom == Config("http://x/v1", "k", "0.0.0.0", 9000, "/tmp/db")


def test_delta_log_fields_count_personality_and_working_memory():
    from strand.proxy import _delta_log_fields

    delta = (
        "## Strand Memory\n[Persona Stance]\nAnchor prompt\n- Dry Wit: calm\n"
        "[Operational Directives]\n- a: x\n- b: y\n[Interaction Preferences]\n- Nm: z\n"
        "[Working Memory]\nEntities:\n- Acme (vehicle): W34\nRelations:\n- User -drives-> Acme\n- A -b-> C"
    )
    assert _delta_log_fields(delta) == "entities=1 edges=2 traits=2 directives=2 preferences=1"
    plain = "## Strand Memory\nEntities:\n- A (person): x\n- B (person): y\nRelations:\n- A -knows-> B"
    assert _delta_log_fields(plain) == "entities=2 edges=1"
    assert _delta_log_fields("") == "entities=0 edges=0"


HOST = {"user": "alice", "hostname": "workstation-01", "os": "Fedora Linux 44"}


def _systems(seen):
    return [body_of(r)["messages"][0]["content"] for r in seen]


async def _post_chat(client, text, headers=None, history=()):
    messages = [*history, {"role": "user", "content": text}]
    return await client.post("/v1/chat/completions", json={"messages": messages, "session_id": "s"}, headers=headers or {})


def test_fresh_session_has_no_onboarding_directive(tmp_path):
    async def scenario():
        seen: list[httpx.Request] = []
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (seen.append(r), ok_json(r))[1]))
        async with DatabaseManager(tmp_path / "m.db") as db:
            app = create_app(Config(upstream_url="http://upstream.test/v1"), ContextResolver(db, SessionLedger()), upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://strand") as client:
                await _post_chat(client, "hello there", {"X-Strand-Mode": "onboarding"})
                await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
        return _systems(seen)

    (system,) = run(scenario())
    for text in ("Onboarding Directive", "What should I call you?", "TASK-YIELD", "Intent Triage"):
        assert text not in system


def _model_b_operator(request):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "entities": [
                                    {"name": "Alice", "archetype": "person", "origin": "user_explicit", "confidence": 1.0}
                                ],
                                "edges": [{"source": "Alice", "target": "workstation-01", "relation": "operates_host"}],
                            }
                        )
                    }
                }
            ]
        },
    )


def test_model_b_operator_folds_generic_user(tmp_path, monkeypatch):
    import strand.extractors as ex
    from strand.distill import DistillationEngine
    from strand.extractors import LLMExtractor
    from strand.onboarding import count_operators

    monkeypatch.setattr(ex, "get_host_context", lambda: dict(HOST))

    async def scenario():
        def greeting(_: httpx.Request) -> httpx.Response:
            text = "Nice to meet you, Alice. I run locally on workstation-01 as your sovereign working memory."
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(greeting))
        slm = httpx.AsyncClient(transport=httpx.MockTransport(_model_b_operator))
        async with DatabaseManager(tmp_path / "b.db") as db:
            generic = await db.upsert_entity("User", "person", "generic", origin="model_inferred", confidence=0.6)
            rule = await db.upsert_entity("No apologies", "directive", "never apologize")
            await db.upsert_edge(generic, rule, "demands")
            engine = DistillationEngine(db, extractor=LLMExtractor("http://slm.test/v1", "tiny", client=slm))
            app = create_app(
                Config(upstream_url="http://upstream.test/v1"), ContextResolver(db, SessionLedger()), upstream, engine
            )
            history = [
                {"role": "user", "content": "hello there"},
                {"role": "assistant", "content": "Hello."},
                {"role": "user", "content": "Call me Alice"},
            ]
            before = await count_operators(db)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://strand") as client:
                await client.post("/v1/chat/completions", json={"messages": history, "session_id": "s"})
                await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
            await slm.aclose()
            async with db.conn.execute("SELECT name FROM entities WHERE archetype = 'person'") as cur:
                people = [r[0] for r in await cur.fetchall()]
            async with db.conn.execute(
                "SELECT s.name, t.name, e.relation FROM edges e JOIN entities s ON s.id = e.source_id "
                "JOIN entities t ON t.id = e.target_id"
            ) as cur:
                edges = {tuple(r) for r in await cur.fetchall()}
            return before, await count_operators(db), people, edges

    before, after, people, edges = run(scenario())
    assert before == 0 and after > 0
    assert people == ["Alice"]
    assert ("Alice", "workstation-01", "operates_host") in edges
    assert ("Alice", "No apologies", "demands") in edges


def test_proxy_has_no_procedural_name_interceptors():
    import strand.proxy as proxy_mod

    for attr in ("declared_name", "EXPECTING_NAME", "anchor_operator_name"):
        assert not hasattr(proxy_mod, attr), attr


FULL_HOST = {
    "user": "alice",
    "hostname": "workstation-01",
    "os": "Fedora Linux 44",
    "desktop": "KDE",
    "session": "wayland",
    "cpu": "Test CPU 9000X",
    "ram_gb": 32.0,
}
SPEC_TOKENS = ("Fedora", "Test CPU 9000X", "32 GB", "KDE", "wayland")


def test_session_ledger_host_telemetry_override_is_per_session_and_reset():
    ledger = SessionLedger()
    assert ledger.allow_host_telemetry("a") is False
    ledger.set_allow_host_telemetry("a", True)
    assert ledger.allow_host_telemetry("a") is True
    assert ledger.allow_host_telemetry("b") is False
    ledger.set_allow_host_telemetry("a", False)
    assert ledger.allow_host_telemetry("a") is False
    ledger.set_allow_host_telemetry("a", True)
    ledger.reset("a")
    assert ledger.allow_host_telemetry("a") is False


def test_upstream_failure_masks_url_credentials_in_response_and_log(tmp_path, caplog):
    import logging

    from strand.logging import LOGGER_NAME

    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach http://user:hunter2@up.test/v1 ({request.url})", request=request)

    async def scenario():
        async with proxy_client(tmp_path / "m.db", upstream) as (client, _):
            return await client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": "Marcus"}]}
            )

    parent = logging.getLogger(LOGGER_NAME)
    parent.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            response = run(scenario())
    finally:
        parent.removeHandler(caplog.handler)
    assert response.status_code == 502
    assert "hunter2" not in response.text and "hunter2" not in caplog.text
    assert "user:********@up.test" in response.json()["error"]["message"]


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def _post_with_search(tmp_path, tools_enabled=True, body_extra=None, **config_kwargs):
    async def scenario():
        async with proxy_client(tmp_path / "m.db", ok_json, tools_enabled=tools_enabled, **config_kwargs) as (client, seen):
            body = {"messages": [{"role": "user", "content": "hello"}], **(body_extra or {})}
            await client.post("/v1/chat/completions", json=body)
            return seen

    (request,) = run(scenario())
    return body_of(request)


GS = {"google_search": {}}


def test_web_search_adds_top_level_google_search_for_gemini(tmp_path):
    payload = _post_with_search(tmp_path, upstream_url=GEMINI_URL, web_search_enabled=True)
    assert payload["tools"] == [GS]  # no function tools: Gemini rejects the mix
    assert "extra_body" not in payload  # Gemini rejects a literal extra_body field


def test_web_search_disabled_on_gemini_keeps_workspace_tools(tmp_path):
    payload = _post_with_search(tmp_path, upstream_url=GEMINI_URL, web_search_enabled=False)
    assert "extra_body" not in payload
    names = {t["function"]["name"] for t in payload["tools"]}
    assert {"list_directory", "read_file"} <= names


def test_web_search_keeps_client_tools_clean_and_no_duplicates(tmp_path):
    client_tool = {"type": "function", "function": {"name": "mine", "parameters": {}}}
    payload = _post_with_search(
        tmp_path,
        tools_enabled=False,
        body_extra={"tools": [client_tool, {"type": "google_search"}], "extra_body": {"tools": [GS], "other": 1}},
        upstream_url=GEMINI_URL,
        web_search_enabled=True,
    )
    assert payload["tools"] == [client_tool, GS]
    assert payload["extra_body"] == {"other": 1}


def test_web_search_only_search_tool_when_workspace_tools_disabled(tmp_path):
    payload = _post_with_search(tmp_path, tools_enabled=False, upstream_url=GEMINI_URL, web_search_enabled=True)
    assert payload["tools"] == [GS]
    assert "extra_body" not in payload


def test_web_search_disabled_omits_google_search(tmp_path):
    payload = _post_with_search(
        tmp_path,
        body_extra={"extra_body": {"tools": [GS]}},
        upstream_url=GEMINI_URL,
        web_search_enabled=False,
    )
    assert "extra_body" not in payload
    assert all("google_search" not in t for t in payload.get("tools", []))


def test_web_search_not_added_for_non_google_upstream(tmp_path):
    payload = _post_with_search(tmp_path, web_search_enabled=True)
    assert all("google_search" not in t for t in payload.get("tools", []))
    assert "extra_body" not in payload
    payload = _post_with_search(
        tmp_path, upstream_url="https://evil.test/generativelanguage.googleapis.com/v1", web_search_enabled=True
    )
    assert all("google_search" not in t for t in payload.get("tools", []))


def test_config_file_toggles_apply_to_subsequent_requests(tmp_path):
    cfg_path = tmp_path / "config.json"

    async def scenario():
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return ok_json(request)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with DatabaseManager(tmp_path / "m.db") as db:
            resolver = ContextResolver(db, SessionLedger())
            config = Config(upstream_api_key="sk-test", upstream_url=GEMINI_URL)
            app = create_app(config, resolver, upstream, config_path=cfg_path)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://strand") as client:
                body = {"messages": [{"role": "user", "content": "hello"}]}
                await client.post("/v1/chat/completions", json=body)  # no file: boot config
                cfg_path.write_text(json.dumps({"web_search_enabled": True}))
                await client.post("/v1/chat/completions", json=body)
                cfg_path.write_text(json.dumps({"web_search_enabled": False, "tools_enabled": False}))
                await client.post("/v1/chat/completions", json=body)
                cfg_path.write_text("{not json")
                await client.post("/v1/chat/completions", json=body)  # bad file keeps last good
            await upstream.aclose()
        return [body_of(r) for r in seen]

    first, second, third, fourth = run(scenario())
    assert "extra_body" not in first and first["tools"]
    assert second["tools"] == [GS] and "extra_body" not in second
    assert "extra_body" not in third and "tools" not in third
    assert "extra_body" not in fourth and "tools" not in fourth
