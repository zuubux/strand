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


def _onboard_scenario(tmp_path, monkeypatch, persons, headers=None, text="hello there"):
    import strand.proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "get_host_context", lambda: dict(HOST))

    async def scenario():
        seen: list[httpx.Request] = []
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (seen.append(r), ok_json(r))[1]))
        async with DatabaseManager(tmp_path / "m.db") as db:
            for name, origin in persons:
                await db.upsert_entity(name, "person", "x", origin=origin, confidence=1.0 if origin == "user_explicit" else 0.6)
            app = create_app(Config(upstream_url="http://upstream.test/v1"), ContextResolver(db, SessionLedger()), upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://strand") as client:
                await _post_chat(client, text, headers)
                await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
        return _systems(seen)

    return run(scenario())


def test_day_zero_triggers_without_user_explicit_person(tmp_path, monkeypatch):
    (system,) = _onboard_scenario(tmp_path, monkeypatch, [("Inferred Ian", "model_inferred")])
    assert "[Onboarding Directive" in system
    assert "Intent Triage" not in system


def test_day_zero_does_not_trigger_with_user_explicit_person(tmp_path, monkeypatch):
    (system,) = _onboard_scenario(tmp_path, monkeypatch, [("Alice", "user_explicit")], text="Tell me about Alice")
    assert "[Onboarding Directive" not in system


def test_graduated_database_boots_new_sessions_without_onboarding(tmp_path, monkeypatch):
    import strand.proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "get_host_context", lambda: dict(HOST))

    async def scenario():
        seen: list[httpx.Request] = []
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (seen.append(r), ok_json(r))[1]))
        async with DatabaseManager(tmp_path / "m.db") as db:
            alice = await db.upsert_entity("Alice", "person", "x", origin="user_explicit", confidence=1.0)
            pref = await db.upsert_entity("Host Telemetry On-Demand", "preference", "x")
            await db.upsert_edge(alice, pref, "prefers")
            app = create_app(Config(upstream_url="http://upstream.test/v1"), ContextResolver(db, SessionLedger()), upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://strand") as client:
                await _post_chat(client, "hello there")
                await _post_chat(client, "fix the bug in foo.py", headers={"X-Strand-Mode": "onboarding"})
                await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
        return _systems(seen)

    ambient, explicit = run(scenario())
    assert "[Onboarding Directive" not in ambient
    assert "Hi, I'm Strand" not in ambient
    assert "TASK-YIELD CONSTRAINT" not in ambient
    assert "Intent Triage" in explicit  # explicit /onboard still works


def test_directive_envelope_has_task_yield_and_question_budget(tmp_path, monkeypatch):
    (system,) = _onboard_scenario(tmp_path, monkeypatch, [])
    assert "TASK-YIELD CONSTRAINT" in system
    assert "Default question budget = 0." in system


def test_turn_one_directive_forces_name_question_and_softens_user_fallback(tmp_path, monkeypatch):
    (system,) = _onboard_scenario(tmp_path, monkeypatch, [])
    assert "Hi, I'm Strand. What should I call you?" in system
    assert "Do not reflexively default to the local account username ($USER)" in system
    assert "if the operator is coy, deflects, or expresses indifference" in system
    assert "Ask for their preferred name rather than assuming the Linux account name" in system
    assert "host workstation-01" in system  # specs stay ambient


def test_later_turn_directive_covers_greeting_hostname_and_mission(tmp_path, monkeypatch):
    from strand.onboarding import onboarding_directive

    text = onboarding_directive(HOST, first_turn=False)
    assert "running locally on workstation-01" in text
    assert "In 1-2 plain sentences, explain what you do" in text
    assert "you keep memory and context locally on this computer" in text
    assert "strip out sensitive info before anything goes out" in text
    assert "mission" not in text.lower()
    assert "sovereign" not in text.lower()
    assert "What should I call you?" not in text


def test_onboard_on_existing_database_triggers_intent_triage(tmp_path, monkeypatch):
    (system,) = _onboard_scenario(
        tmp_path, monkeypatch, [("Alice", "user_explicit")], headers={"X-Strand-Mode": "onboarding"}
    )
    assert "Intent Triage" in system
    assert "1. Update their name/identity." in system
    assert "2. Recalibrate communication directives, tone, and formatting." in system
    assert "3. Re-run the full onboarding dialogue." in system
    assert "handled in Settings" in system
    assert "What should I call you?" not in system


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


def test_model_b_operator_completes_day_zero_and_folds_generic_user(tmp_path, monkeypatch):
    import strand.extractors as ex
    import strand.proxy as proxy_mod
    from strand.distill import DistillationEngine
    from strand.extractors import LLMExtractor
    from strand.onboarding import count_operators

    monkeypatch.setattr(proxy_mod, "get_host_context", lambda: dict(HOST))
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
                {"role": "assistant", "content": "Hi, I'm Strand. What should I call you?"},
                {"role": "user", "content": "Alicia is fine"},
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


def _telemetry_system(tmp_path, monkeypatch, config=None, headers=None):
    import strand.proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "get_host_context", lambda: dict(FULL_HOST))
    config = config or Config(upstream_url="http://upstream.test/v1")

    async def scenario():
        seen: list[httpx.Request] = []
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (seen.append(r), ok_json(r))[1]))
        async with DatabaseManager(tmp_path / "m.db") as db:
            app = create_app(config, ContextResolver(db, SessionLedger()), upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://strand") as client:
                for hdrs in headers or [{}]:
                    await _post_chat(client, "hello there", hdrs)
                await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
        return _systems(seen)

    return run(scenario())


def test_host_telemetry_off_omits_specs_but_keeps_hostname(tmp_path, monkeypatch):
    (system,) = _telemetry_system(tmp_path, monkeypatch)
    assert "[Onboarding Directive" in system
    assert "host workstation-01" in system
    for token in SPEC_TOKENS:
        assert token not in system
    assert "CPU" not in system and "RAM" not in system


def test_host_telemetry_config_true_injects_full_specs(tmp_path, monkeypatch):
    config = Config(upstream_url="http://upstream.test/v1", enable_host_telemetry=True)
    (system,) = _telemetry_system(tmp_path, monkeypatch, config=config)
    for token in SPEC_TOKENS:
        assert token in system
    assert "host workstation-01" in system


def test_host_telemetry_session_override_activates_mid_session(tmp_path, monkeypatch):
    first, second, third = _telemetry_system(
        tmp_path,
        monkeypatch,
        headers=[{}, {"X-Strand-Host-Telemetry": "true"}, {}],
    )
    assert "Test CPU 9000X" not in first
    assert "Test CPU 9000X" in second and "Fedora Linux 44" in second
    assert "Test CPU 9000X" in third  # the override persists for the session


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


def _telemetry_choice_scenario(tmp_path, monkeypatch, target):
    """Onboarding session where Model B distills a hardware choice; then one more turn."""
    import strand.proxy as proxy_mod
    from strand.distill import DistillationEngine
    from strand.extractors import LLMExtractor

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(proxy_mod, "get_host_context", lambda: dict(FULL_HOST))
    payload = {
        "entities": [
            {"name": "Alice", "archetype": "person", "summary": "Operator", "origin": "user_explicit"},
            {"name": target, "archetype": "preference", "summary": "Hardware telemetry stance"},
        ],
        "edges": [{"source": "Alice", "target": target, "relation": "prefers"}],
    }

    async def scenario():
        seen: list[httpx.Request] = []
        answer = httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "Understood, tuning the rig settings now."}}]}
        )
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (seen.append(r), answer)[1]))
        slm = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})
            )
        )
        ledger = SessionLedger()
        async with DatabaseManager(tmp_path / "m.db") as db:
            engine = DistillationEngine(db, extractor=LLMExtractor("http://slm.test/v1", "tiny", client=slm))
            app = create_app(Config(upstream_url="http://upstream.test/v1"), ContextResolver(db, ledger), upstream, engine)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://strand") as client:
                history = [{"role": "user", "content": "Alice"}]
                for text in ("Yeah, check it out", "anything else?"):
                    history.append({"role": "user", "content": text})
                    await client.post("/v1/chat/completions", json={"messages": list(history), "session_id": "s"})
                    await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
            await slm.aclose()
        return seen, ledger

    seen, ledger = run(scenario())
    config_path = tmp_path / ".config" / "strand" / "config.json"
    return _systems(seen), ledger, config_path


def test_conversational_hardware_opt_in_activates_session_and_persists(tmp_path, monkeypatch):
    systems, ledger, config_path = _telemetry_choice_scenario(tmp_path, monkeypatch, "Host Telemetry Enabled")
    assert "Test CPU 9000X" not in systems[0]  # Turn 1/2: no eager hardware dump
    assert ledger.allow_host_telemetry("s") is True
    assert "Test CPU 9000X" in systems[1] and "32 GB" in systems[1]  # very next turn carries specs
    assert json.loads(config_path.read_text()) == {"enable_host_telemetry": True}
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_conversational_hardware_decline_keeps_specs_redacted_and_persists_false(tmp_path, monkeypatch):
    systems, ledger, config_path = _telemetry_choice_scenario(tmp_path, monkeypatch, "Host Telemetry On-Demand")
    assert ledger.allow_host_telemetry("s") is False
    assert all("Test CPU 9000X" not in s for s in systems)
    assert json.loads(config_path.read_text()) == {"enable_host_telemetry": False}


def test_no_telemetry_edge_writes_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _telemetry_system(tmp_path, monkeypatch)
    assert not (tmp_path / ".config" / "strand" / "config.json").exists()


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
