"""Invariant Bedrock & dynamic persona precedence (OBS-POL-005 Phase 1, offline)."""

import asyncio

from strand.common import BEDROCK_INVARIANT
from strand.db import DatabaseManager
from strand.resolver import ContextResolver
from strand.session import SessionLedger

FACTORY = "Factory persona: chirpy helper."


def run(coro):
    return asyncio.run(coro)


async def _seed_stance(db):
    user = await db.upsert_entity("User", "person", "The operator")
    trait = await db.upsert_entity("Dry Wit", "trait", "Dry wit, unhurried")
    rule = await db.upsert_entity("no-filler", "directive", "No conversational filler")
    await db.upsert_edge(user, trait, "enforces")
    await db.upsert_edge(user, rule, "demands")
    return trait, rule


def test_bedrock_text_is_the_permanent_identity():
    assert BEDROCK_INVARIANT.startswith("You are Strand, a sovereign, local-first cognitive companion")
    assert BEDROCK_INVARIANT.endswith("Never use conversational filler.")


def test_cold_boot_injects_bedrock_and_factory_persona(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            return await ContextResolver(db, persona_prompt=FACTORY).resolve("hello", "s")

    out = run(scenario())
    assert BEDROCK_INVARIANT in out and FACTORY in out
    assert out.index(BEDROCK_INVARIANT) < out.index(FACTORY)


def test_dynamic_stance_keeps_bedrock_and_suppresses_factory_persona(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed_stance(db)
            return await ContextResolver(db, persona_prompt=FACTORY).resolve("hello", "s")

    out = run(scenario())
    assert BEDROCK_INVARIANT in out
    assert FACTORY not in out
    assert "[Persona Stance]\n- Dry Wit: Dry wit, unhurried" in out
    assert "[Operational Directives]\n- no-filler: No conversational filler" in out


def test_no_operator_directives_falls_back_to_factory_persona(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Atlas", "project", "Main")
            return await ContextResolver(db, persona_prompt=FACTORY).resolve("Atlas", "s")

    out = run(scenario())
    assert BEDROCK_INVARIANT in out and FACTORY in out


def test_superseded_directives_do_not_suppress_factory_persona(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            trait, _ = await _seed_stance(db)
            await db.conn.execute("UPDATE edges SET weight = 0, confidence = 0, metadata = '{\"deprecated\": true}'")
            await db.conn.commit()
            return await ContextResolver(db, persona_prompt=FACTORY).resolve("hello", "s")

    out = run(scenario())
    assert FACTORY in out and "Dry Wit" not in out


def test_ledger_marks_stance_seen_and_drops_everything_on_turn_two(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            trait, rule = await _seed_stance(db)
            ledger = SessionLedger()
            resolver = ContextResolver(db, ledger, persona_prompt=FACTORY)
            t1 = await resolver.resolve("hello", "s")
            seen = set(ledger._touch("s").seen_entity_ids)
            t2 = await resolver.resolve("hello again", "s")
            other = await resolver.resolve("hello", "other")
            return trait, rule, t1, seen, t2, other

    trait, rule, t1, seen, t2, other = run(scenario())
    assert {trait, rule} <= seen
    assert BEDROCK_INVARIANT in t1
    assert t2 == ""  # zero token tax on Turn 2+
    assert BEDROCK_INVARIANT in other  # a new session is a new Turn 1


# --- Collection / archetype traversal -----------------------------------------------------------


async def _seed_garage(db):
    nic = await db.upsert_entity("Nic", "person", "Operator", origin="user_explicit")
    e34 = await db.upsert_entity("E34", "vehicle", "BMW 5 Series")
    forester = await db.upsert_entity("Forester", "vehicle", "Subaru")
    sold = await db.upsert_entity("Miata", "vehicle", "Mazda")
    other = await db.upsert_entity("Alice", "person", "Colleague")
    stranger = await db.upsert_entity("Corvette", "vehicle", "Alice's car")
    host = await db.upsert_entity("onyx", "system", "Workstation")
    await db.upsert_edge(nic, host, "operates_host")
    await db.upsert_edge(nic, e34, "previously_owned")
    await db.upsert_edge(nic, forester, "owns")
    await db.upsert_edge(nic, sold, "owns")
    await db.upsert_edge(other, stranger, "owns")
    await db.deprecate_edge("Nic", "Miata", "owns", reason="wrong")


def test_car_history_query_matches_operator_vehicles(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed_garage(db)
            resolver = ContextResolver(db)
            return [e.name for e in await resolver.match_entities("Do you recall my car history?")]

    names = run(scenario())
    assert set(names) == {"E34", "Forester"}


def test_collection_query_requires_self_reference_and_archetype_word(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed_garage(db)
            resolver = ContextResolver(db)
            return [
                [e.name for e in await resolver.match_entities(q)]
                for q in ("What is a car?", "Tell me about my past", "vehicles owned")
            ]

    plain, no_archetype, plural = run(scenario())
    assert plain == [] and no_archetype == []
    assert set(plural) == {"E34", "Forester"}


def test_collection_traversal_reaches_vehicles_in_gather(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed_garage(db)
            entities, edges = await ContextResolver(db).gather("list my cars")
            return {e.name for e in entities}, {e.relation for e in edges}

    names, relations = run(scenario())
    assert {"E34", "Forester", "Nic"} <= names
    assert {"owns", "previously_owned"} <= relations

