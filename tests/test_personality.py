"""Personality Matrix substrate: trait / directive / preference archetypes (offline)."""

import asyncio
import json

import httpx

from strand.cli import main
from strand.config import Config
from strand.db import DatabaseManager
from strand.distill import DistillationEngine
from strand.extractors import SYSTEM_PROMPT, LLMExtractor
from strand.common import BEDROCK_INVARIANT
from strand.resolver import ContextResolver
from strand.stopwords import is_valid_entity_name
from tests.conftest import HOST_TIME_LINE

QUERY = "Stop apologizing, be more terse, and give me unified diffs for every code change."
RESPONSE = "Understood. I will keep replies terse and send every code change as a unified diff."

PERSONALITY = {
    "entities": [
        {"name": "User", "archetype": "person", "summary": "The operator"},
        {"name": "Dry Wit", "archetype": "trait", "summary": "Sovereign systems peer, dry wit, unhurried, calm"},
        {"name": "no-apologies", "archetype": "directive", "summary": "Never apologize or use conversational filler"},
        {"name": "unified-diffs", "archetype": "preference", "summary": "Provide code updates as unified git diffs"},
    ],
    "edges": [
        {"source": "User", "target": "Dry Wit", "relation": "enforces"},
        {"source": "User", "target": "no-apologies", "relation": "demands"},
        {"source": "User", "target": "unified-diffs", "relation": "prefers"},
    ],
    "deprecated_edges": [],
}


def run(coro):
    return asyncio.run(coro)


def extractor_for(payload):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMExtractor("http://slm.test/v1", "tiny", client=client), seen


async def _rows(db, sql):
    async with db.conn.execute(sql) as cur:
        return [tuple(r) for r in await cur.fetchall()]


# --- validation -----------------------------------------------------------------------------


def test_personality_names_accept_descriptive_and_hyphenated_identifiers():
    for archetype in ("trait", "directive", "preference"):
        assert is_valid_entity_name("no-apologies", archetype)
        assert is_valid_entity_name("Never explain basic CLI flags", archetype)
        assert is_valid_entity_name("Nm", archetype)
        assert not is_valid_entity_name("", archetype)
        assert not is_valid_entity_name("---", archetype)
    assert not is_valid_entity_name("Nm")  # unchanged for the default archetype


def test_config_persona_prompt_env_and_file():
    assert Config().persona_prompt == ""
    assert Config.from_env({}, {"persona_prompt": " Calm peer. "}).persona_prompt == "Calm peer."
    env = {"STRAND_PERSONA_PROMPT": "Env wins"}
    assert Config.from_env(env, {"persona_prompt": "file"}).persona_prompt == "Env wins"


# --- extractor ------------------------------------------------------------------------------


def test_extractor_prompt_documents_personality_archetypes():
    for token in ("prefers", "demands", "enforces", "deprecated_edges", "trait|directive|preference"):
        assert token in SYSTEM_PROMPT


def test_extractor_classifies_and_engine_persists_personality(tmp_path):
    extractor, seen = extractor_for(PERSONALITY)
    result = run(extractor(QUERY, RESPONSE))
    assert {e["name"]: e["archetype"] for e in result["entities"]} == {
        "User": "person",
        "Dry Wit": "trait",
        "no-apologies": "directive",
        "unified-diffs": "preference",
    }
    assert [e["relation"] for e in result["edges"]] == ["enforces", "demands", "prefers"]
    assert seen["body"]["messages"][0]["content"] == SYSTEM_PROMPT

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db, extractor=extractor).process_exchange("s", QUERY, RESPONSE)
            return await _rows(db, "SELECT name, archetype FROM entities ORDER BY name")

    assert run(scenario()) == [
        ("Dry Wit", "trait"),
        ("User", "person"),
        ("no-apologies", "directive"),
        ("unified-diffs", "preference"),
    ]


def test_contradicted_preference_is_deprecated_and_dropped_from_context(tmp_path):
    update = {
        "entities": [{"name": "full-files", "archetype": "preference", "summary": "Send complete files, not diffs"}],
        "edges": [{"source": "User", "target": "full-files", "relation": "prefers"}],
        "deprecated_edges": [
            {"source": "User", "target": "unified-diffs", "relation": "prefers", "reason": "user now wants full files"}
        ],
    }
    first, _ = extractor_for(PERSONALITY)
    second, _ = extractor_for(update)

    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await DistillationEngine(db, extractor=first).process_exchange("s", QUERY, RESPONSE)
            await DistillationEngine(db, extractor=second).process_exchange(
                "s", "Actually, stop using diffs; send me the full files from now on.", RESPONSE
            )
            edge = await _rows(
                db,
                "SELECT weight, confidence, json_extract(metadata, '$.deprecated') FROM edges "
                "WHERE relation = 'prefers' AND target_id = (SELECT id FROM entities WHERE name = 'unified-diffs')",
            )
            delta = await ContextResolver(db).resolve("hello there", "s2")
            return edge, delta

    edge, delta = run(scenario())
    assert edge == [(0.0, 0.0, 1)]
    assert "full-files: Send complete files, not diffs" in delta
    assert "unified-diffs" not in delta
    assert "no-apologies" in delta  # untouched rules stay active


# --- resolver formatting & ledger -----------------------------------------------------------


async def _seed(db):
    user = await db.upsert_entity("User", "person", "The operator")
    trait = await db.upsert_entity("Dry Wit", "trait", "Sovereign systems peer, dry wit, calm")
    rule = await db.upsert_entity("no-apologies", "directive", "Never apologize or use filler")
    pref = await db.upsert_entity("Torque in Nm", "preference", "Torque specs in Nm")
    car = await db.upsert_entity("Acme X540", "vehicle", "W34 sedan")
    await db.upsert_edge(user, trait, "enforces")
    await db.upsert_edge(user, rule, "demands")
    await db.upsert_edge(user, pref, "prefers")
    await db.upsert_edge(user, car, "drives")


def test_hierarchical_formatting_and_session_dedup(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await _seed(db)
            resolver = ContextResolver(db)
            turn1 = await resolver.resolve("Tell me about my Acme X540", "s1")
            turn2 = await resolver.resolve("Tell me about my Acme X540", "s1")
            turn3 = await resolver.resolve("Anything else?", "s1")
            other = await resolver.resolve("Anything else?", "s2")
            return turn1, turn2, turn3, other

    turn1, turn2, turn3, other = run(scenario())
    order = ["[Persona Stance]", "[Operational Directives]", "[Interaction Preferences]", "[Working Memory]"]
    positions = [turn1.index(t) for t in order]
    assert positions == sorted(positions)
    assert turn1.startswith(f"## Strand Memory\n{BEDROCK_INVARIANT}\n[Persona Stance]\n- Dry Wit: Sovereign systems peer, dry wit, calm")
    assert "[Operational Directives]\n- no-apologies: Never apologize or use filler" in turn1
    assert "[Interaction Preferences]\n- Torque in Nm: Torque specs in Nm" in turn1
    working = turn1.split("[Working Memory]")[1]
    assert "- Acme X540 (vehicle): W34 sedan" in working
    assert "- User -drives-> Acme X540" in working
    assert "Dry Wit" not in working and "demands" not in turn1
    # Turn 2+: the personality delta is empty, and so is everything else (SessionLedger dedup).
    assert turn2 == "" and turn3 == ""
    # A different session gets the personality nodes once, with no working-memory section.
    assert other.startswith(f"## Strand Memory\n{BEDROCK_INVARIANT}\n[Persona Stance]")
    assert "[Working Memory]" not in other



def test_empty_sections_are_omitted_and_plain_output_unchanged(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("no-filler", "directive", "No conversational filler")
            only_rule = await ContextResolver(db).resolve("hi", "a")
        async with DatabaseManager(tmp_path / "n.db") as db:
            a = await db.upsert_entity("Alice", "person", "Lead")
            b = await db.upsert_entity("Atlas", "project", "Main")
            await db.upsert_edge(a, b, "works_on")
            plain = await ContextResolver(db).resolve("Alice", "a")
        return only_rule, plain

    only_rule, plain = run(scenario())
    assert only_rule == f"## Strand Memory\n{BEDROCK_INVARIANT}\n[Operational Directives]\n- no-filler: No conversational filler\n{HOST_TIME_LINE}"
    assert "[Persona Stance]" not in only_rule and "[Interaction Preferences]" not in only_rule
    assert plain.startswith(f"## Strand Memory\n{BEDROCK_INVARIANT}\nEntities:") and "[Working Memory]" not in plain


def test_persona_prompt_anchor_injected_once_per_session(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            resolver = ContextResolver(db, persona_prompt="  Calm,\n unhurried peer.  ")
            first = await resolver.resolve("hello", "s")
            second = await resolver.resolve("hello", "s")
            return first, second

    first, second = run(scenario())
    assert first == f"## Strand Memory\n{BEDROCK_INVARIANT}\n[Persona Stance]\nCalm, unhurried peer.\n{HOST_TIME_LINE}"
    assert second == ""


def test_private_personality_nodes_hidden_unless_allowed(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("secret-rule", "directive", "Hidden", {"private": True})
            resolver = ContextResolver(db)
            hidden = await resolver.resolve("hi", "a")
            shown = await resolver.resolve("hi", "b", allow_private=True)
            return hidden, shown

    hidden, shown = run(scenario())
    assert hidden == f"## Strand Memory\n{BEDROCK_INVARIANT}\n{HOST_TIME_LINE}"
    assert "secret-rule" in shown


def test_cli_list_filters_each_personality_archetype(tmp_path, capsys):
    path = tmp_path / "cli.db"

    async def seed():
        async with DatabaseManager(path) as db:
            await _seed(db)

    run(seed())
    expected = {"trait": "Dry Wit", "directive": "no-apologies", "preference": "Torque in Nm"}
    for archetype, name in expected.items():
        assert main(["--db", str(path), "list", "--archetype", archetype]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2 and name in lines[1] and archetype in lines[1]
