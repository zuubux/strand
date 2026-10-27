import asyncio
import json
import logging

import httpx

from cortext.config import Config
from cortext.db import DatabaseManager
from cortext.distill import DistillationEngine, is_substantive_exchange, strip_markdown_structure
from cortext.proxy import create_app
from cortext.resolver import ContextResolver
from cortext.session import SessionLedger

QUERY = "Tell me about Marcus."
RESPONSE = "Marcus is a staff engineer. Marcus manages Atlas. Atlas is the billing project."


def run(coro):
    return asyncio.run(coro)


async def _rows(db, sql):
    async with db.conn.execute(sql) as cur:
        return [tuple(r) for r in await cur.fetchall()]


def test_process_exchange_persists_entities_edges_and_episode(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            episode_id = await DistillationEngine(db).process_exchange("s1", QUERY, RESPONSE)
            return (
                episode_id,
                await _rows(db, "SELECT id, name, summary FROM entities ORDER BY name"),
                await _rows(db, "SELECT source_id, target_id, relation FROM edges"),
                await _rows(db, "SELECT id, session_id, narrative, extracted_entities FROM episodes"),
            )

    episode_id, entities, edges, episodes = run(scenario())
    by_name = {name: (eid, summary) for eid, name, summary in entities}
    assert {"Marcus", "Atlas"} <= set(by_name)
    assert by_name["Marcus"][1].startswith("a staff engineer")
    assert (by_name["Marcus"][0], by_name["Atlas"][0], "manages") in edges
    ((eid, session_id, narrative, extracted),) = episodes
    assert eid == episode_id and session_id == "s1"
    assert "Marcus" in narrative
    assert set(json.loads(extracted)) == {e[0] for e in entities}


def test_custom_extractor_and_summary_preserved_on_edge_only_update(tmp_path):
    async def extractor(query, response):
        return {
            "entities": [{"name": "Ada", "archetype": "person", "summary": "Mathematician"}],
            "edges": [{"source": "Ada", "target": "Engine", "relation": "designs", "weight": 2.0}],
            "summary": "Discussed Ada.",
        }

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db, extractor).process_exchange("s", "q", "r")
            return (
                await _rows(db, "SELECT name, archetype, summary FROM entities ORDER BY name"),
                await _rows(db, "SELECT relation, weight FROM edges"),
                await _rows(db, "SELECT narrative FROM episodes"),
            )

    entities, edges, episodes = run(scenario())
    assert entities == [("Ada", "person", "Mathematician"), ("Engine", "concept", "")]
    assert edges == [("designs", 2.0)]
    assert episodes == [("Discussed Ada.",)]


def test_extractor_failure_is_logged_not_raised(tmp_path, caplog):
    def boom(query, response):
        raise RuntimeError("nope")

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            result = await DistillationEngine(db, boom).process_exchange("s", "q", "r")
            return result, await _rows(db, "SELECT id FROM episodes")

    with caplog.at_level(logging.WARNING, logger="cortext.distill"):
        result, episodes = run(scenario())
    assert result is None and episodes == []
    assert "Distillation failed" in caplog.text


def test_is_substantive_exchange_trivial_vs_substantive():
    reply = "Happy to help with that."
    for q in ["", "   ", "hi", "Hello!", "ok", "thanks", "Thank you", "sounds good", "bye", "ok thanks!"]:
        assert not is_substantive_exchange(q, reply), q
    assert not is_substantive_exchange("Tell me about Marcus.", "")
    assert not is_substantive_exchange("Tell me about Marcus.", "   ")
    assert not is_substantive_exchange("Tell me about Marcus.", "Okay.")
    assert not is_substantive_exchange("Tell me about Marcus.", "Hello! How can I help you today?")
    for q in [
        "Marcus is SRE Lead",
        "my VIN is WBAHD6322",
        "remember that Atlas ships Friday",
        "My car is a BMW E34",
        "Marcus agreed to lead the database migration scheduled for Tuesday",
    ]:
        assert is_substantive_exchange(q, reply), q


def test_process_exchange_skips_trivial_turns(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            engine = DistillationEngine(db)
            results = [await engine.process_exchange("s", q, "Hello! Nice to meet you, Marcus Webb.") for q in ("hi", "ok", "thanks")]
            return (
                results,
                await _rows(db, "SELECT id FROM episodes"),
                await _rows(db, "SELECT id FROM entities"),
                await _rows(db, "SELECT id FROM edges"),
            )

    results, episodes, entities, edges = run(scenario())
    assert results == [None, None, None]
    assert episodes == [] and entities == [] and edges == []


def test_process_exchange_records_substantive_exchange(tmp_path):
    query = "Marcus agreed to lead the database migration scheduled for Tuesday"
    response = "Noted. Marcus manages Atlas, and the migration is the top priority this week."

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            episode_id = await DistillationEngine(db).process_exchange("s", query, response)
            return episode_id, await _rows(db, "SELECT name FROM entities"), await _rows(db, "SELECT session_id FROM episodes")

    episode_id, entities, episodes = run(scenario())
    assert episode_id is not None
    assert ("Marcus",) in entities
    assert episodes == [("s",)]


def test_short_fact_assertion_passes_gate_and_extracts(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            episode_id = await DistillationEngine(db).process_exchange(
                "s", "My car is a BMW E34", "Got it. The BMW E34 is a classic 5 Series."
            )
            return episode_id, await _rows(db, "SELECT name FROM entities"), await _rows(db, "SELECT id FROM episodes")

    episode_id, entities, episodes = run(scenario())
    assert episode_id is not None and len(episodes) == 1
    assert any("BMW" in name for (name,) in entities)


def _proxy_scenario(tmp_path, upstream_handler, payload):
    async def scenario():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
        async with DatabaseManager(tmp_path / "m.db") as db:
            app = create_app(
                Config(upstream_url="http://upstream.test/v1", upstream_api_key="k"),
                ContextResolver(db, SessionLedger()),
                upstream,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://cortext") as client:
                response = await client.post("/v1/chat/completions", json=payload)
                await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
            return (
                response,
                await _rows(db, "SELECT name FROM entities ORDER BY name"),
                await _rows(db, "SELECT session_id, narrative FROM episodes"),
            )

    return run(scenario())


def test_proxy_non_streaming_distills_in_background(tmp_path):
    def upstream(_):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": RESPONSE}}]})

    payload = {"model": "m", "session_id": "abc", "messages": [{"role": "user", "content": QUERY}]}
    response, entities, episodes = _proxy_scenario(tmp_path, upstream, payload)
    assert response.status_code == 200
    assert ("Marcus",) in entities and ("Atlas",) in entities
    ((session_id, narrative),) = episodes
    assert session_id == "abc" and "Marcus" in narrative


def test_proxy_streaming_distills_after_stream_completes(tmp_path):
    words = RESPONSE.split(" ")
    chunks = [
        f'data: {json.dumps({"choices": [{"delta": {"content": w + " "}}]})}\n\n' for w in words
    ] + ["data: [DONE]\n\n"]
    body = "".join(chunks).encode()

    def upstream(_):
        return httpx.Response(200, stream=httpx.ByteStream(body), headers={"content-type": "text/event-stream"})

    payload = {"model": "m", "stream": True, "messages": [{"role": "user", "content": QUERY}]}
    response, entities, episodes = _proxy_scenario(tmp_path, upstream, payload)
    assert response.content == body
    assert ("Marcus",) in entities and ("Atlas",) in entities
    assert len(episodes) == 1


def test_proxy_upstream_error_does_not_distill(tmp_path):
    def upstream(_):
        return httpx.Response(500, json={"error": "x"})

    payload = {"model": "m", "messages": [{"role": "user", "content": QUERY}]}
    response, entities, episodes = _proxy_scenario(tmp_path, upstream, payload)
    assert response.status_code == 500
    assert entities == [] and episodes == []


BMW_QUERY = "My primary project car is a 1995 BMW 540i with an M60 V8. Remember that."
BMW_RESPONSE = "Got it. Whenever you ask, I will recall it. Classic choice."


def test_heuristic_links_car_to_engine_and_filters_noise(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db).process_exchange("s", BMW_QUERY, BMW_RESPONSE)
            return (
                await _rows(db, "SELECT name, origin, confidence FROM entities"),
                await _rows(
                    db,
                    "SELECT s.name, t.name, e.relation, e.weight, e.origin, e.confidence FROM edges e "
                    "JOIN entities s ON s.id = e.source_id JOIN entities t ON t.id = e.target_id",
                ),
            )

    entities, edges = run(scenario())
    names = {n for n, _, _ in entities}
    assert {"BMW 540i", "M60 V8"} <= names
    assert all(o == "user_explicit" and c == 1.0 for _, o, c in entities)
    assert ("BMW 540i", "M60 V8", "has_engine", 1.0, "user_explicit", 1.0) in edges
    assert not {n.lower() for n in names} & {"remember", "got", "whenever", "classic", "my"}
    assert len(names) == 2


CAR_QUERY = "My primary project car is a 1995 BMW 540i with an M60 V8"
CAR_RESPONSE = "Great, a 1995 BMW 540i with an M60 V8 is a classic."


def test_multiword_entity_gets_constituent_aliases(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db).process_exchange("s", CAR_QUERY, CAR_RESPONSE)
            return await _rows(db, "SELECT name, metadata FROM entities")

    metadata = {name: json.loads(meta) for name, meta in run(scenario())}
    assert "BMW 540i" in metadata
    assert {"BMW", "540i"} <= set(metadata["BMW 540i"]["aliases"])

def test_single_token_candidate_consolidates_into_existing_multiword_entity(tmp_path):
    async def extractor(query, response):
        return {
            "entities": [{"name": "BMW", "archetype": "vehicle"}, {"name": "Marcus"}],
            "edges": [{"source": "Marcus", "target": "BMW", "relation": "drives"}],
        }

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db).process_exchange("s", CAR_QUERY, CAR_RESPONSE)
            await db.conn.execute("UPDATE entities SET updated_at = '2000-01-01 00:00:00' WHERE name = 'BMW 540i'")
            await db.conn.commit()
            await DistillationEngine(db, extractor=extractor).process_exchange("s2", CAR_QUERY, CAR_RESPONSE)
            return (
                await _rows(db, "SELECT name FROM entities"),
                await _rows(db, "SELECT updated_at FROM entities WHERE name = 'BMW 540i'"),
                await _rows(db, "SELECT e.name FROM edges g JOIN entities e ON e.id = g.target_id WHERE g.relation = 'drives'"),
            )

    names, updated, targets = run(scenario())
    assert "BMW" not in {n for (n,) in names}
    assert updated != [("2000-01-01 00:00:00",)]
    assert ("BMW 540i",) in targets




def test_edge_free_inferred_entities_are_never_persisted(tmp_path):
    async def extractor(query, response):
        return {
            "entities": [
                {"name": "BMW Longlife-01", "archetype": "concept", "origin": "model_inferred"},
                {"name": "BMW", "archetype": "vehicle", "origin": "model_inferred"},
                {"name": "Ada", "archetype": "person", "origin": "model_inferred"},
                {"name": "Zed", "archetype": "person", "origin": "user_explicit", "confidence": 1.0},
            ],
            "edges": [{"source": "Ada", "target": "Engine", "relation": "designs", "origin": "model_inferred"}],
        }

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db).process_exchange("s0", CAR_QUERY, CAR_RESPONSE)
            await DistillationEngine(db, extractor=extractor).process_exchange("s1", CAR_QUERY, CAR_RESPONSE)
            return (
                {n for (n,) in await _rows(db, "SELECT name FROM entities")},
                await _rows(db, "SELECT COUNT(*) FROM edges"),
            )

    names, _ = run(scenario())
    assert not names & {"BMW Longlife-01", "BMW"}
    assert {"Ada", "Engine", "Zed"} <= names  # connected inferred + user_explicit kept


def test_resolver_matches_alias_token_and_returns_engine_edge(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db).process_exchange("s", CAR_QUERY, CAR_RESPONSE)
            resolver = ContextResolver(db)
            matched = await resolver.match_entities("servicing the BMW this weekend")
            return [e.name for e in matched], await resolver.resolve("servicing the BMW", "s2")

    names, context = run(scenario())
    assert "BMW 540i" in names
    assert "BMW 540i -has_engine-> M60 V8" in context


def test_resolver_token_fallback_without_aliases_and_ignores_short_tokens(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Volvo V70 Wagon", "vehicle", "")
            resolver = ContextResolver(db)
            return [
                [e.name for e in await resolver.match_entities(q)]
                for q in ("fix the wagon", "wagons are long", "the v7 thing")
            ]

    assert run(scenario()) == [["Volvo V70 Wagon"], [], []]


def test_token_match_outranks_inferred_exact_noise(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            car = await db.upsert_entity("BMW 540i", "vehicle", "")
            engine = await db.upsert_entity("M60 V8", "system", "")
            await db.upsert_edge(car, engine, "has_engine")
            for noise in ("BMW", "Engine", "Weight", "Capacity", "Look"):
                await db.upsert_entity(noise, "concept", "", origin="model_inferred", confidence=0.6)
            resolver = ContextResolver(db, max_entities=5)
            query = "What engine oil weight and capacity should I look for servicing the BMW?"
            return [e.name for e in await resolver.match_entities(query)], await resolver.resolve(query, "s")

    names, context = run(scenario())
    assert names[0] == "BMW 540i"
    assert "BMW 540i -has_engine-> M60 V8" in context


def test_ungrounded_and_filler_model_entities_are_dropped(tmp_path):
    response = (
        "Important Tip: Capacity and Weight matter. Feel free to ask. Note that Classic cars vary. "
        "The M60 V8 uses Nikasil. Zephyr manages Orion."
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db).process_exchange("s", "Tell me about the M60 V8 engine", response)
            return {r[0] for r in await _rows(db, "SELECT name FROM entities")}

    names = run(scenario())
    assert "M60 V8" in names
    assert not names & {"Important Tip", "Capacity", "Weight", "Feel", "Note", "Classic", "Zephyr", "Orion"}


def test_grounded_assistant_entity_linked_to_user_topic_is_kept():
    from cortext.distill import heuristic_extract

    result = heuristic_extract("Tell me about Marcus.", "Marcus manages Atlas. Capacity is key.")
    assert {e["name"] for e in result["entities"]} == {"Marcus", "Atlas"}
    assert [(e["source"], e["target"]) for e in result["edges"]] == [("Marcus", "Atlas")]


def test_markdown_headings_and_bold_labels_do_not_become_entities(tmp_path):
    response = (
        "### Recommended Spark Plugs\n"
        "Use NGK plugs in the M60 V8.\n\n"
        "## Torque Specification\n"
        "**Torque:** 22 ft-lb\n"
        "**Recommended Spark Plugs:**\n"
        "#### Overview\n"
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db).process_exchange("s", "What spark plugs and torque spec for the M60 V8?", response)
            return {r[0] for r in await _rows(db, "SELECT name FROM entities")}

    names = run(scenario())
    assert not names & {"Recommended Spark Plugs", "Torque Specification", "Torque", "Overview"}


def test_structural_section_terms_are_filler():
    from cortext.stopwords import is_filler_name

    for name in ("Torque Specification", "Torque Specifications", "Tips", "Overview", "Instructions", "Recommendations"):
        assert is_filler_name(name), name
    assert not is_filler_name("Spark Plugs") and not is_filler_name("M60 V8")
    assert strip_markdown_structure("### Torque Specification\nNGK plugs") .strip() == "NGK plugs"
