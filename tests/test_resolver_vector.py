import asyncio

from strand.db import DatabaseManager
from strand.resolver import ContextResolver
from tests.test_embed import MockEmbeddingManager, unit


def run(coro):
    return asyncio.run(coro)


async def seed(db):
    car = await db.upsert_entity("Corvette", "vehicle", "Fast car", embedding=unit(0))
    owner = await db.upsert_entity("Marcus", "person", "Owner", embedding=unit(1))
    secret = await db.upsert_entity("Vault", "private", "Secret", embedding=unit(2))
    await db.upsert_edge(car, owner, "owned_by")
    return car, owner, secret


def test_semantic_fallback_resolves_and_traverses(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            embedder = MockEmbeddingManager({"my automobile": 0})
            resolver = ContextResolver(db, embedder=embedder)
            entities, edges = await resolver.gather("my automobile")
            return [e.name for e in entities], [(e.relation) for e in edges], embedder.calls

    names, relations, calls = run(go())
    assert names[0] == "Corvette" and "Marcus" in names
    assert relations == ["owned_by"]
    assert calls == ["my automobile"]


def test_exact_match_first_then_semantic_fills(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            resolver = ContextResolver(db, embedder=MockEmbeddingManager({"Marcus and wheels": 0}))
            return [e.name for e in await resolver.match_entities("Marcus and wheels")]

    assert run(go()) == ["Marcus", "Corvette"]


def test_semantic_skipped_when_full(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            embedder = MockEmbeddingManager({"Marcus": 0})
            resolver = ContextResolver(db, max_entities=1, embedder=embedder)
            return [e.name for e in await resolver.match_entities("Marcus")], embedder.calls

    names, calls = run(go())
    assert names == ["Marcus"] and calls == []


def test_semantic_respects_privacy(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            resolver = ContextResolver(db, embedder=MockEmbeddingManager({"hidden stuff": 2}))
            return (
                [e.name for e in await resolver.match_entities("hidden stuff", allow_private=False)],
                [e.name for e in await resolver.match_entities("hidden stuff", allow_private=True)],
            )

    public, private = run(go())
    assert public == [] and private == ["Vault"]


def test_semantic_distance_threshold(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            resolver = ContextResolver(db, embedder=MockEmbeddingManager({"unrelated": 9}))
            return await resolver.match_entities("unrelated")

    assert run(go()) == []


def test_no_embedder_keeps_lexical_behaviour(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            return await ContextResolver(db).match_entities("my automobile")

    assert run(go()) == []


def test_config_distance_threshold_controls_cutoff(tmp_path, monkeypatch):
    from strand.config import Config, load_config
    assert Config().distance_threshold == 1.0
    monkeypatch.setenv("STRAND_DISTANCE_THRESHOLD", "0.5")
    assert load_config(config_path=tmp_path / "none.json").distance_threshold == 0.5
    monkeypatch.delenv("STRAND_DISTANCE_THRESHOLD")
    assert Config.from_env({}, {"distance_threshold": 0.9}).distance_threshold == 0.9

    mid = unit(0)
    mid[1] = 0.85  # L2 distance to unit(0) is exactly 0.85

    async def go(threshold):
        async with DatabaseManager(tmp_path / f"m{threshold}.db") as db:
            await db.upsert_entity("Mid", "concept", "", embedding=mid)
            resolver = ContextResolver(
                db, embedder=MockEmbeddingManager({"q": 0}), distance_threshold=threshold
            )
            return [e.name for e in await resolver.match_entities("q")]

    assert run(go(1.0)) == ["Mid"]
    assert run(go(0.7)) == []
    assert run(go(0.9)) == ["Mid"]


def test_build_app_passes_distance_threshold(tmp_path):
    from strand.config import Config
    from strand.daemon import build_app

    captured = {}
    import strand.daemon as daemon

    original = daemon.ContextResolver

    class Spy(original):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    daemon.ContextResolver = Spy
    try:
        build_app(Config(db_path=str(tmp_path / "m.db"), distance_threshold=0.42))
    finally:
        daemon.ContextResolver = original
    assert captured["distance_threshold"] == 0.42


def test_helpers_fail_closed_without_allow_private(tmp_path):
    """SEC-PRIV-003: omitting `allow_private` must never surface private entities."""

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            car, owner, secret = await seed(db)
            await db.upsert_edge(owner, secret, "guards")
            resolver = ContextResolver(db)
            query = "Marcus and Vault"
            default_match = [e.name for e in await resolver.match_entities(query)]
            open_match = [e.name for e in await resolver.match_entities(query, allow_private=True)]
            entities, edges = await resolver.gather(query)
            open_entities, _ = await resolver.gather(query, allow_private=True)
            roots = await resolver.match_entities(query, allow_private=True)
            private_ids = {secret}
            default_roots = (await resolver._gather_with_roots(query))[2]
            personality = await resolver.personality_entities()
            return (
                default_match, open_match, entities, edges, open_entities, default_roots, personality,
                roots, private_ids,
            )

    default_match, open_match, entities, edges, open_entities, default_roots, personality, roots, ids = run(go())
    assert "Vault" not in default_match and "Vault" in open_match
    assert "Vault" not in [e.name for e in entities] and "Vault" in [e.name for e in open_entities]
    assert all(e.id not in ids for e in entities)
    assert all(edge.source_id not in ids and edge.target_id not in ids for edge in edges)
    assert all(e.name != "Vault" for e in default_roots)
    assert all(e.name != "Vault" for e in personality)
