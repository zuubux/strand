import asyncio
import json
import logging
import sqlite3
import time

import httpx
import pytest

from strand.cli import main
from strand.compactor import SessionCompactor, parse_compaction
from strand.config import ENV_VARS, Config
from strand.db import DatabaseManager

HOUR = 3600.0


def run(coro):
    return asyncio.run(coro)


def reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def compactor_for(db, handler, model="tiny"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SessionCompactor(db, "http://slm.test/v1", model, client=client)


async def seed(db, session="homelab", age_hours=48.0, count=2):
    nas = await db.upsert_entity("NAS", "system", "old", origin="model_inferred", confidence=0.4)
    pi = await db.upsert_entity("Pihole", "system", "DNS", origin="model_inferred", confidence=0.5)
    ids = []
    for i in range(count):
        ids.append(await db.record_episode(session, f"NAS update {i}. Pihole runs on it.", [nas, pi]))
    await db.conn.execute(
        "UPDATE episodes SET timestamp = ? WHERE session_id = ?",
        (time.time() - age_hours * HOUR, session),
    )
    await db.conn.commit()
    return ids


async def summary(db, name):
    async with db.conn.execute("SELECT summary FROM entities WHERE name = ?", (name,)) as cur:
        return (await cur.fetchone())[0]


def test_config_key_and_default():
    assert ENV_VARS["compaction_threshold_hours"] == "STRAND_COMPACTION_THRESHOLD_HOURS"
    assert Config().compaction_threshold_hours == 24.0
    cfg = Config.from_env({"STRAND_COMPACTION_THRESHOLD_HOURS": "2.5"}, {})
    assert cfg.compaction_threshold_hours == 2.5


def test_migration_adds_compacted_at(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, "
        "session_id TEXT NOT NULL, narrative TEXT NOT NULL, extracted_entities JSON DEFAULT '[]')"
    )
    conn.execute("INSERT INTO episodes (timestamp, session_id, narrative) VALUES (1.0, 's', 'n')")
    conn.commit()
    conn.close()

    async def scenario():
        async with DatabaseManager(path) as db:
            async with db.conn.execute("PRAGMA table_info(episodes)") as cur:
                cols = {r[1] for r in await cur.fetchall()}
            return cols, await db.get_session_episodes("s")

    cols, episodes = run(scenario())
    assert "compacted_at" in cols
    assert len(episodes) == 1 and episodes[0]["compacted_at"] is None


def test_get_uncompacted_sessions_respects_cutoff(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "d.db") as db:
            await seed(db, "old", age_hours=48)
            await seed(db, "mixed", age_hours=48, count=1)
            await db.record_episode("mixed", "recent turn")  # newest episode is fresh
            await db.record_episode("fresh", "just now")
            done = await seed(db, "done", age_hours=48)
            await db.mark_episodes_compacted(done)
            return (
                await db.get_uncompacted_sessions(24 * HOUR),
                await db.get_uncompacted_sessions(0.0),
            )

    old_only, everything = run(scenario())
    assert old_only == ["old"]
    assert set(everything) == {"old", "mixed", "fresh"}


def test_mark_episodes_compacted_sets_timestamps(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            ids = await seed(db)
            await db.mark_episodes_compacted([])  # no-op
            await db.mark_episodes_compacted(ids[:1])
            return (
                await db.get_session_episodes("homelab", uncompacted_only=False),
                await db.get_session_episodes("homelab"),
            )

    everything, pending = run(scenario())
    assert everything[0]["compacted_at"] is not None
    assert everything[1]["compacted_at"] is None
    assert [e["id"] for e in pending] == [everything[1]["id"]]


def test_parse_compaction_variants():
    good = '{"entity_summaries": [{"name": "A", "summary": "s"}, {"name": ""}], "session_summary": "x"}'
    assert parse_compaction(good) == {
        "entity_summaries": [{"name": "A", "summary": "s"}],
        "session_summary": "x",
    }
    assert parse_compaction(f"```json\n{good}\n```")["session_summary"] == "x"
    assert parse_compaction("nope") is None
    assert parse_compaction("[1]") is None


def test_compact_session_updates_summaries_and_marks_episodes(tmp_path):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return reply(
            json.dumps(
                {
                    "entity_summaries": [
                        {"name": "NAS", "summary": "Primary storage box running Pihole."},
                        {"name": "Ghost", "summary": "unknown entity"},
                    ],
                    "session_summary": "Homelab work.",
                }
            )
        )

    async def scenario():
        async with DatabaseManager(tmp_path / "s.db") as db:
            await seed(db)
            result = await compactor_for(db, handler).compact_session("homelab")
            return result, await summary(db, "NAS"), await db.get_session_episodes("homelab")

    result, nas_summary, pending = run(scenario())
    assert result["episodes"] == 2 and result["summaries_updated"] == 1
    assert result["session_summary"] == "Homelab work."
    assert nas_summary == "Primary storage box running Pihole."
    assert pending == []
    assert "NAS update 0" in seen["body"]["messages"][1]["content"]


def test_empty_session_returns_immediately(tmp_path):
    def handler(request):
        raise AssertionError("SLM must not be called")

    async def scenario():
        async with DatabaseManager(tmp_path / "e.db") as db:
            return await compactor_for(db, handler).compact_session("missing")

    result = run(scenario())
    assert result["episodes"] == 0 and not result["skipped"]


def test_low_confidence_compaction_never_overwrites_user_explicit(tmp_path):
    def handler(request):
        return reply(
            json.dumps(
                {
                    "entity_summaries": [
                        {"name": "Atlas", "summary": "model guess"},
                        {"name": "NAS", "summary": "better"},
                    ],
                    "session_summary": "",
                }
            )
        )

    async def scenario():
        async with DatabaseManager(tmp_path / "p.db") as db:
            await seed(db)
            atlas = await db.upsert_entity("Atlas", "project", "User truth")
            await db.record_episode("homelab", "Atlas notes", [atlas])
            await db.conn.execute("UPDATE episodes SET timestamp = 1.0")
            await db.conn.commit()
            result = await compactor_for(db, handler).compact_session("homelab")
            return result, await summary(db, "Atlas"), await summary(db, "NAS")

    result, atlas_summary, nas_summary = run(scenario())
    assert atlas_summary == "User truth"
    assert nas_summary == "better"
    assert result["summaries_updated"] == 1
    assert result["episodes"] == 3


def test_slm_offline_logs_and_skips(tmp_path, caplog):
    def handler(request):
        raise httpx.ConnectError("refused")

    async def scenario():
        async with DatabaseManager(tmp_path / "o.db") as db:
            await seed(db)
            compactor = compactor_for(db, handler)
            result = await compactor.compact_session("homelab")
            count = await compactor.compact_all(0.0)
            return result, count, await db.get_session_episodes("homelab")

    with caplog.at_level(logging.WARNING):
        result, count, pending = run(scenario())
    assert result["skipped"] is True
    assert count == 0
    assert len(pending) == 2
    assert any("compaction SLM unavailable" in r.getMessage() for r in caplog.records)


def test_garbage_slm_output_is_skipped(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "g.db") as db:
            await seed(db)
            result = await compactor_for(db, lambda r: reply("not json")).compact_session("homelab")
            return result, await db.get_session_episodes("homelab")

    result, pending = run(scenario())
    assert result["skipped"] and len(pending) == 2


def test_compact_all_counts_and_uses_threshold(tmp_path):
    def handler(request):
        return reply('{"entity_summaries": [], "session_summary": ""}')

    async def scenario():
        async with DatabaseManager(tmp_path / "a.db") as db:
            await seed(db, "a", age_hours=48)
            await db.record_episode("b", "recent")
            compactor = compactor_for(db, handler)
            first = await compactor.compact_all()
            second = await compactor.compact_all()
            forced = await compactor.compact_all(older_than_hours=0.0)
            return first, second, forced

    assert run(scenario()) == (1, 0, 1)


def test_heuristic_fallback_without_model(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "h.db") as db:
            await seed(db)
            compactor = SessionCompactor(db, "", "")
            result = await compactor.compact_session("homelab")
            return result, await summary(db, "NAS")

    result, nas_summary = run(scenario())
    assert result["episodes"] == 2 and not result["skipped"]
    assert "NAS update" in nas_summary


def test_cli_compact(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("STRAND_EXTRACTOR_MODEL", raising=False)
    monkeypatch.delenv("STRAND_COMPACTION_THRESHOLD_HOURS", raising=False)
    path = tmp_path / "cli.db"

    async def setup():
        async with DatabaseManager(path) as db:
            await seed(db, "homelab-setup", age_hours=48)
            await db.record_episode("fresh", "recent")

    run(setup())
    assert main(["--db", str(path), "compact"]) == 0
    out = capsys.readouterr().out
    assert "Compacted session 'homelab-setup' (2 episodes, 2 summaries updated)" in out
    assert "fresh" not in out

    assert main(["--db", str(path), "compact"]) == 0
    assert "no sessions to compact" in capsys.readouterr().out

    assert main(["--db", str(path), "compact", "--session", "fresh"]) == 0
    assert "Compacted session 'fresh' (1 episodes, 0 summaries updated)" in capsys.readouterr().out


@pytest.mark.parametrize("hours", ["0"])
def test_cli_compact_hours_override(tmp_path, monkeypatch, capsys, hours):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("STRAND_EXTRACTOR_MODEL", raising=False)
    path = tmp_path / "cli2.db"

    async def setup():
        async with DatabaseManager(path) as db:
            await db.record_episode("fresh", "recent")

    run(setup())
    assert main(["--db", str(path), "compact", "--hours", hours]) == 0
    assert "Compacted session 'fresh'" in capsys.readouterr().out
