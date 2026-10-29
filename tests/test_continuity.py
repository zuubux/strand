import asyncio
import time

import pytest

from strand.config import ENV_VARS, Config
from strand.db import DatabaseManager
from strand.resolver import ContextResolver
from strand.session import SessionLedger

DAY = 86400.0


def run(coro):
    return asyncio.run(coro)


async def _age(db, episode_id, days):
    await db.conn.execute(
        "UPDATE episodes SET timestamp = ? WHERE id = ?", (time.time() - days * DAY, episode_id)
    )
    await db.conn.commit()


@pytest.mark.parametrize(
    "name,archetype",
    [("Atlas", "project"), ("Zettelkasten", "concept"), ("Ripgrep", "tool"), ("Torque Wrench", "item")],
)
def test_lookup_horizon_session_and_domain_agnostic(tmp_path, name, archetype):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            ent = await db.upsert_entity(name, archetype, "x")
            other = await db.upsert_entity("Unrelated", archetype, "y")
            fresh = await db.record_episode("s1", f"Worked on {name}", [ent, other])
            old = await db.record_episode("s2", f"Ancient {name}", [ent])
            await _age(db, old, 10)
            await db.record_episode("s3", "Nothing relevant", [other])
            await db.record_episode("s4", "Malformed", [])
            hit = await db.get_recent_episodes_for_entities([ent])
            wide = await db.get_recent_episodes_for_entities([ent], max_age_days=30)
            excl = await db.get_recent_episodes_for_entities([ent], exclude_session_id="s1")
            capped = await db.get_recent_episodes_for_entities([ent], max_age_days=30, limit=1)
            none = await db.get_recent_episodes_for_entities([])
            return fresh, old, hit, wide, excl, capped, none, ent, other

    fresh, old, hit, wide, excl, capped, none, ent, other = run(scenario())
    assert [h["id"] for h in hit] == [fresh]
    assert hit[0]["narrative"] == f"Worked on {name}" and hit[0]["entities"] == [ent, other]
    assert len(hit[0]["created_at"]) == 19
    assert [h["id"] for h in wide] == [fresh, old]
    assert excl == []
    assert [c["id"] for c in capped] == [fresh]
    assert none == []


def test_resolver_injects_then_dedupes_per_session(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            ent = await db.upsert_entity("Wiring Harness", "item", "Loom")
            await db.record_episode("old-session", "Re-pinned the   connector\nblock.", [ent])
            resolver = ContextResolver(db, SessionLedger())
            t1 = await resolver.resolve("status of the Wiring Harness?", "new")
            t2 = await resolver.resolve("more on the Wiring Harness", "new")
            other = await resolver.resolve("Wiring Harness again", "other")
            same = await resolver.resolve("Wiring Harness again", "old-session")
            return t1, t2, other, same

    t1, t2, other, same = run(scenario())
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert t1.startswith("## Strand Memory\n[Recent Continuity]\n")
    assert f"- {today}: Re-pinned the connector block." in t1
    assert t1.index("[Recent Continuity]") < t1.index("Entities:")
    assert t2 == ""
    assert "[Recent Continuity]" in other
    assert "[Recent Continuity]" not in same


def test_resolver_omits_when_unmatched_or_expired(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            ent = await db.upsert_entity("Kiln", "system", "Pottery")
            ep = await db.record_episode("a", "Fired a test batch", [ent])
            miss = await ContextResolver(db, SessionLedger()).resolve("nothing here", "b")
            await _age(db, ep, 8)
            expired = await ContextResolver(db, SessionLedger()).resolve("the Kiln", "b")
            wide = await ContextResolver(
                db, SessionLedger(), continuity_horizon_days=30
            ).resolve("the Kiln", "b")
            return miss, expired, wide

    miss, expired, wide = run(scenario())
    assert miss == ""
    assert "[Recent Continuity]" not in expired and "Kiln" in expired
    assert "[Recent Continuity]" in wide


def test_resolver_caps_and_hides_private(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            ent = await db.upsert_entity("Garden", "project", "Beds")
            secret = await db.upsert_entity("Diary", "private", "hidden")
            for i in range(4):
                await db.record_episode("a", f"note {i}", [ent])
            await db.record_episode("a", "secret note", [ent, secret])
            r = ContextResolver(db, SessionLedger(), continuity_max_episodes=2)
            first = await r.resolve("Garden", "b")
            second = await r.resolve("Garden", "b")
            third = await r.resolve("Garden", "b")
            return first, second, third

    first, second, third = run(scenario())
    assert "secret" not in first
    assert "note 3" in first and "note 2" in first and "note 1" not in first
    assert "note 1" in second and "note 0" in second and "note 3" not in second
    assert "secret" not in second and third == ""  # every eligible episode already seen


def test_ledger_tracks_episodes():
    ledger = SessionLedger()
    assert ledger.seen_episode_ids("s") == frozenset()
    ledger.mark_episodes_seen("s", [1, 2])
    assert ledger.seen_episode_ids("s") == {1, 2}
    assert ledger.seen_episode_ids("t") == frozenset()
    ledger.reset("s")
    assert ledger.seen_episode_ids("s") == frozenset()


def test_config_continuity_keys(tmp_path):
    assert ENV_VARS["continuity_horizon_days"] == "STRAND_CONTINUITY_HORIZON_DAYS"
    assert ENV_VARS["continuity_max_episodes"] == "STRAND_CONTINUITY_MAX_EPISODES"
    cfg = Config()
    assert cfg.continuity_horizon_days == 7.0 and cfg.continuity_max_episodes == 2
    cfg = Config.from_env({"STRAND_CONTINUITY_HORIZON_DAYS": "3.5"}, {"continuity_max_episodes": 4})
    assert cfg.continuity_horizon_days == 3.5 and cfg.continuity_max_episodes == 4
