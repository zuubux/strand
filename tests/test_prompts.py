import asyncio

import pytest

from strand.common import BEDROCK_INVARIANT
from strand.config import Config, save_config_value
from strand.db import DatabaseManager
from strand.prompts import PERSONA_STANCES, get_persona_stance
from strand.resolver import ContextResolver


def run(coro):
    return asyncio.run(coro)


def test_stances_defined():
    assert set(PERSONA_STANCES) == {"peer_workbench", "mentor_thoughtful", "operator_terse"}
    assert len(set(PERSONA_STANCES.values())) == 3


@pytest.mark.parametrize("name", sorted(PERSONA_STANCES))
def test_get_persona_stance_valid(name):
    text = get_persona_stance(name)
    assert text == PERSONA_STANCES[name] and text.strip()


@pytest.mark.parametrize("name", [None, "", "nope", 42])
def test_get_persona_stance_falls_back(name):
    assert get_persona_stance(name) == PERSONA_STANCES["peer_workbench"]


def test_config_persona_stance_resolution():
    assert Config().persona_stance == "peer_workbench"
    assert Config.from_env({}, {}).persona_stance == "peer_workbench"
    assert Config.from_env({}, {"persona_stance": "operator_terse"}).persona_stance == "operator_terse"
    env = {"STRAND_PERSONA_STANCE": "mentor_thoughtful"}
    assert Config.from_env(env, {"persona_stance": "operator_terse"}).persona_stance == "mentor_thoughtful"


def test_save_config_value_validates_stance(tmp_path):
    path = tmp_path / "config.json"
    save_config_value("persona_stance", "Operator_Terse", path)
    assert "operator_terse" in path.read_text()
    with pytest.raises(ValueError):
        save_config_value("persona_stance", "bogus", path)


@pytest.mark.parametrize("name", sorted(PERSONA_STANCES))
def test_resolver_injects_configured_stance(tmp_path, name):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            resolver = ContextResolver(db, persona_stance=name)
            first = await resolver.resolve("hello", "s")
            second = await resolver.resolve("hello", "s")
            return first, second

    first, second = run(scenario())
    assert first == f"## Strand Memory\n{BEDROCK_INVARIANT}\n[Persona Stance]\n{PERSONA_STANCES[name]}"
    assert second == ""


def test_resolver_unknown_stance_falls_back_and_dynamic_traits_follow(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Dry", "trait", "Keep it dry.")
            resolver = ContextResolver(db, persona_stance="bogus")
            return await resolver.resolve("hello", "s")

    text = run(scenario())
    assert text.startswith(f"## Strand Memory\n{BEDROCK_INVARIANT}\n[Persona Stance]\n")
    base, dynamic = text.split("[Persona Stance]\n")[1].split("\n", 1)
    assert base == PERSONA_STANCES["peer_workbench"]
    assert "- Dry: Keep it dry." in dynamic


def test_explicit_persona_prompt_overrides_stance(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            resolver = ContextResolver(db, persona_prompt="Custom.", persona_stance="operator_terse")
            return await resolver.resolve("hello", "s")

    text = run(scenario())
    assert "Custom." in text and PERSONA_STANCES["operator_terse"] not in text
