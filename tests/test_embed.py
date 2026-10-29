import asyncio
import sys

import pytest

from strand.db import EMBEDDING_DIM, DatabaseManager
from strand.embed import Embedder, EmbeddingManager


def unit(axis: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIM
    vec[axis] = 1.0
    return vec


class MockEmbeddingManager:
    """Deterministic offline embedder: fixed unit vectors per text, axis 383 otherwise."""

    def __init__(self, table: dict[str, int] | None = None) -> None:
        self.table = table or {}
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return unit(self.table.get(text, EMBEDDING_DIM - 1))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "memory.db"


def test_mock_satisfies_embedder_protocol():
    assert isinstance(MockEmbeddingManager(), Embedder)
    assert isinstance(EmbeddingManager(model_factory=lambda name: None), Embedder)


def test_manager_is_lazy_and_loads_once():
    built = []

    class FakeModel:
        def embed(self, texts):
            for _ in texts:
                yield [0.5] * EMBEDDING_DIM

    def factory(name):
        built.append(name)
        return FakeModel()

    manager = EmbeddingManager("some/model", model_factory=factory)
    assert built == [] and not manager.loaded
    assert manager.embed("a") == [0.5] * EMBEDDING_DIM
    manager.embed("b")
    assert built == ["some/model"] and manager.loaded


def test_manager_import_does_not_import_fastembed():
    assert "fastembed" not in sys.modules


def test_manager_rejects_wrong_dimension():
    class BadModel:
        def embed(self, texts):
            yield [0.1, 0.2]

    with pytest.raises(ValueError, match="384"):
        EmbeddingManager(model_factory=lambda name: BadModel()).embed("x")


def test_upsert_stores_vector_and_replaces(db_path):
    async def go():
        async with DatabaseManager(db_path) as db:
            eid = await db.upsert_entity("Operator", "person", "", embedding=unit(0))
            await db.upsert_entity("Operator", "person", "", embedding=unit(1))
            async with db.conn.execute("SELECT COUNT(*) FROM entity_embeddings") as cur:
                count = (await cur.fetchone())[0]
            return eid, count, await db.find_similar_entities(unit(1), limit=1)

    eid, count, hits = run(go())
    assert count == 1 and hits == [eid]


def test_knn_distance_ordering_and_threshold(db_path):
    async def go():
        async with DatabaseManager(db_path) as db:
            a = await db.upsert_entity("A", "concept", "", embedding=unit(0))
            near = unit(0)
            near[1] = 0.2
            b = await db.upsert_entity("B", "concept", "", embedding=near)
            c = await db.upsert_entity("C", "concept", "", embedding=unit(5))
            return (
                a, b, c,
                await db.find_similar_entities(unit(0), limit=3, distance_threshold=0.7),
                await db.find_similar_entities(unit(0), limit=3, distance_threshold=5.0),
                await db.find_similar_entities(unit(0), limit=1, distance_threshold=5.0),
                await db.find_similar_entities(unit(0), limit=0),
            )

    a, b, c, tight, loose, limited, none = run(go())
    assert tight == [a, b]
    assert loose == [a, b, c]
    assert limited == [a]
    assert none == []


def test_find_similar_rejects_wrong_dimension(db_path):
    async def go():
        async with DatabaseManager(db_path) as db:
            await db.find_similar_entities([0.1, 0.2])

    with pytest.raises(ValueError, match="384"):
        run(go())


def test_drop_removes_vector(db_path):
    from strand.cli import _delete_entities

    async def go():
        async with DatabaseManager(db_path) as db:
            eid = await db.upsert_entity("Gone", "concept", "", embedding=unit(2))
            await _delete_entities(db, [eid])
            return await db.find_similar_entities(unit(2), limit=3, distance_threshold=5.0)

    assert run(go()) == []


def _embedding_ids(path):
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return sorted(r[0] for r in conn.execute("SELECT entity_id FROM entity_embeddings"))
    finally:
        conn.close()


def test_embed_backfill_cli_embeds_only_missing(db_path, monkeypatch, capsys):
    from strand import cli

    async def seed():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Alice", "person", "Lead dev", embedding=unit(0))
            await db.upsert_entity("Atlas", "project", "Main project")
            await db.upsert_entity("Bare", "concept", "")

    run(seed())
    assert _embedding_ids(db_path) == [1]

    mock = MockEmbeddingManager({"Atlas: Main project": 5})
    monkeypatch.setattr(cli, "EmbeddingManager", lambda: mock)
    assert cli.main(["--db", str(db_path), "embed-backfill"]) == 0
    assert "embedded 2" in capsys.readouterr().out
    assert mock.calls == ["Atlas: Main project", "Bare"]
    assert _embedding_ids(db_path) == [1, 2, 3]

    mock.calls.clear()
    assert cli.main(["--db", str(db_path), "embed-backfill"]) == 0
    assert "embedded 0" in capsys.readouterr().out
    assert mock.calls == []


def test_upsert_auto_embeds_with_name_summary_text(db_path):
    mock = MockEmbeddingManager({"Atlas: Main project": 5, "Bare": 6})

    async def go():
        async with DatabaseManager(db_path, embedder=mock) as db:
            atlas = await db.upsert_entity("Atlas", "project", "Main project")
            bare = await db.upsert_entity("Bare", "concept", "")
            return atlas, bare, await db.find_similar_entities(unit(5), limit=1)

    atlas, bare, nearest = run(go())
    assert mock.calls == ["Atlas: Main project", "Bare"]
    assert nearest == [atlas]
    assert _embedding_ids(db_path) == sorted([atlas, bare])
