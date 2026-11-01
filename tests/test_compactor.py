import asyncio
import json
import logging
import sqlite3
import time

import httpx
import pytest

from strand.cli import main
from strand.compactor import SYSTEM_PROMPT, SessionCompactor, parse_compaction, parse_compaction_output
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


FULL_OUTPUT = {
    "entity_summaries": [{"name": "NAS", "summary": "Storage box."}],
    "session_summary": "Homelab work.",
    "discovered_aliases": [{"canonical_name": "Karen", "alias": "KK"}],
    "operator_profile": {
        "focus_summary": "Working on NAS and DNS.",
        "inferred_traits": [
            {"name": "Terse diffs", "archetype": "preference", "relation": "prefers", "summary": "Wants minimal diffs."},
            {"name": "Strict tests", "archetype": "trait", "relation": "enforces", "summary": "Runs -W error."},
        ],
    },
}


def test_compaction_prompt_requests_multi_horizon_schema():
    for token in (
        "discovered_aliases",
        "canonical_name",
        "operator_profile",
        "focus_summary",
        "inferred_traits",
        "prefers",
        "enforces",
        "one-off conversational steering",
        "sustained pattern",
    ):
        assert token in SYSTEM_PROMPT, token


def test_parse_compaction_output_valid_multi_horizon():
    out = parse_compaction_output(json.dumps(FULL_OUTPUT))
    assert out["entity_summaries"] == [{"name": "NAS", "summary": "Storage box."}]
    assert out["session_summary"] == "Homelab work."
    assert out["discovered_aliases"] == [{"canonical_name": "Karen", "alias": "KK"}]
    profile = out["operator_profile"]
    assert profile["focus_summary"] == "Working on NAS and DNS."
    assert (profile["origin"], profile["confidence"]) == ("model_inferred", 0.60)
    assert [t["name"] for t in profile["inferred_traits"]] == ["Terse diffs", "Strict tests"]
    assert all(t["origin"] == "model_inferred" and t["confidence"] == 0.60 for t in profile["inferred_traits"])


def test_parse_compaction_output_overrides_claimed_provenance():
    data = json.loads(json.dumps(FULL_OUTPUT))
    data["operator_profile"]["inferred_traits"][0].update(origin="user_explicit", confidence=1.0)
    trait = parse_compaction_output(json.dumps(data))["operator_profile"]["inferred_traits"][0]
    assert (trait["origin"], trait["confidence"]) == ("model_inferred", 0.60)


def test_parse_compaction_output_code_fence_and_preamble():
    raw = json.dumps(FULL_OUTPUT)
    for wrapped in (f"```json\n{raw}\n```", f"Sure! Here is the JSON:\n{raw}\nHope that helps.", f"Here:\n```json\n{raw}\n```"):
        out = parse_compaction_output(wrapped)
        assert out["discovered_aliases"] == [{"canonical_name": "Karen", "alias": "KK"}]
        assert len(out["operator_profile"]["inferred_traits"]) == 2


def test_parse_compaction_output_partial_and_malformed_keys():
    out = parse_compaction_output('{"entity_summaries": [{"name": "A", "summary": "s"}]}')
    assert out["entity_summaries"] == [{"name": "A", "summary": "s"}]
    assert out["discovered_aliases"] == []
    assert out["operator_profile"]["focus_summary"] == ""
    assert out["operator_profile"]["inferred_traits"] == []
    bad = parse_compaction_output(
        json.dumps(
            {
                "entity_summaries": "nope",
                "discovered_aliases": ["KK", {"canonical_name": "Karen"}, {"canonical_name": "X", "alias": "x"}],
                "operator_profile": ["not a dict"],
            }
        )
    )
    assert bad["entity_summaries"] == [] and bad["discovered_aliases"] == []
    assert bad["operator_profile"]["inferred_traits"] == []
    odd = parse_compaction_output('{"operator_profile": {"focus_summary": 5, "inferred_traits": "x"}}')
    assert odd["operator_profile"]["focus_summary"] == "" and odd["operator_profile"]["inferred_traits"] == []


def test_parse_compaction_output_malformed_json_fails_open():
    for raw in ("", "nope", "{broken", '{"a": ', "[1, 2]", "```json\n{oops\n```", None, 42):
        out = parse_compaction_output(raw)
        assert out["entity_summaries"] == [] and out["discovered_aliases"] == []
        assert out["session_summary"] == ""
        assert out["operator_profile"]["focus_summary"] == ""
        assert out["operator_profile"]["inferred_traits"] == []


def test_parse_compaction_output_drops_invalid_archetypes_and_relations():
    def trait(name, archetype, relation, summary="s"):
        return {"name": name, "archetype": archetype, "relation": relation, "summary": summary}

    raw = json.dumps(
        {
            "operator_profile": {
                "inferred_traits": [
                    trait("ok", "Preference", "Prefers"),
                    trait("directive-not-allowed-relation", "directive", "demands"),
                    trait("bad-archetype", "person", "prefers"),
                    trait("private", "private", "enforces"),
                    trait("no-relation", "trait", ""),
                    trait("no-summary", "trait", "enforces", ""),
                    "junk",
                ]
            }
        }
    )
    traits = parse_compaction_output(raw)["operator_profile"]["inferred_traits"]
    assert [(t["name"], t["archetype"], t["relation"]) for t in traits] == [("ok", "preference", "prefers")]


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


def evolution_reply(**overrides):
    data = {
        "entity_summaries": [],
        "session_summary": "",
        "discovered_aliases": [],
        "operator_profile": {"focus_summary": "", "inferred_traits": []},
    }
    data.update(overrides)
    return lambda request: reply(json.dumps(data))


async def operator(db, summary_text="Primary operator", origin="user_explicit", confidence=1.0):
    op = await db.upsert_entity("Nic", "person", summary_text, origin=origin, confidence=confidence)
    host = await db.upsert_entity("box", "system", "host")
    await db.upsert_edge(op, host, "operates_host")
    return op


def test_compact_session_merges_discovered_aliases_and_fails_open(tmp_path):
    handler = evolution_reply(
        discovered_aliases=[
            {"canonical_name": "NAS", "alias": "the vault"},
            {"canonical_name": "Nonexistent", "alias": "ghost"},
        ]
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "a.db") as db:
            await seed(db)
            result = await compactor_for(db, handler).compact_session("homelab")
            async with db.conn.execute("SELECT metadata FROM entities WHERE name = 'NAS'") as cur:
                meta = json.loads((await cur.fetchone())[0])
            return result, meta, await db.find_entity("ghost"), await db.get_session_episodes("homelab")

    result, meta, ghost, pending = run(scenario())
    assert not result["skipped"] and pending == []
    assert meta["aliases"] == ["the vault"]
    assert ghost is None


def test_compact_session_inferred_traits_link_to_operator(tmp_path):
    handler = evolution_reply(
        operator_profile={
            "focus_summary": "Working on homelab.",
            "inferred_traits": [
                {"name": "Terse diffs", "archetype": "preference", "relation": "prefers", "summary": "Wants small diffs"}
            ],
        }
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "t.db") as db:
            await seed(db)
            op = await db.upsert_entity("User", "person", "generic", origin="model_inferred", confidence=0.3)
            await compactor_for(db, handler).compact_session("homelab")
            async with db.conn.execute(
                "SELECT t.name, t.archetype, t.origin, t.confidence, e.weight, e.origin, e.confidence "
                "FROM edges e JOIN entities t ON t.id = e.target_id WHERE e.source_id = ? AND e.relation = 'prefers'",
                (op,),
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]
            return rows, await summary(db, "User")

    rows, op_summary = run(scenario())
    assert rows == [("Terse diffs", "preference", "model_inferred", 0.6, 1.0, "model_inferred", 0.6)]
    assert op_summary == "Working on homelab."


def test_compact_session_does_not_overwrite_explicit_operator_or_traits(tmp_path):
    handler = evolution_reply(
        operator_profile={
            "focus_summary": "model focus",
            "inferred_traits": [
                {"name": "Verbose", "archetype": "trait", "relation": "prefers", "summary": "inferred"}
            ],
        }
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "x.db") as db:
            await seed(db)
            op = await operator(db)
            trait = await db.upsert_entity("Verbose", "directive", "User rule")
            await db.upsert_edge(op, trait, "prefers", weight=0.9)
            await compactor_for(db, handler).compact_session("homelab")
            async with db.conn.execute(
                "SELECT weight, origin, confidence FROM edges WHERE source_id = ? AND target_id = ?", (op, trait)
            ) as cur:
                edge = tuple(await cur.fetchone())
            async with db.conn.execute("SELECT archetype, summary FROM entities WHERE id = ?", (trait,)) as cur:
                ent = tuple(await cur.fetchone())
            return await summary(db, "Nic"), edge, ent

    op_summary, edge, ent = run(scenario())
    assert op_summary == "Primary operator"
    assert edge == (0.9, "user_explicit", 1.0)
    assert ent == ("directive", "User rule")


def test_compact_session_operator_profile_without_operator_is_noop(tmp_path):
    handler = evolution_reply(
        operator_profile={
            "focus_summary": "x",
            "inferred_traits": [{"name": "T", "archetype": "trait", "relation": "prefers", "summary": "s"}],
        }
    )

    async def scenario():
        async with DatabaseManager(tmp_path / "n.db") as db:
            await seed(db)
            result = await compactor_for(db, handler).compact_session("homelab")
            return result, await db.find_entity("T")

    result, trait = run(scenario())
    assert not result["skipped"] and trait is None
