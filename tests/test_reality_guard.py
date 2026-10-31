"""COG-1 Proactive Contradiction & Reality Guard (offline)."""

import asyncio

from strand.db import DatabaseManager
from strand.proxy import _delta_log_fields
from strand.resolver import ContextResolver

CHECK = "[Reality Check - Attribute Discrepancy]"


def run(coro):
    return asyncio.run(coro)


async def _garage(db, relation, target, summary=""):
    car = await db.upsert_entity("E34", "vehicle", "Project car")
    part = await db.upsert_entity(target, "concept", summary)
    await db.upsert_edge(car, part, relation)
    return car


def resolve(tmp_path, relation, target, query, persona="", personality=False, summary=""):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _garage(db, relation, target, summary)
            if personality:
                user = await db.upsert_entity("User", "person", "Operator")
                for name, archetype, rel in (
                    ("Dry Wit", "trait", "enforces"),
                    ("no-apologies", "directive", "demands"),
                    ("unified-diffs", "preference", "prefers"),
                ):
                    node = await db.upsert_entity(name, archetype, f"{name} rule")
                    await db.upsert_edge(user, node, rel)
            return await ContextResolver(db, persona_prompt=persona).resolve(query, "s1")

    return run(scenario())


def test_transmission_contradiction_triggers_check(tmp_path):
    delta = resolve(tmp_path, "has_transmission", "6-speed Manual", "My E34 has a 5-speed gearbox, right?")
    assert CHECK in delta
    assert "- Discrepancy on E34:" in delta
    assert "  * Stored fact: [has_transmission] -> 6-speed Manual" in delta
    assert "  * Query phrasing: mentions '5-speed'" in delta
    assert "Clarify or challenge this discrepancy rather than assuming the contradictory spec." in delta


def test_manual_vs_automatic_contradiction(tmp_path):
    delta = resolve(tmp_path, "has_transmission", "ZF Manual", "Service the automatic transmission on the E34")
    assert CHECK in delta and "mentions 'automatic'" in delta


def test_engine_cylinder_contradiction(tmp_path):
    delta = resolve(tmp_path, "has_engine", "M60 V8", "Tune the V6 in my E34")
    assert CHECK in delta
    assert "[has_engine] -> M60 V8" in delta
    assert "mentions 'V6'" in delta


def test_fuel_and_os_contradictions(tmp_path):
    assert CHECK in resolve(tmp_path, "has_engine", "Diesel M57", "Which petrol does the E34 take?")
    assert CHECK in resolve(tmp_path / "b", "runs", "Fedora 41", "Does the E34 run Ubuntu?")
    assert CHECK in resolve(tmp_path / "c", "has_arch", "Intel", "Is the E34 on arm?")


def test_non_conflicting_queries_do_not_trigger(tmp_path):
    for i, query in enumerate(
        [
            "Which manual transmission fluid does the E34 need?",
            "E34 oil change interval",
            "The E34 6-speed is great",
            "E34 needs a V8 rebuild",
            "E34 rev8 tuning",
            "E34 auto repair shop",
        ]
    ):
        delta = resolve(tmp_path / str(i), "has_transmission", "6-speed Manual", query)
        if "V8" in query or "rev8" in query:
            delta = resolve(tmp_path / f"e{i}", "has_engine", "M60 V8", query)
        assert CHECK not in delta, query


def test_non_attribute_relation_is_ignored(tmp_path):
    assert CHECK not in resolve(tmp_path, "friend_of", "6-speed Manual", "My E34 has a 5-speed")


def test_summary_tokens_count_as_stored_fact(tmp_path):
    delta = resolve(tmp_path, "has_engine", "M60", "Tune the V6 in my E34", summary="3.0 litre V8 engine")
    assert CHECK in delta


def test_format_keeps_personality_sections_and_places_check_first(tmp_path):
    delta = resolve(
        tmp_path,
        "has_engine",
        "M60 V8",
        "Tune the V6 in my E34",
        persona="Calm peer.",
        personality=True,
    )
    order = [
        delta.index(m)
        for m in (
            "## Strand Memory",
            CHECK,
            "[Persona Stance]",
            "[Operational Directives]",
            "[Interaction Preferences]",
            "[Working Memory]",
        )
    ]
    assert order == sorted(order)
    assert "Calm peer." not in delta  # factory persona superseded by graph stance nodes
    assert "- Dry Wit: Dry Wit rule" in delta
    assert "- E34 -has_engine-> M60 V8" in delta
    assert _delta_log_fields(delta) == "entities=2 edges=1 traits=1 directives=1 preferences=1 discrepancies=1"


def test_no_discrepancy_omits_section_and_log_field(tmp_path):
    delta = resolve(tmp_path, "has_engine", "M60 V8", "Tune the V8 in my E34")
    assert CHECK not in delta
    assert "discrepancies" not in _delta_log_fields(delta)


def test_check_repeats_on_later_turns_without_touching_ledger(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _garage(db, "has_engine", "M60 V8")
            resolver = ContextResolver(db)
            first = await resolver.resolve("Tune the V8 in my E34", "s")
            second = await resolver.resolve("Tune the V8 in my E34", "s")
            third = await resolver.resolve("Actually the E34 has a V6", "s")
            return first, second, third

    first, second, third = run(scenario())
    assert first and second == ""
    assert CHECK in third and "Entities:" not in third
    assert _delta_log_fields(third) == "entities=0 edges=0 discrepancies=1"
