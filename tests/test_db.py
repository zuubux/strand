import asyncio
import json
import sqlite3

import pytest
import sqlite_vec

from cortext.db import EMBEDDING_DIM, DatabaseManager


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_memory.db"


def _vec(value: float) -> list[float]:
    return [value] * EMBEDDING_DIM


def test_setup_creates_wal_fk_vec_and_schema(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            async with db.conn.execute("PRAGMA journal_mode") as cur:
                journal_mode = (await cur.fetchone())[0]
            async with db.conn.execute("PRAGMA foreign_keys") as cur:
                foreign_keys = (await cur.fetchone())[0]
            async with db.conn.execute("SELECT vec_version()") as cur:
                vec_version = (await cur.fetchone())[0]
            async with db.conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('entities', 'edges', 'episodes', 'entity_embeddings')"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
            return journal_mode, foreign_keys, vec_version, tables

    journal_mode, foreign_keys, vec_version, tables = run(scenario())
    assert db_path.exists()
    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert vec_version.startswith("v")
    assert tables == {"entities", "edges", "episodes", "entity_embeddings"}


def test_default_path_is_under_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = DatabaseManager()
    assert manager.db_path == tmp_path / ".config" / "cortext" / "memory.db"

    async def scenario():
        async with manager:
            pass

    run(scenario())
    assert manager.db_path.exists()


def test_connect_is_idempotent_and_conn_requires_connect(db_path):
    manager = DatabaseManager(db_path)
    with pytest.raises(RuntimeError):
        manager.conn

    async def scenario():
        await manager.connect()
        first = manager.conn
        await manager.connect()
        assert manager.conn is first
        await manager.close()
        await manager.close()

    run(scenario())
    with pytest.raises(RuntimeError):
        manager.conn


def test_upsert_two_entities(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            nic_id = await db.upsert_entity("Nic", "person", "Owner of the garage", {"city": "Austin"})
            bmw_id = await db.upsert_entity("1995 BMW 540i", "vehicle", "E34 sedan")
            async with db.conn.execute("SELECT * FROM entities ORDER BY id") as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return nic_id, bmw_id, rows

    nic_id, bmw_id, rows = run(scenario())
    assert isinstance(nic_id, int) and isinstance(bmw_id, int)
    assert nic_id != bmw_id
    assert [r["name"] for r in rows] == ["Nic", "1995 BMW 540i"]
    assert rows[0]["archetype"] == "person"
    assert rows[0]["summary"] == "Owner of the garage"
    assert json.loads(rows[0]["metadata"]) == {"city": "Austin"}
    assert rows[1]["archetype"] == "vehicle"
    assert json.loads(rows[1]["metadata"]) == {}
    assert rows[0]["created_at"] and rows[0]["updated_at"]


def test_upsert_entity_updates_in_place(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            first = await db.upsert_entity("Nic", "person", "v1", {"a": 1})
            second = await db.upsert_entity("Nic", "person", "v2")
            async with db.conn.execute("SELECT COUNT(*) FROM entities") as cur:
                count = (await cur.fetchone())[0]
            async with db.conn.execute("SELECT summary, metadata FROM entities WHERE id = ?", (first,)) as cur:
                row = await cur.fetchone()
            third = await db.upsert_entity("Nic", "person", "v3", {"b": 2})
            async with db.conn.execute("SELECT metadata FROM entities WHERE id = ?", (first,)) as cur:
                meta = json.loads((await cur.fetchone())[0])
            return first, second, third, count, row["summary"], json.loads(row["metadata"]), meta

    first, second, third, count, summary, metadata_kept, metadata_replaced = run(scenario())
    assert first == second == third
    assert count == 1
    assert summary == "v2"
    assert metadata_kept == {"a": 1}  # metadata=None must not wipe existing metadata
    assert metadata_replaced == {"b": 2}


def test_upsert_entity_embedding_roundtrip_and_replace(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            entity_id = await db.upsert_entity("Nic", "person", "", embedding=_vec(0.5))
            await db.upsert_entity("Nic", "person", "", embedding=_vec(0.25))
            async with db.conn.execute("SELECT COUNT(*) FROM entity_embeddings") as cur:
                count = (await cur.fetchone())[0]
            async with db.conn.execute(
                "SELECT entity_id FROM entity_embeddings WHERE embedding MATCH ? AND k = 1",
                (sqlite_vec.serialize_float32(_vec(0.25)),),
            ) as cur:
                nearest = (await cur.fetchone())[0]
            return entity_id, count, nearest

    entity_id, count, nearest = run(scenario())
    assert count == 1
    assert nearest == entity_id


def test_upsert_entity_rejects_wrong_embedding_dimension(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            with pytest.raises(ValueError):
                await db.upsert_entity("Nic", "person", "", embedding=[0.1, 0.2])
            async with db.conn.execute("SELECT COUNT(*) FROM entities") as cur:
                return (await cur.fetchone())[0]

    assert run(scenario()) == 0


def test_upsert_edge_nic_owns_bmw(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            nic_id = await db.upsert_entity("Nic", "person", "Owner")
            bmw_id = await db.upsert_entity("1995 BMW 540i", "vehicle", "E34 sedan")
            edge_id = await db.upsert_edge(nic_id, bmw_id, "owns", weight=0.9, metadata={"since": 2021})
            again = await db.upsert_edge(nic_id, bmw_id, "owns", weight=0.5)
            async with db.conn.execute("SELECT * FROM edges") as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return nic_id, bmw_id, edge_id, again, rows

    nic_id, bmw_id, edge_id, again, rows = run(scenario())
    assert edge_id == again
    assert len(rows) == 1
    assert rows[0]["source_id"] == nic_id
    assert rows[0]["target_id"] == bmw_id
    assert rows[0]["relation"] == "owns"
    assert rows[0]["weight"] == 0.5
    assert json.loads(rows[0]["metadata"]) == {"since": 2021}  # preserved when metadata=None


def test_upsert_edge_enforces_foreign_keys(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            nic_id = await db.upsert_entity("Nic", "person", "")
            with pytest.raises(sqlite3.IntegrityError):
                await db.upsert_edge(nic_id, 9999, "owns")
            # The failed write must not poison later writes on the shared connection.
            bmw_id = await db.upsert_entity("1995 BMW 540i", "vehicle", "")
            await db.upsert_edge(nic_id, bmw_id, "owns")
            async with db.conn.execute("SELECT COUNT(*) FROM edges") as cur:
                return (await cur.fetchone())[0]

    assert run(scenario()) == 1


def test_get_entity_subgraph_returns_nic_owns_bmw(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            nic_id = await db.upsert_entity("Nic", "person", "Owner")
            bmw_id = await db.upsert_entity("1995 BMW 540i", "vehicle", "E34 sedan")
            await db.upsert_edge(nic_id, bmw_id, "owns", metadata={"since": 2021})
            from_nic = await db.get_entity_subgraph(nic_id)
            from_bmw = await db.get_entity_subgraph(bmw_id)
            return nic_id, bmw_id, from_nic, from_bmw

    nic_id, bmw_id, from_nic, from_bmw = run(scenario())
    assert len(from_nic) == 1
    edge = from_nic[0]
    assert edge["source_id"] == nic_id
    assert edge["source_name"] == "Nic"
    assert edge["source_archetype"] == "person"
    assert edge["relation"] == "owns"
    assert edge["target_id"] == bmw_id
    assert edge["target_name"] == "1995 BMW 540i"
    assert edge["target_archetype"] == "vehicle"
    assert edge["target_summary"] == "E34 sedan"
    assert edge["weight"] == 1.0
    assert edge["metadata"] == {"since": 2021}
    assert edge["hop"] == 1
    # Traversal is bidirectional; orientation (Nic -> owns -> BMW) is preserved.
    assert from_bmw == from_nic


def test_get_entity_subgraph_hops_limit_and_ordering(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            nic = await db.upsert_entity("Nic", "person", "")
            bmw = await db.upsert_entity("1995 BMW 540i", "vehicle", "")
            shop = await db.upsert_entity("Garage", "system", "")
            part = await db.upsert_entity("M60 Engine", "concept", "")
            lone = await db.upsert_entity("Unrelated", "concept", "")
            await db.upsert_edge(nic, bmw, "owns", weight=0.4)
            await db.upsert_edge(nic, shop, "works_at", weight=0.9)
            await db.upsert_edge(bmw, part, "has_part", weight=1.0)
            await db.upsert_edge(part, nic, "is_serviced_by", weight=0.1)  # cycle back to Nic
            one_hop = await db.get_entity_subgraph(nic, max_hops=1)
            two_hop = await db.get_entity_subgraph(nic, max_hops=2)
            limited = await db.get_entity_subgraph(nic, max_hops=2, limit=2)
            isolated = await db.get_entity_subgraph(lone, max_hops=2)
            return one_hop, two_hop, limited, isolated

    one_hop, two_hop, limited, isolated = run(scenario())
    # 1 hop: every edge touching Nic (including the inbound one), strongest first.
    assert [e["relation"] for e in one_hop] == ["works_at", "owns", "is_serviced_by"]
    assert {e["hop"] for e in one_hop} == {1}
    # 2 hops adds BMW -> has_part -> M60, listed after all 1-hop edges; no duplicates.
    assert [e["relation"] for e in two_hop] == ["works_at", "owns", "is_serviced_by", "has_part"]
    assert two_hop[-1]["hop"] == 2
    assert len({e["edge_id"] for e in two_hop}) == len(two_hop)
    assert [e["relation"] for e in limited] == ["works_at", "owns"]
    assert isolated == []


def test_get_entity_subgraph_degenerate_arguments(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            nic = await db.upsert_entity("Nic", "person", "")
            bmw = await db.upsert_entity("1995 BMW 540i", "vehicle", "")
            await db.upsert_edge(nic, bmw, "owns")
            return (
                await db.get_entity_subgraph(nic, max_hops=0),
                await db.get_entity_subgraph(nic, limit=0),
                await db.get_entity_subgraph(424242),
            )

    assert run(scenario()) == ([], [], [])


def test_record_episode(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            nic_id = await db.upsert_entity("Nic", "person", "")
            bmw_id = await db.upsert_entity("1995 BMW 540i", "vehicle", "")
            with_ids = await db.record_episode("session-1", "Nic changed the BMW's oil.", [nic_id, bmw_id])
            without_ids = await db.record_episode("session-1", "Nothing extracted.")
            async with db.conn.execute("SELECT * FROM episodes ORDER BY id") as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            return nic_id, bmw_id, with_ids, without_ids, rows

    nic_id, bmw_id, with_ids, without_ids, rows = run(scenario())
    assert [r["id"] for r in rows] == [with_ids, without_ids]
    assert rows[0]["session_id"] == "session-1"
    assert rows[0]["narrative"] == "Nic changed the BMW's oil."
    assert json.loads(rows[0]["extracted_entities"]) == [nic_id, bmw_id]
    assert json.loads(rows[1]["extracted_entities"]) == []
    assert isinstance(rows[0]["timestamp"], float) and rows[0]["timestamp"] > 0


def test_data_persists_across_connections(db_path):
    async def write():
        async with DatabaseManager(db_path) as db:
            nic = await db.upsert_entity("Nic", "person", "")
            bmw = await db.upsert_entity("1995 BMW 540i", "vehicle", "")
            await db.upsert_edge(nic, bmw, "owns")
            return nic

    async def read(nic_id):
        async with DatabaseManager(db_path) as db:
            return await db.get_entity_subgraph(nic_id)

    nic_id = run(write())
    edges = run(read(nic_id))
    assert [(e["source_name"], e["relation"], e["target_name"]) for e in edges] == [
        ("Nic", "owns", "1995 BMW 540i")
    ]


# --- Semantic entity merging / alias consolidation ----------------------------------


def _dedup_embedder():
    from tests.test_embed import MockEmbeddingManager

    return MockEmbeddingManager(
        {
            "AMD Epyc 7302P": 0,
            "AMD Epyc 7302P CPU": 0,
            "AMD Epyc 7302P CPU: 16-core server CPU": 0,
            "AMD Epyc 7302P CPU: Other": 0,
            "Epyc Concept": 0,
            "Corvette": 1,
        }
    )


async def _entity_rows(db):
    async with db.conn.execute("SELECT id, name, summary, metadata FROM entities ORDER BY id") as cur:
        return [tuple(r) for r in await cur.fetchall()]


def test_near_identical_candidate_merges_and_records_alias(db_path):
    async def scenario():
        async with DatabaseManager(db_path, embedder=_dedup_embedder()) as db:
            canon = await db.upsert_entity("AMD Epyc 7302P", "system", "")
            merged = await db.upsert_entity_deduped("AMD Epyc 7302P CPU", "system", "16-core server CPU")
            again = await db.upsert_entity_deduped("AMD Epyc 7302P CPU", "system", "")
            async with db.conn.execute("SELECT updated_at FROM entities WHERE id = ?", (canon,)) as cur:
                stamp = (await cur.fetchone())[0]
            return canon, merged, again, await _entity_rows(db), stamp

    canon, merged, again, rows, stamp = run(scenario())
    assert merged == again == canon
    assert len(rows) == 1
    assert json.loads(rows[0][3])["aliases"] == ["AMD Epyc 7302P CPU"]
    assert rows[0][2] == "16-core server CPU" and stamp


def test_merge_does_not_overwrite_existing_summary_or_lower_confidence(db_path):
    async def scenario():
        async with DatabaseManager(db_path, embedder=_dedup_embedder()) as db:
            canon = await db.upsert_entity("AMD Epyc 7302P", "system", "Original")
            await db.upsert_entity_deduped(
                "AMD Epyc 7302P CPU", "system", "Other", origin="model_inferred", confidence=0.6
            )
            async with db.conn.execute(
                "SELECT summary, origin, confidence FROM entities WHERE id = ?", (canon,)
            ) as cur:
                return tuple(await cur.fetchone())

    assert run(scenario()) == ("Original", "user_explicit", 1.0)


def test_merge_raises_confidence_when_candidate_wins(db_path):
    async def scenario():
        async with DatabaseManager(db_path, embedder=_dedup_embedder()) as db:
            canon = await db.upsert_entity(
                "AMD Epyc 7302P", "system", "", origin="model_inferred", confidence=0.6
            )
            await db.upsert_entity_deduped("AMD Epyc 7302P CPU", "system", "")
            async with db.conn.execute(
                "SELECT origin, confidence FROM entities WHERE id = ?", (canon,)
            ) as cur:
                return tuple(await cur.fetchone())

    assert run(scenario()) == ("user_explicit", 1.0)


def test_different_archetype_does_not_merge_despite_zero_distance(db_path):
    async def scenario():
        async with DatabaseManager(db_path, embedder=_dedup_embedder()) as db:
            a = await db.upsert_entity("AMD Epyc 7302P", "system", "")
            b = await db.upsert_entity_deduped("Epyc Concept", "concept", "")
            return a, b, await _entity_rows(db)

    a, b, rows = run(scenario())
    assert a != b and len(rows) == 2


def test_distant_candidate_does_not_merge(db_path):
    async def scenario():
        async with DatabaseManager(db_path, embedder=_dedup_embedder()) as db:
            a = await db.upsert_entity("AMD Epyc 7302P", "system", "")
            b = await db.upsert_entity_deduped("Corvette", "system", "")
            return a, b

    a, b = run(scenario())
    assert a != b


def test_alias_and_case_insensitive_match_without_vectors(db_path, monkeypatch):
    async def scenario():
        async with DatabaseManager(db_path) as db:  # no embedder: string matching only
            canon = await db.upsert_entity("Promethean", "system", "", {"aliases": ["The Box"]})
            by_alias = await db.upsert_entity_deduped("the box", "system", "")
            by_name = await db.upsert_entity_deduped("PROMETHEAN", "system", "")
            fresh = await db.upsert_entity_deduped("Promethean Server", "system", "")
            return canon, by_alias, by_name, fresh

    canon, by_alias, by_name, fresh = run(scenario())
    assert by_alias == by_name == canon
    assert fresh != canon  # no vectors: near-synonyms are not merged


def test_vec_disabled_falls_back_to_string_matching(db_path):
    async def scenario():
        async with DatabaseManager(db_path, embedder=_dedup_embedder()) as db:
            db.vec_enabled = False
            canon = await db.upsert_entity("AMD Epyc 7302P", "system", "")
            near = await db.upsert_entity_deduped("AMD Epyc 7302P CPU", "system", "")
            exact = await db.upsert_entity_deduped("amd epyc 7302p", "system", "")
            return canon, near, exact

    canon, near, exact = run(scenario())
    assert exact == canon and near != canon
