import asyncio

import aiosqlite

from cortext.db import DatabaseManager
from cortext.distill import DistillationEngine, heuristic_extract
from cortext.resolver import ContextResolver


def run(coro):
    return asyncio.run(coro)


async def _one(db, sql, params=()):
    async with db.conn.execute(sql, params) as cur:
        return tuple(await cur.fetchone())


async def _all(db, sql):
    async with db.conn.execute(sql) as cur:
        return [tuple(r) for r in await cur.fetchall()]


def test_defaults_on_fresh_entity_and_edge(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Nic", "person", "")
            b = await db.upsert_entity("Atlas", "project", "")
            await db.upsert_edge(a, b, "works_on")
            return (
                await _one(db, "SELECT origin, confidence FROM entities WHERE id = ?", (a,)),
                await _one(db, "SELECT origin, confidence FROM edges"),
            )

    assert run(scenario()) == (("user_explicit", 1.0), ("user_explicit", 1.0))


def test_user_explicit_entity_not_downgraded_or_overwritten(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            eid = await db.upsert_entity("Car", "vehicle", "BMW 540i", {"k": 1})
            again = await db.upsert_entity(
                "Car", "concept", "Toyota", {"k": 2}, origin="model_inferred", confidence=0.6
            )
            row = await _one(db, "SELECT archetype, summary, metadata, origin, confidence FROM entities")
            return eid, again, row

    eid, again, row = run(scenario())
    assert eid == again
    assert row == ("vehicle", "BMW 540i", '{"k": 1}', "user_explicit", 1.0)


def test_higher_confidence_takes_precedence_and_equal_user_is_kept(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("X", "concept", "guess", origin="model_inferred", confidence=0.5)
            await db.upsert_entity("X", "concept", "better", origin="model_inferred", confidence=0.7)
            await db.upsert_entity("X", "concept", "worse", origin="model_inferred", confidence=0.6)
            mid = await _one(db, "SELECT summary, confidence FROM entities")
            await db.upsert_entity("X", "concept", "truth")
            await db.upsert_entity("X", "concept", "model", origin="model_inferred", confidence=1.0)
            return mid, await _one(db, "SELECT summary, origin, confidence FROM entities")

    mid, final = run(scenario())
    assert mid == ("better", 0.7)
    assert final == ("truth", "user_explicit", 1.0)


def test_edge_precedence_and_subgraph_fields_and_order(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("A", "concept", "")
            b = await db.upsert_entity("B", "concept", "", origin="model_inferred", confidence=0.6)
            c = await db.upsert_entity("C", "concept", "")
            await db.upsert_edge(a, b, "likes", weight=5.0, origin="model_inferred", confidence=0.6)
            await db.upsert_edge(a, c, "owns", weight=1.0)
            await db.upsert_edge(a, c, "owns", weight=9.0, origin="model_inferred", confidence=0.3)
            return await db.get_entity_subgraph(a)

    rows = run(scenario())
    assert [r["relation"] for r in rows] == ["owns", "likes"]  # confidence beats weight
    owns, likes = rows
    assert (owns["origin"], owns["confidence"], owns["weight"]) == ("user_explicit", 1.0, 1.0)
    assert (likes["origin"], likes["confidence"]) == ("model_inferred", 0.6)
    assert (likes["target_origin"], likes["target_confidence"]) == ("model_inferred", 0.6)
    assert (likes["source_origin"], likes["source_confidence"]) == ("user_explicit", 1.0)


def test_confidence_out_of_range_rejected(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            results = []
            for bad in (-0.1, 1.5):
                try:
                    await db.upsert_entity("X", "concept", "", confidence=bad)
                    results.append(False)
                except ValueError:
                    results.append(True)
            return results

    assert run(scenario()) == [True, True]


def test_legacy_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"

    async def scenario():
        async with aiosqlite.connect(str(path)) as raw:
            await raw.executescript(
                "CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,"
                " archetype TEXT NOT NULL, summary TEXT DEFAULT '', metadata JSON DEFAULT '{}',"
                " created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);"
                "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,"
                " target_id INTEGER NOT NULL, relation TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,"
                " metadata JSON DEFAULT '{}', UNIQUE(source_id, target_id, relation));"
                "INSERT INTO entities (name, archetype) VALUES ('Old', 'concept');"
            )
            await raw.commit()
        async with DatabaseManager(path) as db:
            return await _one(db, "SELECT origin, confidence FROM entities")

    assert run(scenario()) == ("user_explicit", 1.0)


def test_heuristic_extract_attributes_user_vs_assistant():
    result = heuristic_extract(
        "Remember that Marcus manages Atlas. Tell me about Zephyr.",
        "Dana leads Orion. Marcus is the CTO.",
    )
    ents = {e["name"]: e for e in result["entities"]}
    assert (ents["Marcus"]["origin"], ents["Marcus"]["confidence"]) == ("user_explicit", 1.0)
    assert (ents["Atlas"]["origin"], ents["Atlas"]["confidence"]) == ("user_explicit", 1.0)
    assert (ents["Dana"]["origin"], ents["Dana"]["confidence"]) == ("model_inferred", 0.6)
    edges = {(e["source"], e["target"]): e for e in result["edges"]}
    assert edges[("Marcus", "Atlas")]["origin"] == "user_explicit"
    assert edges[("Dana", "Orion")]["origin"] == "model_inferred"
    assert edges[("Dana", "Orion")]["confidence"] == 0.6


def test_process_exchange_persists_provenance_and_protects_user_facts(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            engine = DistillationEngine(db)
            await engine.process_exchange("s", "Remember that Marcus manages Atlas.", "Noted. Dana leads Orion.")
            # A later assistant reply repeats/contradicts the user's fact at lower confidence.
            await engine.process_exchange(
                "s", "What is going on?", "Marcus is a junior intern. Marcus manages Atlas. Dana leads Orion."
            )
            ents = {r[0]: r[1:] for r in await _all(db, "SELECT name, origin, confidence, summary FROM entities")}
            edges = await _all(
                db,
                "SELECT s.name, t.name, e.origin, e.confidence FROM edges e "
                "JOIN entities s ON s.id = e.source_id JOIN entities t ON t.id = e.target_id",
            )
            return ents, edges

    ents, edges = run(scenario())
    assert ents["Marcus"] == ("user_explicit", 1.0, "")
    assert ents["Atlas"][:2] == ("user_explicit", 1.0)
    assert ents["Dana"][:2] == ("model_inferred", 0.6)
    assert ("Marcus", "Atlas", "user_explicit", 1.0) in edges
    assert ("Dana", "Orion", "model_inferred", 0.6) in edges


def test_resolver_marks_inferred_facts_only(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            m = await db.upsert_entity("Marcus", "person", "Engineer")
            a = await db.upsert_entity("Atlas", "project", "Billing")
            z = await db.upsert_entity("Zed", "person", "Maybe a PM", origin="model_inferred", confidence=0.6)
            await db.upsert_edge(m, a, "manages")
            await db.upsert_edge(m, z, "mentors", origin="model_inferred", confidence=0.6)
            return await ContextResolver(db).resolve("Marcus", "s1")

    lines = run(scenario()).splitlines()
    assert "- Marcus (person): Engineer" in lines
    assert "- Atlas (project): Billing" in lines
    assert "- Zed (person): Maybe a PM [inferred, confidence: 0.60]" in lines
    assert "- Marcus -manages-> Atlas" in lines
    assert "- Marcus -mentors-> Zed [inferred, confidence: 0.60]" in lines
