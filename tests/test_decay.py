"""Temporal access decay, frequency reinforcement and touch_access (offline)."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from strand.config import ENV_VARS, Config, load_config, save_config_value
from strand.db import DatabaseManager
from strand.resolver import ContextResolver, effective_priority
from strand.session import SessionLedger

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "decay.db"


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


async def _age(db, name, days_ago, access_count=0, accessed=False):
    """Backdate an entity's created_at (and optionally last_accessed_at)."""
    await db.conn.execute(
        "UPDATE entities SET created_at = ?, access_count = ?, last_accessed_at = ? WHERE name = ?",
        (_ts(days_ago), access_count, _ts(days_ago) if accessed else None, name),
    )
    await db.conn.commit()


def _resolver(db, **kwargs):
    return ContextResolver(db, SessionLedger(), clock=lambda: NOW, **kwargs)


# --- schema / migration / touch_access ---------------------------------------------


def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_fresh_schema_has_access_columns(db_path):
    async def scenario():
        async with DatabaseManager(db_path):
            pass

    run(scenario())
    for table in ("entities", "edges"):
        cols = _columns(db_path, table)
        assert cols["access_count"][2] == "INTEGER" and cols["access_count"][3] == 1
        assert cols["access_count"][4] == "0"
        assert "last_accessed_at" in cols


def test_migration_adds_access_columns_to_old_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            archetype TEXT NOT NULL, summary TEXT DEFAULT '', metadata JSON DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, relation TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
            metadata JSON DEFAULT '{}', UNIQUE(source_id, target_id, relation));
        INSERT INTO entities (name, archetype) VALUES ('Old', 'concept');
        """
    )
    conn.commit()
    conn.close()

    async def scenario():
        async with DatabaseManager(db_path) as db:
            async with db.conn.execute("SELECT access_count, last_accessed_at FROM entities") as cur:
                return tuple(await cur.fetchone())

    assert run(scenario()) == (0, None)
    for table in ("entities", "edges"):
        assert {"access_count", "last_accessed_at"} <= set(_columns(db_path, table))
    assert run(scenario()) == (0, None)  # reconnecting a migrated DB is a no-op


def test_touch_access_bumps_entities_and_edges(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            a = await db.upsert_entity("A", "concept", "")
            b = await db.upsert_entity("B", "concept", "")
            edge = await db.upsert_edge(a, b, "uses")
            await db.touch_access([a], [edge])
            await db.touch_access([a, a], [])
            async with db.conn.execute(
                "SELECT name, access_count, last_accessed_at FROM entities ORDER BY id"
            ) as cur:
                ents = [tuple(r) for r in await cur.fetchall()]
            async with db.conn.execute("SELECT access_count, last_accessed_at FROM edges") as cur:
                edge_row = tuple(await cur.fetchone())
            return ents, edge_row

    ents, edge_row = run(scenario())
    assert ents[0][1] == 2 and ents[0][2] is not None  # duplicate ids in one call count once
    assert ents[1] == ("B", 0, None)
    assert edge_row[0] == 1 and edge_row[1] is not None


def test_touch_access_handles_empty_and_unknown_ids(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.touch_access([], [])
            await db.touch_access([999], [999])

    run(scenario())


# --- config ---------------------------------------------------------------------------


def test_decay_config_defaults_env_and_file(tmp_path):
    assert Config().decay_enabled is True and Config().decay_half_life_days == 30.0
    assert ENV_VARS["decay_enabled"] == "STRAND_DECAY_ENABLED"
    assert ENV_VARS["decay_half_life_days"] == "STRAND_DECAY_HALF_LIFE_DAYS"
    cfg = Config.from_env({"STRAND_DECAY_ENABLED": "true", "STRAND_DECAY_HALF_LIFE_DAYS": "0.5"}, {})
    assert cfg.decay_enabled is True and cfg.decay_half_life_days == 0.5
    assert Config.from_env({"STRAND_DECAY_ENABLED": "false"}, {"decay_enabled": True}).decay_enabled is False
    path = tmp_path / "config.json"
    save_config_value("decay_enabled", "true", path)
    save_config_value("decay_half_life_days", "7", path)
    loaded = load_config({}, path)
    assert loaded.decay_enabled is True and loaded.decay_half_life_days == 7.0
    with pytest.raises(ValueError):
        save_config_value("decay_enabled", "maybe", path)


# --- effective priority math ----------------------------------------------------------


def test_effective_priority_disabled_is_confidence_times_weight():
    assert effective_priority(0.8, 0.5, 99, _ts(900), now=NOW, decay_enabled=False) == pytest.approx(0.4)


def test_effective_priority_half_life_and_frequency():
    kw = dict(now=NOW, decay_enabled=True, half_life_days=30.0)
    assert effective_priority(1.0, 1.0, 0, None, _ts(30), **kw) == pytest.approx(0.5)
    assert effective_priority(1.0, 1.0, 0, None, _ts(60), **kw) == pytest.approx(0.25)
    # last_accessed_at wins over created_at
    assert effective_priority(1.0, 1.0, 0, _ts(0), _ts(300), **kw) == pytest.approx(1.0)
    assert effective_priority(1.0, 1.0, 4, _ts(0), None, **kw) == pytest.approx(1.2)
    assert effective_priority(1.0, 1.0, 1000, _ts(0), None, **kw) == pytest.approx(1.5)  # capped
    future = (NOW + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert effective_priority(1.0, 1.0, 0, future, None, **kw) == pytest.approx(1.0)


def test_effective_priority_fractional_half_life():
    kw = dict(now=NOW, decay_enabled=True, half_life_days=0.5)
    assert effective_priority(1.0, 1.0, 0, _ts(0.5), **kw) == pytest.approx(0.5)  # 12 hours
    assert effective_priority(1.0, 1.0, 0, _ts(1.0), **kw) == pytest.approx(0.25)


# --- resolver behaviour ---------------------------------------------------------------


def test_recent_entity_outranks_old_unaccessed(db_path):
    async def scenario(**kwargs):
        async with DatabaseManager(db_path) as db:
            if not await db.find_duplicate_entity("Old Server", "system", ""):
                await db.upsert_entity("Old Server", "system", "old")
                await db.upsert_entity("New Server", "system", "new")
                await _age(db, "Old Server", 40)
                await _age(db, "New Server", 1)
            r = _resolver(db, max_entities=1, **kwargs)
            return [e.name for e in await r.match_entities("Old Server and New Server")]

    # Baseline: equal confidence -> first mention in the query wins.
    assert run(scenario()) == ["Old Server"]
    assert run(scenario(decay_enabled=True, decay_half_life_days=30.0)) == ["New Server"]


def test_fractional_half_life_resolution(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Stale", "concept", "stale")
            await db.upsert_entity("Fresh", "concept", "fresh")
            await _age(db, "Stale", 2)  # 4 half-lives at 0.5d -> 0.0625 < cutoff
            await _age(db, "Fresh", 0.1)
            r = _resolver(db, decay_enabled=True, decay_half_life_days=0.5)
            return [e.name for e in await r.match_entities("Stale and Fresh")]

    assert run(scenario()) == ["Fresh"]


def test_frequency_reinforcement_keeps_old_entity_ahead(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Neglected", "concept", "n")
            await db.upsert_entity("Beloved", "concept", "b")
            await _age(db, "Neglected", 20)
            await _age(db, "Beloved", 20, access_count=10)
            r = _resolver(db, decay_enabled=True, decay_half_life_days=30.0, max_entities=1)
            return [e.name for e in await r.match_entities("Neglected and Beloved")]

    assert run(scenario()) == ["Beloved"]


def test_old_untouched_entities_are_pruned_below_cutoff(db_path):
    async def scenario(enabled):
        async with DatabaseManager(db_path) as db:
            if not await db.find_duplicate_entity("Ancient", "concept", ""):
                await db.upsert_entity("Ancient", "concept", "x")
                await _age(db, "Ancient", 365)
            r = _resolver(db, decay_enabled=enabled)
            return [e.name for e in await r.match_entities("Ancient")]

    assert run(scenario(False)) == ["Ancient"]  # baseline: never pruned
    assert run(scenario(True)) == []


def test_decay_orders_and_prunes_edges(db_path):
    async def scenario(enabled):
        async with DatabaseManager(db_path) as db:
            if not await db.find_duplicate_entity("Hub", "system", ""):
                hub = await db.upsert_entity("Hub", "system", "")
                old = await db.upsert_entity("OldPeer", "system", "")
                new = await db.upsert_entity("NewPeer", "system", "")
                dead = await db.upsert_entity("DeadPeer", "system", "")
                e_old = await db.upsert_edge(hub, old, "links")
                e_new = await db.upsert_edge(hub, new, "links")
                e_dead = await db.upsert_edge(hub, dead, "links")
                await db.conn.executemany(
                    "UPDATE edges SET updated_at = ? WHERE id = ?",
                    [(_ts(20), e_old), (_ts(1), e_new), (_ts(400), e_dead)],
                )
                await db.conn.commit()
            r = _resolver(db, decay_enabled=enabled, edge_limit=10)
            _, edges = await r.gather("Hub")
            return [e.id for e in edges]

    assert run(scenario(False)) == [1, 2, 3]
    assert run(scenario(True)) == [2, 1]  # fresh first; 400-day-old edge pruned


# --- touch on resolution --------------------------------------------------------------


def test_resolve_touches_contributing_entities_and_edges(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            hub = await db.upsert_entity("Hub", "system", "core")
            peer = await db.upsert_entity("Peer", "system", "leaf")
            await db.upsert_entity("Bystander", "system", "unrelated")
            await db.upsert_edge(hub, peer, "links")
            r = _resolver(db)
            first = await r.resolve("Hub", "s1")
            await r.resolve("Hub", "s1")  # empty delta still reinforces the recalled facts
            await r.resolve("nothing relevant", "s1")
            async with db.conn.execute("SELECT name, access_count FROM entities ORDER BY id") as cur:
                ents = {row[0]: row[1] for row in await cur.fetchall()}
            async with db.conn.execute("SELECT access_count, last_accessed_at FROM edges") as cur:
                edge = tuple(await cur.fetchone())
            return first, ents, edge

    first, ents, edge = run(scenario())
    assert "- Hub (system): core" in first and "-links->" in first
    assert ents == {"Hub": 2, "Peer": 2, "Bystander": 0}
    assert edge[0] == 2 and edge[1] is not None


def test_resolve_survives_touch_failure(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Hub", "system", "core")
            r = _resolver(db)

            async def boom(*_args, **_kwargs):
                raise RuntimeError("disk full")

            db.touch_access = boom
            return await r.resolve("Hub", "s1")

    assert "Hub" in run(scenario())


# --- strict half-life validation / defensive clamping --------------------------------


@pytest.mark.parametrize("bad", [0, -1, -0.5, "nan", "inf", "-inf", "abc"])
def test_strict_config_rejects_invalid_half_life(tmp_path, bad):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"decay_half_life_days": bad}))
    with pytest.raises(ValueError, match="decay_half_life_days"):
        load_config({}, path, strict=True)
    with pytest.raises(ValueError, match="decay_half_life_days"):
        load_config({"STRAND_DECAY_HALF_LIFE_DAYS": str(bad)}, tmp_path / "none.json", strict=True)
    with pytest.raises(ValueError, match="decay_half_life_days"):
        save_config_value("decay_half_life_days", bad, tmp_path / "saved.json")
    assert not (tmp_path / "saved.json").exists()


def test_strict_config_accepts_valid_half_life(tmp_path):
    assert load_config({"STRAND_DECAY_HALF_LIFE_DAYS": "0.5"}, tmp_path / "n.json", strict=True).decay_half_life_days == 0.5
    assert load_config({}, tmp_path / "n.json", strict=True).decay_half_life_days == 30.0


@pytest.mark.parametrize("bad", [0.0, -3.0, float("nan"), float("inf"), float("-inf")])
def test_effective_priority_falls_back_to_undecayed_on_bad_half_life(bad):
    value = effective_priority(0.8, 0.5, 9, _ts(0), _ts(300), now=NOW, decay_enabled=True, half_life_days=bad)
    assert value == pytest.approx(0.4)


async def _backdate_edge(db, edge_id, days_ago, access_count=0):
    await db.conn.execute(
        "UPDATE edges SET last_accessed_at = ?, updated_at = ?, access_count = ? WHERE id = ?",
        (_ts(days_ago), _ts(days_ago), access_count, edge_id),
    )
    await db.conn.commit()


def _stamp(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def test_prune_decayed_edges_atomic_and_dry_run(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "p.db") as db:
            hub = await db.upsert_entity("Hub", "concept", "")
            cold = await db.upsert_entity("Cold", "concept", "", origin="model_inferred", confidence=0.6)
            warm = await db.upsert_entity("Warm", "concept", "")
            busy = await db.upsert_entity("Busy", "concept", "")
            e_cold = await db.upsert_edge(hub, cold, "mentions")
            e_warm = await db.upsert_edge(hub, warm, "mentions")
            e_busy = await db.upsert_edge(hub, busy, "mentions")
            for edge_id, days, count in ((e_cold, 200, 0), (e_warm, 5, 0), (e_busy, 200, 50)):
                await db.conn.execute(
                    "UPDATE edges SET last_accessed_at = ?, updated_at = ?, access_count = ? WHERE id = ?",
                    (_stamp(days), _stamp(days), count, edge_id),
                )
            await db.conn.commit()
            dry = await db.prune_decayed_edges(90, dry_run=True)
            async with db.conn.execute("SELECT COUNT(*) FROM edges") as cur:
                after_dry = (await cur.fetchone())[0]
            real = await db.prune_decayed_edges(90)
            async with db.conn.execute("SELECT id FROM edges ORDER BY id") as cur:
                left = [r[0] for r in await cur.fetchall()]
            async with db.conn.execute("SELECT name FROM entities ORDER BY id") as cur:
                names = [r[0] for r in await cur.fetchall()]
            again = await db.prune_decayed_edges(90)
            return dry, after_dry, real, left, names, again, (e_warm, e_busy)

    dry, after_dry, real, left, names, again, kept = run(scenario())
    assert dry == (1, 1) and after_dry == 3
    assert real == (1, 1) and left == list(kept)
    assert names == ["Hub", "Warm", "Busy"]  # orphaned model_inferred entity swept, others kept
    assert again == (0, 0)
