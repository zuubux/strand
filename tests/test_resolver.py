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
    from strand import prompts

    assert BEDROCK_INVARIANT is prompts.BEDROCK_INVARIANT
    assert BEDROCK_INVARIANT.startswith("You are Strand, a local peer and technical collaborator")
    assert BEDROCK_INVARIANT.endswith("state the result plainly and stop.")


def test_cold_boot_injects_bedrock_and_factory_persona(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            return await ContextResolver(db, persona_prompt=FACTORY).resolve("hello", "s")

    out = run(scenario())
    assert BEDROCK_INVARIANT in out and FACTORY in out
    assert out.index(BEDROCK_INVARIANT) < out.index(FACTORY)


def test_dynamic_stance_keeps_bedrock_and_retains_factory_persona(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed_stance(db)
            return await ContextResolver(db, persona_prompt=FACTORY).resolve("hello", "s")

    out = run(scenario())
    assert BEDROCK_INVARIANT in out
    assert f"[Persona Stance]\n{FACTORY}\n- Dry Wit: Dry wit, unhurried" in out
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
    alice = await db.upsert_entity("Alice", "person", "Operator", origin="user_explicit")
    sedan = await db.upsert_entity("W34", "vehicle", "Acme Sedan")
    cruiser = await db.upsert_entity("Cruiser", "vehicle", "Kestrel")
    sold = await db.upsert_entity("Miata", "vehicle", "Mazda")
    other = await db.upsert_entity("Bob", "person", "Colleague")
    stranger = await db.upsert_entity("Corvette", "vehicle", "Bob's car")
    host = await db.upsert_entity("workstation-01", "system", "Workstation")
    await db.upsert_edge(alice, host, "operates_host")
    await db.upsert_edge(alice, sedan, "previously_owned")
    await db.upsert_edge(alice, cruiser, "owns")
    await db.upsert_edge(alice, sold, "owns")
    await db.upsert_edge(other, stranger, "owns")
    await db.deprecate_edge("Alice", "Miata", "owns", reason="wrong")


def test_car_history_query_matches_operator_vehicles(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed_garage(db)
            resolver = ContextResolver(db)
            return [e.name for e in await resolver.match_entities("Do you recall my car history?")]

    names = run(scenario())
    assert set(names) == {"W34", "Cruiser"}


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
    assert set(plural) == {"W34", "Cruiser"}


def test_collection_traversal_reaches_vehicles_in_gather(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed_garage(db)
            entities, edges = await ContextResolver(db).gather("list my cars")
            return {e.name for e in entities}, {e.relation for e in edges}

    names, relations = run(scenario())
    assert {"W34", "Cruiser", "Alice"} <= names
    assert {"owns", "previously_owned"} <= relations



# --- PERF-RES-001: in-process entity cache keyed by db.write_version -------------------------


def test_unchanged_database_hits_the_cache(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Atlas", "project", "Main", {"aliases": ["Titan"]})
            resolver = ContextResolver(db)
            first = await resolver.match_entities("tell me about Atlas")
            loads = resolver.cache_loads
            second = await resolver.match_entities("and what about titan?")
            third = await resolver.match_entities("Atlas again")
            return first, second, third, loads, resolver.cache_loads, resolver.cache_hits

    first, second, third, loads, loads_after, hits = run(scenario())
    assert [e.name for e in first] == [e.name for e in second] == [e.name for e in third] == ["Atlas"]
    assert loads == 1 and loads_after == 1  # the table is scanned once
    assert hits == 2


def test_cache_hit_issues_no_entity_table_scan(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Atlas", "project", "Main")
            resolver = ContextResolver(db)
            await resolver.match_entities("Atlas")
            statements: list[str] = []
            await db.conn.set_trace_callback(statements.append)
            await resolver.match_entities("Atlas")
            await resolver._private_ids()
            await db.conn.set_trace_callback(None)
            return statements

    statements = run(scenario())
    assert not [s for s in statements if "FROM entities" in s]


def test_writes_increment_write_version_and_invalidate_cache(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            resolver = ContextResolver(db)
            seen = [db.write_version]
            atlas = await db.upsert_entity("Atlas", "project", "Main")
            seen.append(db.write_version)
            assert [e.name for e in await resolver.match_entities("Atlas")] == ["Atlas"]
            other = await db.upsert_entity("Borealis", "project", "Second")
            seen.append(db.write_version)
            assert [e.name for e in await resolver.match_entities("Borealis")] == ["Borealis"]
            await db.upsert_edge(atlas, other, "relates_to")
            seen.append(db.write_version)
            assert await db.update_entity_metadata(atlas, {"aliases": ["Titan"]})
            seen.append(db.write_version)
            assert [e.name for e in await resolver.match_entities("Titan")] == ["Atlas"]
            assert await db.drop_entity(other)
            seen.append(db.write_version)
            assert await resolver.match_entities("Borealis") == []
            return seen, resolver.cache_loads

    seen, loads = run(scenario())
    assert seen == sorted(set(seen)) and len(set(seen)) == len(seen)  # strictly increasing
    assert loads == 4  # every mutation forced exactly one refresh


def test_deprecate_edge_increments_write_version(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Atlas", "project", "Main")
            b = await db.upsert_entity("Borealis", "project", "Second")
            await db.upsert_edge(a, b, "relates_to")
            before = db.write_version
            await db.deprecate_edge(a, b, "relates_to")
            return before, db.write_version

    before, after = run(scenario())
    assert after > before


def test_read_only_bookkeeping_keeps_cache_valid(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Atlas", "project", "Main")
            resolver = ContextResolver(db)
            await resolver.resolve("Atlas", "s1")
            await resolver.resolve("Atlas", "s2")  # touch_access + record_episode must not invalidate
            await db.record_episode("s1", "talked")
            await resolver.match_entities("Atlas")
            return resolver.cache_loads

    assert run(scenario()) == 1


def test_private_entities_stay_hidden_across_cache_hits(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Vault", "private", "Secret plans")
            await db.upsert_entity("Ledger", "project", "Flagged", {"private": True})
            await db.upsert_entity("Garden", "project", "Public")
            resolver = ContextResolver(db)
            query = "Vault Ledger Garden"
            out = []
            for allow in (False, True, False, False):  # shared snapshot, alternating privacy
                out.append([e.name for e in await resolver.match_entities(query, allow)])
            hidden = await resolver._private_ids()
            text = await resolver.resolve(query, "s")
            return out, resolver.cache_loads, hidden, text

    out, loads, hidden, text = run(scenario())
    assert out[0] == out[2] == out[3] == ["Garden"]
    assert set(out[1]) == {"Vault", "Ledger", "Garden"}
    assert len(hidden) == 2
    assert "Vault" not in text and "Ledger" not in text and "Garden" in text
    assert loads == 1


def test_marking_private_or_dropping_takes_effect_immediately(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            atlas = await db.upsert_entity("Atlas", "project", "Main")
            resolver = ContextResolver(db)
            warm = [e.name for e in await resolver.match_entities("Atlas")]  # cached as public
            await db.update_entity_metadata(atlas, {"private": True})
            hidden_now = await resolver.match_entities("Atlas")
            gathered = await resolver.gather("Atlas")
            allowed = [e.name for e in await resolver.match_entities("Atlas", allow_private=True)]
            await db.drop_entity(atlas)
            gone = await resolver.match_entities("Atlas", allow_private=True)
            return warm, hidden_now, gathered, allowed, gone

    warm, hidden_now, gathered, allowed, gone = run(scenario())
    assert warm == ["Atlas"]
    assert hidden_now == [] and gathered == ([], [])
    assert allowed == ["Atlas"]
    assert gone == []


def test_out_of_process_mark_private_invalidates_cache(tmp_path):
    import sqlite3

    async def scenario():
        path = tmp_path / "m.db"
        async with DatabaseManager(path) as db:
            await db.upsert_entity("Atlas", "project", "Main")
            resolver = ContextResolver(db)
            assert [e.name for e in await resolver.match_entities("Atlas")] == ["Atlas"]
            version = db.write_version
            other = sqlite3.connect(path)
            other.execute("UPDATE entities SET metadata = '{\"private\": true}'")
            other.commit()
            other.close()
            assert db.write_version == version  # this process never saw the write
            return await resolver.match_entities("Atlas")

    assert run(scenario()) == []


def test_bedrock_injected_with_empty_graph_and_no_persona(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            resolver = ContextResolver(db)
            return await resolver.resolve("hello", "s"), await resolver.resolve("hello", "s")

    first, second = run(scenario())
    assert first == f"## Strand Memory\n{BEDROCK_INVARIANT}"
    assert second == ""


def test_format_context_delta_includes_bedrock_with_no_personality_entities():
    from strand.resolver import BEDROCK_ID, format_context_delta
    from strand.session import SessionDelta

    out = format_context_delta([], [], SessionDelta([BEDROCK_ID], []))
    assert out == f"## Strand Memory\n{BEDROCK_INVARIANT}"
