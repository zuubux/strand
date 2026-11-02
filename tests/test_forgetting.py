import asyncio
import json
import logging

from strand.db import EMBEDDING_DIM, DatabaseManager
from strand.distill import DistillationEngine, heuristic_extract
from strand.extractors import SYSTEM_PROMPT, parse_extraction
from strand.resolver import ContextResolver
from strand.session import SessionLedger


def run(coro):
    return asyncio.run(coro)


def unit(axis: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[axis] = 1.0
    return vec


def _extractor(**payload):
    def extractor(query, response):
        return {"entities": [], "edges": [], "summary": "", **payload}

    return extractor


async def _names(db):
    async with db.conn.execute("SELECT name FROM entities ORDER BY name") as cur:
        return [r[0] for r in await cur.fetchall()]


async def _count(db, table):
    async with db.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        return (await cur.fetchone())[0]


QUERY = "Please forget Project Titan, it was cancelled"
REPLY = "Understood, I will not keep that around."


def test_parse_extraction_reads_forgotten_entities():
    result = parse_extraction(json.dumps({"forgotten_entities": ["Titan", " ", 5, "Titan", "Old Address"]}))
    assert result["forgotten_entities"] == ["Titan", "Old Address"]
    assert "forgotten_entities" in SYSTEM_PROMPT


def test_parse_extraction_defaults_forgotten_entities_to_empty():
    assert parse_extraction(json.dumps({"entities": []}))["forgotten_entities"] == []
    assert parse_extraction(json.dumps({"forgotten_entities": "Titan"}))["forgotten_entities"] == []
    assert parse_extraction("not json")["forgotten_entities"] == []


def test_heuristic_detects_forget_commands():
    for query, expected in [
        ("Forget Project Titan please", ["Project Titan"]),
        ("Please delete entity Atlas now", ["Atlas"]),
        ("Delete the entity for Project Titan", ["Project Titan"]),
        ("erase Marcus from memory", ["Marcus"]),
        ("scrub Postgres", ["Postgres"]),
    ]:
        assert heuristic_extract(query, "Okay, done.")["forgotten_entities"] == expected, query


def test_heuristic_ignores_remember_requests_and_does_not_extract_forgotten_names():
    assert heuristic_extract("Don't forget Atlas is a billing project", "Noted.")["forgotten_entities"] == []
    extraction = heuristic_extract("Forget Project Titan", "Sure, forgotten.")
    assert extraction["forgotten_entities"] == ["Project Titan"]
    assert all("Titan" not in e["name"] for e in extraction["entities"])


def test_forget_by_exact_name_cascades_edges_and_vectors(tmp_path, caplog):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            titan = await db.upsert_entity("Project Titan", "project", "Cancelled", embedding=unit(1))
            marcus = await db.upsert_entity("Marcus", "person", "", embedding=unit(2))
            await db.upsert_edge(marcus, titan, "leads")
            engine = DistillationEngine(db, _extractor(forgotten_entities=["project titan"]))
            with caplog.at_level(logging.DEBUG):
                await engine.process_exchange("s", QUERY, REPLY)
            similar = await db.find_similar_entities(unit(1), limit=3, distance_threshold=0.1)
            return await _names(db), await _count(db, "edges"), await _count(db, "entity_embeddings"), similar

    names, edges, vectors, similar = run(scenario())
    assert names == ["Marcus"]
    assert edges == 0
    assert vectors == 1
    assert similar == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("User-directed forgetting: dropped entity 'Project Titan' (id=1)" in m for m in messages)
    assert any("forgotten=1" in m for m in messages)


def test_forget_by_alias(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Home Address", "concept", "", {"aliases": ["old address"]})
            await db.upsert_entity("Marcus", "person", "")
            await DistillationEngine(db, _extractor(forgotten_entities=["Old Address"])).process_exchange(
                "s", "Forget my old address", REPLY
            )
            return await _names(db)

    assert run(scenario()) == ["Marcus"]


def test_forget_discards_same_extraction_upserts_and_edges(tmp_path):
    extractor = _extractor(
        entities=[
            {"name": "Titan", "origin": "user_explicit", "confidence": 1.0},
            {"name": "Marcus", "origin": "user_explicit", "confidence": 1.0},
        ],
        edges=[{"source": "Marcus", "target": "Titan", "relation": "leads"}],
        forgotten_entities=["Titan"],
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Titan", "project", "")
            await DistillationEngine(db, extractor).process_exchange("s", QUERY, REPLY)
            return await _names(db), await _count(db, "edges")

    names, edges = run(scenario())
    assert names == ["Marcus"]
    assert edges == 0


def test_forgotten_entity_is_not_resolvable_and_session_ledger_is_evicted(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Titan", "project", "Secret project")
            await db.upsert_entity("Marcus", "person", "Engineer")
            ledger = SessionLedger()
            resolver = ContextResolver(db, ledger)
            before = await resolver.resolve("Tell me about Titan", "s1")
            seen_before = set(ledger._sessions["s1"].seen_entity_ids)
            engine = DistillationEngine(db, _extractor(forgotten_entities=["Titan"]), ledger=ledger)
            await engine.process_exchange("s1", QUERY, REPLY)
            seen_after = set(ledger._sessions["s1"].seen_entity_ids)
            after = await resolver.resolve("Tell me about Titan", "s1")
            return before, seen_before, seen_after, after

    before, seen_before, seen_after, after = run(scenario())
    assert "Titan" in before
    assert seen_before and not seen_after
    assert "Titan" not in after


def test_ledger_evict_entity():
    ledger = SessionLedger()
    ledger.filter_new("a", [1, 2], [7])
    ledger.filter_new("b", [1], [])
    assert ledger.evict_entity(1) == 2
    assert ledger.evict_entity(99) == 0
    assert ledger.filter_new("a", [1, 2], [7]).entity_ids == (1,)


def test_forgetting_unknown_entity_is_a_safe_noop(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Marcus", "person", "")
            engine = DistillationEngine(db, _extractor(forgotten_entities=["Ghost", "", 5]))
            episode = await engine.process_exchange("s", "Forget Ghost entirely", REPLY)
            return episode, await _names(db), await db.drop_entity_by_name("Ghost"), await db.drop_entity(999)

    episode, names, by_name, by_id = run(scenario())
    assert episode is not None
    assert names == ["Marcus"]
    assert by_name is False and by_id is False


def test_db_drop_entity_by_name_and_ambiguous_alias(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("A", "concept", "", {"aliases": ["shared"]})
            await db.upsert_entity("B", "concept", "", {"aliases": ["shared", "bee"]})
            ambiguous = await db.drop_entity_by_name("shared")
            alias = await db.drop_entity_by_name("BEE")
            return ambiguous, alias, await _names(db)

    assert run(scenario()) == (False, True, ["A"])



def test_drop_entity_scrubs_episode_residue(tmp_path):
    """SEC-MEM-009: ids and name mentions vanish from episodes so compaction cannot resurrect them."""

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            titan = await db.upsert_entity("Titan", "project", "Secret", metadata={"aliases": ["Proj T"]})
            marcus = await db.upsert_entity("Marcus", "person", "Engineer")
            only = await db.record_episode("s", "Discussed Titan launch", [titan])
            shared = await db.record_episode("s", "Marcus pitched titan and proj t plans", [marcus, titan])
            prose = await db.record_episode("s", "Later TITAN came up again", [])
            keep = await db.record_episode("s", "Marcus fixed a bug", [marcus])
            assert await db.drop_entity(titan)
            async with db.conn.execute("SELECT id, narrative, extracted_entities FROM episodes ORDER BY id") as cur:
                rows = {r[0]: (r[1], json.loads(r[2])) for r in await cur.fetchall()}
            return rows, only, shared, prose, keep, marcus, titan

    rows, only, shared, prose, keep, marcus, titan = run(scenario())
    assert only not in rows
    assert rows[shared] == ("Marcus pitched [forgotten] and [forgotten] plans", [marcus])
    assert rows[prose] == ("Later [forgotten] came up again", [])
    assert rows[keep] == ("Marcus fixed a bug", [marcus])
    assert all(titan not in ids for _, ids in rows.values())
