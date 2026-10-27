import asyncio
import json
import logging

from cortext.db import DatabaseManager
from cortext.distill import DistillationEngine, heuristic_extract
from cortext.extractors import SYSTEM_PROMPT, parse_extraction
from cortext.resolver import ContextResolver
from cortext.session import SessionLedger


def run(coro):
    return asyncio.run(coro)


async def _edge(db, source_id, target_id, relation):
    async with db.conn.execute(
        "SELECT weight, confidence, metadata, updated_at FROM edges "
        "WHERE source_id = ? AND target_id = ? AND relation = ?",
        (source_id, target_id, relation),
    ) as cur:
        return await cur.fetchone()


def test_deprecate_edge_zeroes_weight_and_confidence_and_keeps_row(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Nic", "person", "")
            b = await db.upsert_entity("Subaru", "vehicle", "")
            await db.upsert_edge(a, b, "owns")
            first = await db.deprecate_edge("Nic", "Subaru", "owns", reason="sold")
            row = await _edge(db, a, b, "owns")
            second = await db.deprecate_edge("Nic", "Subaru", "owns")
            missing = await db.deprecate_edge("Nic", "Subaru", "drives")
            unknown = await db.deprecate_edge("Nic", "Nobody", "owns")
            return first, row, second, missing, unknown

    first, row, second, missing, unknown = run(scenario())
    assert first is True
    assert row["weight"] == 0.0 and row["confidence"] == 0.0 and row["updated_at"]
    meta = json.loads(row["metadata"])
    assert meta["deprecated"] is True and meta["deprecation_reason"] == "sold"
    assert second is False and missing is False and unknown is False


def test_deprecate_edge_resolves_canonical_aliases_and_ids(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Nic", "person", "")
            b = await db.upsert_entity("BMW 540i", "vehicle", "", {"aliases": ["Beemer"]})
            c = await db.upsert_entity("Postgres", "system", "")
            await db.upsert_edge(a, b, "drives")
            await db.upsert_edge(a, c, "uses")
            by_alias = await db.deprecate_edge("nic", "beemer", "Drives")
            by_id = await db.deprecate_edge(a, c, "uses")
            return by_alias, by_id, await _edge(db, a, b, "drives"), await _edge(db, a, c, "uses")

    by_alias, by_id, e1, e2 = run(scenario())
    assert by_alias is True and by_id is True
    assert e1["weight"] == 0.0 and e2["confidence"] == 0.0


def test_resolver_ignores_deprecated_edges_in_multi_hop(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Marcus", "person", "Engineer")
            b = await db.upsert_entity("Atlas", "project", "Billing")
            c = await db.upsert_entity("Postgres", "system", "Datastore")
            d = await db.upsert_entity("Redis", "system", "Cache")
            await db.upsert_edge(a, b, "leads")
            await db.upsert_edge(b, c, "runs_on")
            await db.upsert_edge(b, d, "caches_in", weight=0.0)
            await db.deprecate_edge("Marcus", "Atlas", "leads", "stepped down")
            resolver = ContextResolver(db, SessionLedger(), max_hops=2)
            marcus = await resolver.gather("Tell me about Marcus")
            text = await resolver.resolve("Tell me about Marcus", "s")
            atlas = await resolver.gather("Tell me about Atlas")
            return marcus, text, atlas

    (_, edges), text, (atlas_entities, atlas_edges) = run(scenario())
    assert edges == []  # the only path out of Marcus is severed, so nothing is traversed
    assert "Postgres" not in text and "leads" not in text
    assert {e.name for e in atlas_entities} == {"Atlas", "Postgres"}
    assert [e.relation for e in atlas_edges] == ["runs_on"]


def test_resolver_ignores_low_confidence_edges(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Marcus", "person", "")
            b = await db.upsert_entity("Atlas", "project", "")
            await db.upsert_edge(a, b, "leads", origin="model_inferred", confidence=0.1)
            return await ContextResolver(db, SessionLedger()).gather("Marcus")

    _, edges = run(scenario())
    assert edges == []


def test_distill_processes_deprecated_edges_with_alias_mapping(tmp_path, caplog):
    async def extractor(query, response):
        return {
            "entities": [],
            "edges": [],
            "deprecated_edges": [
                {"source": "Nic", "target": "Beemer", "relation": "drives", "reason": "I sold it"},
                {"source": "Nic", "target": "Ghost", "relation": "owns"},
                {"source": "", "target": "x", "relation": "owns"},
            ],
            "summary": "Sold the car.",
        }

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Nic", "person", "")
            b = await db.upsert_entity("BMW 540i", "vehicle", "", {"aliases": ["Beemer"]})
            await db.upsert_edge(a, b, "drives")
            with caplog.at_level(logging.INFO):
                episode = await DistillationEngine(db, extractor).process_exchange(
                    "s", "I sold my Beemer last week", "Sorry to hear that, noted."
                )
            return episode, await _edge(db, a, b, "drives")

    episode, row = run(scenario())
    assert episode is not None
    assert row["weight"] == 0.0 and json.loads(row["metadata"])["deprecation_reason"] == "I sold it"
    assert any("Deprecated edge" in r.getMessage() for r in caplog.records)


def test_reasserting_a_deprecated_edge_revives_it(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Nic", "person", "")
            b = await db.upsert_entity("Subaru", "vehicle", "")
            await db.upsert_edge(a, b, "owns")
            await db.deprecate_edge("Nic", "Subaru", "owns")
            await db.upsert_edge(a, b, "owns")
            return await _edge(db, a, b, "owns")

    row = run(scenario())
    assert row["weight"] == 1.0 and row["confidence"] == 1.0
    assert "deprecated" not in json.loads(row["metadata"])


def test_extractor_schema_and_parsing_include_deprecated_edges():
    assert "deprecated_edges" in SYSTEM_PROMPT
    parsed = parse_extraction(
        json.dumps(
            {
                "entities": [],
                "edges": [],
                "deprecated_edges": [
                    {"source": "Nic", "target": "Acme", "relation": "works_at", "reason": "quit"},
                    {"source": "Nic", "target": "", "relation": "works_at"},
                    "junk",
                ],
            }
        )
    )
    assert parsed["deprecated_edges"] == [
        {"source": "Nic", "target": "Acme", "relation": "works_at", "reason": "quit"}
    ]
    assert parse_extraction("not json")["deprecated_edges"] == []


def test_heuristic_extract_detects_negation_and_defaults_to_empty():
    sold = heuristic_extract("I sold my Subaru yesterday.", "Noted, that is a big change.")
    assert sold["deprecated_edges"][0]["target"] == "Subaru"
    assert sold["deprecated_edges"][0]["relation"] == "owns"
    stopped = heuristic_extract("I no longer use Postgres at all.", "Understood, thanks for sharing.")
    assert [(e["target"], e["relation"]) for e in stopped["deprecated_edges"]] == [("Postgres", "uses")]
    assert heuristic_extract("Marcus manages Atlas.", "Ok, noted that fact.")["deprecated_edges"] == []
