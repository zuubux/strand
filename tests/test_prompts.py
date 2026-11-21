import asyncio

import pytest

from strand import prompts
from strand.common import BEDROCK_INVARIANT
from strand.config import Config, load_config, save_config_value
from strand.db import DatabaseManager
from strand.resolver import ContextResolver
from tests.conftest import HOST_TIME_LINE


def run(coro):
    return asyncio.run(coro)


def test_no_persona_stance_api_remains():
    assert not hasattr(prompts, "PERSONA_STANCES")
    assert not hasattr(prompts, "DEFAULT_PERSONA_STANCE")
    assert not hasattr(prompts, "get_persona_stance")
    assert not hasattr(Config(), "persona_stance")


def test_legacy_persona_stance_key_is_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"persona_stance": "operator_terse", "persona_prompt": "Custom."}')
    cfg = load_config({}, path, strict=True)
    assert cfg.persona_prompt == "Custom." and not hasattr(cfg, "persona_stance")
    assert Config.from_env({}, {"persona_stance": "x"}) == Config.from_env({}, {})


def test_persona_stance_is_no_longer_a_savable_key(tmp_path):
    with pytest.raises(KeyError):
        save_config_value("persona_stance", "peer_workbench", tmp_path / "config.json")


def test_resolver_turn_one_is_pure_bedrock(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            resolver = ContextResolver(db)
            return await resolver.resolve("hello", "s"), await resolver.resolve("hello", "s")

    first, second = run(scenario())
    assert first == f"## Strand Memory\n{BEDROCK_INVARIANT}\n{HOST_TIME_LINE}"
    assert second == ""


def test_dynamic_traits_follow_bedrock(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Dry", "trait", "Keep it dry.")
            return await ContextResolver(db).resolve("hello", "s")

    text = run(scenario())
    assert text.startswith(f"## Strand Memory\n{BEDROCK_INVARIANT}\n[Persona Stance]\n- Dry: Keep it dry.")
