import asyncio
import json

import httpx

from strand.db import DatabaseManager
from strand.distill import DistillationEngine
from strand.extractors import LLMExtractor, parse_extraction

QUERY = "I am building the Atlas project with Alice every week."
RESPONSE = "Great, Atlas sounds like a solid project and Alice seems like a strong lead."
GOOD = {
    "entities": [
        {"name": "Alice", "archetype": "person", "summary": "Lead developer"},
        {"name": "Atlas", "archetype": "project", "summary": "Main project"},
    ],
    "edges": [{"source": "Alice", "target": "Atlas", "relation": "works_on"}],
}


def run(coro):
    return asyncio.run(coro)


def extractor_for(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMExtractor("http://slm.test/v1", "tiny", client=client)


def reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_valid_json_is_parsed_and_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return reply(json.dumps(GOOD))

    result = run(extractor_for(handler)(QUERY, RESPONSE))
    assert seen["url"] == "http://slm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer ollama"
    assert seen["body"]["model"] == "tiny"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert [e["name"] for e in result["entities"]] == ["Alice", "Atlas"]
    assert result["edges"] == GOOD["edges"]


def test_fenced_json_is_accepted():
    result = run(extractor_for(lambda r: reply("```json\n" + json.dumps(GOOD) + "\n```"))(QUERY, RESPONSE))
    assert len(result["entities"]) == 2 and len(result["edges"]) == 1


def test_invalid_json_falls_back_to_empty():
    for content in ("not json at all", "{broken", "[1, 2]", ""):
        result = run(extractor_for(lambda r, c=content: reply(c))(QUERY, RESPONSE))
        assert result["entities"] == [] and result["edges"] == []


def test_malformed_items_are_dropped():
    result = parse_extraction(
        json.dumps({"entities": [{"name": ""}, "x", {"name": "Ok"}], "edges": [{"source": "a"}, 3]})
    )
    assert [e["name"] for e in result["entities"]] == ["Ok"] and result["edges"] == []


def test_connection_error_and_http_error_fall_back_to_empty():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    for handler in (boom, lambda r: httpx.Response(500, text="oops"), lambda r: httpx.Response(200, json={})):
        result = run(extractor_for(handler)(QUERY, RESPONSE))
        assert result["entities"] == [] and result["edges"] == []


def _distill(tmp_path, extractor):
    async def go():
        async with DatabaseManager(tmp_path / "d.db") as db:
            episode = await DistillationEngine(db, extractor=extractor).process_exchange("s", QUERY, RESPONSE)
            async with db.conn.execute("SELECT name FROM entities ORDER BY name") as cur:
                names = [r[0] for r in await cur.fetchall()]
            async with db.conn.execute("SELECT COUNT(*) FROM edges") as cur:
                edges = (await cur.fetchone())[0]
            return episode, names, edges

    return run(go())


def test_engine_integration_persists_extraction(tmp_path):
    episode, names, edges = _distill(tmp_path, extractor_for(lambda r: reply(json.dumps(GOOD))))
    assert episode is not None and names == ["Alice", "Atlas"] and edges == 1


def test_engine_survives_bad_extractor_output(tmp_path):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    episode, names, edges = _distill(tmp_path, extractor_for(boom))
    assert episode is not None and names == [] and edges == 0


def _two_hop(container, generic, kind="preference"):
    return {
        "entities": [
            {"name": "User", "archetype": "person", "summary": "Operator"},
            {"name": container, "archetype": kind, "summary": "grouping"},
            {"name": "AWD", "archetype": "concept", "summary": "All-wheel drive"},
        ],
        "edges": [
            {"source": "User", "target": container, "relation": "has"},
            {"source": container, "target": "AWD", "relation": generic},
        ],
    }


def test_preference_container_collapses_to_direct_triple():
    result = parse_extraction(json.dumps(_two_hop("User's Preference", "includes")))
    assert [e["name"] for e in result["entities"]] == ["User", "AWD"]
    assert result["edges"] == [{"source": "User", "target": "AWD", "relation": "prefers"}]


def test_concern_container_collapses_with_concern_relation():
    result = parse_extraction(json.dumps(_two_hop("User's Concern", "contains", "trait")))
    assert result["edges"] == [{"source": "User", "target": "AWD", "relation": "concerned_about"}]
    assert "User's Concern" not in [e["name"] for e in result["entities"]]


def test_container_with_specific_relation_keeps_it_and_orphan_uses_owner():
    data = {
        "entities": [{"name": "User Settings", "archetype": "preference"}, {"name": "Dark Mode"}],
        "edges": [{"source": "User Settings", "target": "Dark Mode", "relation": "enables"}],
    }
    result = parse_extraction(json.dumps(data))
    assert [e["name"] for e in result["entities"]] == ["Dark Mode"]
    assert result["edges"] == [{"source": "User", "target": "Dark Mode", "relation": "enables"}]


def test_container_with_no_children_is_dropped_and_real_entities_untouched():
    data = {
        "entities": [{"name": "User's Preference"}, {"name": "Preference Engine"}],
        "edges": [{"source": "User", "target": "User's Preference", "relation": "has"}],
    }
    result = parse_extraction(json.dumps(data))
    assert [e["name"] for e in result["entities"]] == ["Preference Engine"]
    assert result["edges"] == []
    assert parse_extraction(json.dumps(GOOD))["edges"] == GOOD["edges"]


def test_system_prompt_forbids_intermediate_containers():
    from strand.extractors import SYSTEM_PROMPT

    assert "(User) --[prefers]--> (AWD)" in SYSTEM_PROMPT
    assert "Never create generic intermediate container" in SYSTEM_PROMPT


def _operator_payload(name="Alice", host="workstation-01"):
    return {
        "entities": [{"name": name, "archetype": "person", "summary": "Operator", "origin": "user_explicit", "confidence": 1.0}],
        "edges": [{"source": name, "target": host, "relation": "operates_host"}],
    }


def test_extractor_keeps_model_b_operator_as_user_explicit(monkeypatch):
    import strand.extractors as ex

    monkeypatch.setattr(ex, "get_host_context", lambda: {"hostname": "workstation-01"})
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return reply(json.dumps(_operator_payload()))

    result = run(extractor_for(handler)("Alice works", RESPONSE))
    assert "Host: workstation-01" in seen["body"]["messages"][1]["content"]
    (person,) = [e for e in result["entities"] if e["archetype"] == "person"]
    assert person["name"] == "Alice"
    assert person["origin"] == "user_explicit" and person["confidence"] == 1.0
    (edge,) = result["edges"]
    assert (edge["source"], edge["target"], edge["relation"]) == ("Alice", "workstation-01", "operates_host")
    assert edge["origin"] == "user_explicit"


def test_non_person_or_unflagged_entities_stay_model_inferred():
    payload = {
        "entities": [
            {"name": "Atlas", "archetype": "project", "origin": "user_explicit", "confidence": 1.0},
            {"name": "Alice", "archetype": "person"},
        ],
        "edges": [{"source": "Alice", "target": "Atlas", "relation": "operates_host"}],
    }
    result = parse_extraction(json.dumps(payload))
    assert all("origin" not in e and "confidence" not in e for e in result["entities"])
    assert all("origin" not in e for e in result["edges"])


def test_name_heuristics_are_gone():
    import strand.extractors as ex

    for attr in ("parse_name_answer", "declared_name", "anchor_declared_operator", "EXPECTING_NAME"):
        assert not hasattr(ex, attr), attr


def test_system_prompt_delegates_name_semantics_to_model_b():
    from strand.extractors import SYSTEM_PROMPT

    assert "What should I call you?" in SYSTEM_PROMPT
    for variant in ("'Alice'", "'Just Alice'", "'Alice works'", "'Alicia is fine'"):
        assert variant in SYSTEM_PROMPT, variant
    assert 'archetype "person", origin "user_explicit" and confidence 1.0' in SYSTEM_PROMPT
    assert "(<Declared Name>) --[operates_host]--> (<Hostname>)" in SYSTEM_PROMPT


def test_model_b_operator_persists_and_reanchors_generic_user(tmp_path, monkeypatch):
    import strand.extractors as ex
    from strand.onboarding import count_operators, reanchor_generic_user

    monkeypatch.setattr(ex, "get_host_context", lambda: {"hostname": "workstation-01"})

    async def go(answer, n):
        async with DatabaseManager(tmp_path / f"m{n}.db") as db:
            generic = await db.upsert_entity("User", "person", "generic", origin="model_inferred", confidence=0.6)
            rule = await db.upsert_entity("No apologies", "directive", "never apologize")
            await db.upsert_edge(generic, rule, "demands")
            assert await count_operators(db) == 0
            extractor = extractor_for(lambda r: reply(json.dumps(_operator_payload())))
            await DistillationEngine(db, extractor=extractor).process_exchange("s", answer, RESPONSE)
            assert await count_operators(db) > 0
            assert await reanchor_generic_user(db)
            async with db.conn.execute("SELECT name, origin, confidence FROM entities WHERE archetype = 'person'") as cur:
                people = [tuple(r) for r in await cur.fetchall()]
            async with db.conn.execute(
                "SELECT s.name, t.name, e.relation FROM edges e JOIN entities s ON s.id = e.source_id "
                "JOIN entities t ON t.id = e.target_id"
            ) as cur:
                return people, {tuple(r) for r in await cur.fetchall()}

    for n, answer in enumerate(("Alice", "Just Alice", "Alice works", "Alicia is fine")):
        people, edges = run(go(answer, n))
        assert people == [("Alice", "user_explicit", 1.0)], answer
        assert ("Alice", "workstation-01", "operates_host") in edges, answer
        assert ("Alice", "No apologies", "demands") in edges, answer


def test_question_answer_with_no_operator_payload_leaves_day_zero_open(tmp_path):
    from strand.onboarding import count_operators

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            extractor = extractor_for(lambda r: reply(json.dumps({"entities": [], "edges": []})))
            await DistillationEngine(db, extractor=extractor).process_exchange("s", "Why do you ask?", RESPONSE)
            return await count_operators(db)

    assert run(go()) == 0


def test_reanchor_is_noop_without_declared_operator(tmp_path):
    from strand.onboarding import reanchor_generic_user

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            generic = await db.upsert_entity("User", "person", "generic", origin="model_inferred", confidence=0.6)
            rule = await db.upsert_entity("No apologies", "directive", "never apologize")
            await db.upsert_edge(generic, rule, "demands")
            folded = await reanchor_generic_user(db)
            async with db.conn.execute("SELECT COUNT(*) FROM edges") as cur:
                return folded, (await cur.fetchone())[0]

    assert run(go()) == (False, 1)


def test_model_b_operator_without_host_edge_still_counts_as_operator(tmp_path):
    from strand.onboarding import count_operators

    payload = {"entities": [{"name": "Alice", "archetype": "person", "origin": "user_explicit"}], "edges": []}

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            extractor = extractor_for(lambda r: reply(json.dumps(payload)))
            await DistillationEngine(db, extractor=extractor).process_exchange("s", "Alice works for me", RESPONSE)
            return await count_operators(db)

    assert run(go()) == 1


# --- OBS-POL-005 Phase 1: critique-driven deprecation with permanence guardrails ----------------


def _critique_extractor(seen):
    """Offline Model B stand-in obeying the prompt contract: only permanence markers deprecate."""
    permanent = {
        "entities": [{"name": "no-diff-summaries", "archetype": "directive", "summary": "Never summarize diffs"}],
        "edges": [{"source": "Alice", "target": "no-diff-summaries", "relation": "demands"}],
        "deprecated_edges": [
            {"source": "Alice", "target": "diff-summaries", "relation": "prefers", "reason": "stopped"}
        ],
    }

    def handler(request):
        body = json.loads(request.content)
        user_msg = body["messages"][1]["content"]
        seen.append(body["messages"][0]["content"])
        payload = permanent if "from now on" in user_msg else {"entities": [], "edges": [], "deprecated_edges": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    return extractor_for(handler)


def test_prompt_documents_permanence_guardrail():
    from strand.extractors import SYSTEM_PROMPT

    for token in (
        "PERMANENT DIRECTIVES",
        "TRANSIENT STEERING",
        "from now on",
        "going forward",
        "right now",
        "just for this",
        "must NOT deprecate any standing edge",
        "model_inferred",
    ):
        assert token in SYSTEM_PROMPT, token


def test_transient_phrase_does_not_deprecate_but_permanent_phrase_does():
    seen = []
    extractor = _critique_extractor(seen)
    transient = run(extractor("Give me the diff right now.", "Here it is."))
    permanent = run(extractor("Stop summarizing diffs from now on.", "Understood."))
    assert transient["deprecated_edges"] == [] and transient["entities"] == []
    assert [(e["target"], e["relation"]) for e in permanent["deprecated_edges"]] == [("diff-summaries", "prefers")]
    assert permanent["entities"][0]["archetype"] == "directive"
    assert len(seen) == 2 and "TRANSIENT STEERING" in seen[0]


# --- Historical statements are active biography, never deprecations -----------------------------


def test_prompt_separates_biography_from_deprecation():
    from strand.extractors import SYSTEM_PROMPT

    for token in ("WRONG, cancelled or should be forgotten", "I used to own an W34", "previously_owned", "NEVER placed in deprecated_edges"):
        assert token in SYSTEM_PROMPT, token


def test_used_to_own_emits_active_user_explicit_edge_not_deprecation():
    payload = {
        "entities": [
            {"name": "Alice", "archetype": "person", "origin": "user_explicit", "summary": "Operator"},
            {"name": "W34", "archetype": "vehicle", "summary": "Acme Sedan"},
        ],
        "edges": [{"source": "Alice", "target": "W34", "relation": "previously_owned"}],
        "deprecated_edges": [],
    }
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"][1]["content"])
        return reply(json.dumps(payload))

    result = run(extractor_for(handler)("I used to own an W34.", "Nice, a classic Acme Sedan."))
    assert result["deprecated_edges"] == []
    edge = result["edges"][0]
    assert (edge["relation"], edge["origin"], edge["confidence"]) == ("previously_owned", "user_explicit", 1.0)
    assert result["entities"][1]["origin"] == "user_explicit"


def test_historical_edge_persists_active_through_engine(tmp_path):
    content = json.dumps(
        {
            "entities": [{"name": "Cruiser", "archetype": "vehicle", "summary": "Kestrel"}],
            "edges": [{"source": "User", "target": "Cruiser", "relation": "previously_owned"}],
        }
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("User", "person", "The operator")
            engine = DistillationEngine(db, extractor=lambda q, r: parse_extraction(content))
            await engine.process_exchange("s", "My previous car was a Cruiser, sold years ago.", "Noted, a Kestrel Cruiser.")
            return [dict(r) for r in await db.get_entity_subgraph(1, active_only=True)]

    rows = run(scenario())
    assert [(r["relation"], r["weight"], r["origin"], r["confidence"]) for r in rows] == [
        ("previously_owned", 1.0, "user_explicit", 1.0)
    ]


def test_assistant_only_assertion_stays_untagged_for_model_clamp():
    content = json.dumps({"edges": [{"source": "Alice", "target": "Atlas", "relation": "works_on"}]})
    edge = parse_extraction(content)["edges"][0]
    assert "origin" not in edge and "confidence" not in edge


def test_system_prompt_documents_host_telemetry_preference_without_collision():
    from strand.extractors import SYSTEM_PROMPT

    assert "(<Operator>) --[prefers]--> (Host Telemetry Enabled)" in SYSTEM_PROMPT
    assert "(<Operator>) --[prefers]--> (Host Telemetry On-Demand)" in SYSTEM_PROMPT
    assert "never deprecates or replaces unrelated directives" in SYSTEM_PROMPT
    assert "Biographical history is never a deprecation" in SYSTEM_PROMPT  # existing rules intact


def test_model_b_telemetry_preference_distills_beside_other_preferences(tmp_path):
    payload = {
        "entities": [
            {"name": "Alice", "archetype": "person", "summary": "Operator", "origin": "user_explicit"},
            {"name": "Host Telemetry Enabled", "archetype": "preference", "summary": "Specs allowed"},
        ],
        "edges": [{"source": "Alice", "target": "Host Telemetry Enabled", "relation": "prefers"}],
    }

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            alice = await db.upsert_entity("Alice", "person", "Operator", origin="user_explicit")
            terse = await db.upsert_entity("Terse diffs", "preference", "unified diffs")
            await db.upsert_edge(alice, terse, "prefers")
            extractor = extractor_for(lambda r: reply(json.dumps(payload)))
            await DistillationEngine(db, extractor=extractor).process_exchange("s", "Yeah, check it out", RESPONSE)
            async with db.conn.execute(
                "SELECT t.name, e.weight FROM edges e JOIN entities t ON t.id = e.target_id "
                "WHERE e.relation = 'prefers' ORDER BY t.name"
            ) as cur:
                return [tuple(r) for r in await cur.fetchall()]

    assert run(go()) == [("Host Telemetry Enabled", 1.0), ("Terse diffs", 1.0)]


def test_failing_extractor_logs_masked_url(caplog):
    import logging

    from strand.logging import LOGGER_NAME

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    extractor = LLMExtractor("http://user:hunter2@slm.test/v1", "tiny", client=client)
    parent = logging.getLogger(LOGGER_NAME)
    parent.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            run(extractor(QUERY, RESPONSE))
    finally:
        parent.removeHandler(caplog.handler)
    assert "hunter2" not in caplog.text
    assert "user:********@slm.test" in caplog.text
