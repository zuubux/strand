import asyncio
import sqlite3

import pytest
import sqlite_vec

from cortext.cli import main
from cortext.db import EMBEDDING_DIM, DatabaseManager


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "cli.db"

    async def seed():
        async with DatabaseManager(path) as db:
            alice = await db.upsert_entity(
                "Alice", "person", "Lead dev", {"aliases": ["Al"]}, embedding=[0.1] * EMBEDDING_DIM
            )
            proj = await db.upsert_entity("Atlas", "project", "Main project")
            guess = await db.upsert_entity(
                "Maybe", "concept", "Dubious", origin="model_inferred", confidence=0.3
            )
            await db.upsert_edge(alice, proj, "works_on", weight=2.0)
            await db.upsert_edge(proj, guess, "mentions", origin="model_inferred", confidence=0.3)
            await db.record_episode("s1", "talked about Atlas", [alice, proj])

    asyncio.run(seed())
    return path


def _run(path, capsys, *argv):
    code = main(["--db", str(path), *argv])
    return code, capsys.readouterr()


def _count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_stats(seeded, capsys):
    code, out = _run(seeded, capsys, "stats")
    assert code == 0
    lines = out.out.splitlines()
    assert lines == [
        "entities: 3",
        "  user_explicit: 2",
        "  model_inferred: 1",
        "edges: 2",
        "episodes: 1",
    ]


def test_list_and_archetype_filter(seeded, capsys):
    code, out = _run(seeded, capsys, "list")
    assert code == 0
    assert out.out.splitlines()[0].split() == ["ID", "NAME", "ARCHETYPE", "CONF", "ORIGIN", "EDGES"]
    assert "Alice" in out.out and "Atlas" in out.out and "Maybe" in out.out
    atlas_row = next(line for line in out.out.splitlines() if "Atlas" in line)
    assert atlas_row.split() == ["2", "Atlas", "project", "1.00", "user_explicit", "2"]

    code, out = _run(seeded, capsys, "list", "--archetype", "person")
    assert "Alice" in out.out and "Atlas" not in out.out

    code, out = _run(seeded, capsys, "list", "--limit", "1")
    assert len(out.out.splitlines()) == 2

    code, out = _run(seeded, capsys, "list", "--archetype", "vehicle")
    assert out.out.strip() == "no entities found"


def test_inspect_shows_one_hop_edges(seeded, capsys):
    code, out = _run(seeded, capsys, "inspect", "Atlas")
    assert code == 0
    text = out.out
    assert "Main project" in text
    assert "outgoing edges (1)" in text and "incoming edges (1)" in text
    assert "-[mentions]-> Maybe  weight=1.00 origin=model_inferred confidence=0.30" in text
    assert "<-[works_on]- Alice  weight=2.00 origin=user_explicit confidence=1.00" in text

    code, out = _run(seeded, capsys, "inspect", "1")
    assert code == 0
    assert "aliases:    Al" in out.out


def test_inspect_missing(seeded, capsys):
    code, out = _run(seeded, capsys, "inspect", "nobody")
    assert code == 1
    assert "not found" in out.err


def test_drop_cascades_edges(seeded, capsys):
    code, out = _run(seeded, capsys, "drop", "Atlas", "--yes")
    assert code == 0
    assert "2 edge(s)" in out.out
    assert _count(seeded, "entities") == 2
    assert _count(seeded, "edges") == 0
    assert _count(seeded, "episodes") == 1


def test_drop_removes_embedding(seeded, capsys):
    conn = sqlite3.connect(seeded)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    assert conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0] == 1
    conn.close()

    _run(seeded, capsys, "drop", "1", "--yes")

    conn = sqlite3.connect(seeded)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0] == 0
    finally:
        conn.close()


def test_drop_requires_confirmation(seeded, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code, out = _run(seeded, capsys, "drop", "Atlas")
    assert code == 1
    assert "aborted" in out.out
    assert _count(seeded, "entities") == 3

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    code, _ = _run(seeded, capsys, "drop", "Atlas")
    assert code == 0
    assert _count(seeded, "entities") == 2


def test_prune_stale(seeded, capsys):
    code, out = _run(seeded, capsys, "prune-stale")
    assert code == 0
    assert "pruned 1 entit" in out.out and "1 edge(s)" in out.out
    assert _count(seeded, "entities") == 2
    assert _count(seeded, "edges") == 1


def test_prune_keeps_user_explicit_and_connected(seeded, capsys):
    code, out = _run(seeded, capsys, "prune-stale", "--confidence-below", "0.1")
    assert "pruned 0 entit" in out.out
    assert _count(seeded, "entities") == 3
    assert _count(seeded, "edges") == 2


def test_db_path_from_env(seeded, capsys, monkeypatch):
    monkeypatch.setenv("CORTEXT_DB_PATH", str(seeded))
    assert main(["stats"]) == 0
    assert "entities: 3" in capsys.readouterr().out
