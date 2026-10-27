import asyncio

import pytest

from cortext.db import DatabaseManager
from cortext.distill import DistillationEngine, heuristic_extract
from cortext.resolver import ContextResolver
from cortext.stopwords import is_valid_entity_name


@pytest.mark.parametrize(
    "name", ["Marcus", "Atlas", "BMW E34", "Database Migration", "Please Review", "SQLite", "AI", "Python"]
)
def test_valid_names(name):
    assert is_valid_entity_name(name)


@pytest.mark.parametrize(
    "name",
    ["However", "please", "CERTAINLY", "Monday", "March", "Hello", "", "   ", "--", "X", "ab", "Sure."],
)
def test_invalid_concept_names(name):
    assert not is_valid_entity_name(name, "concept")


def test_stopword_check_only_applies_to_concepts():
    assert not is_valid_entity_name("March", "concept")
    assert is_valid_entity_name("March", "person")
    assert not is_valid_entity_name("Al", "person")


def test_heuristic_extract_discards_stopwords():
    result = heuristic_extract("Please tell me about Marcus.", "However, Marcus manages Atlas. Certainly! On Monday.")
    names = {e["name"] for e in result["entities"]}
    assert {"Marcus", "Atlas"} <= names
    assert not names & {"However", "Please", "Certainly", "Monday"}


def test_distillation_rejects_stopword_entities(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            def extractor(q, r):
                return {
                    "entities": [{"name": "However"}, {"name": "Please"}, {"name": "Atlas"}],
                    "edges": [{"source": "However", "target": "Atlas", "relation": "uses"}],
                }

            await DistillationEngine(db, extractor).process_exchange("s", "Please help", "However, Atlas.")
            async with db.conn.execute("SELECT name FROM entities") as cur:
                names = {r[0] for r in await cur.fetchall()}
            async with db.conn.execute("SELECT COUNT(*) FROM edges") as cur:
                edges = (await cur.fetchone())[0]
            return names, edges

    names, edges = asyncio.run(scenario())
    # Atlas is model_inferred and its only edge was rejected (stop-word endpoint), so it is edge-free and dropped.
    assert names == set()
    assert edges == 0


def test_resolver_does_not_match_stopword_entities(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("However", "concept", "noise")
            await db.upsert_entity("Atlas", "project", "real")
            return [e.name for e in await ContextResolver(db).match_entities("However, tell me about Atlas")]

    assert asyncio.run(scenario()) == ["Atlas"]
