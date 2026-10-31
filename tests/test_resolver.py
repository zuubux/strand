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
