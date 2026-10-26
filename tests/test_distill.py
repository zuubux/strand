import asyncio
import json
import logging

import httpx

from cortext.config import Config
from cortext.db import DatabaseManager
from cortext.distill import DistillationEngine, is_substantive_exchange
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
