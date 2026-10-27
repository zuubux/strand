import asyncio
import logging
import sys

import pytest
import sqlite_vec

from cortext import cli, daemon, embed
from cortext.config import Config
from cortext.db import DatabaseManager
from cortext.embed import EmbeddingManager, EmbeddingUnavailable
from cortext.resolver import ContextResolver
from tests.test_embed import MockEmbeddingManager, unit

FASTEMBED_WARNING = "fastembed unavailable; falling back to lexical-only matching."
VEC_WARNING = "sqlite-vec extension failed to load; vector indexing disabled."


def run(coro):
    return asyncio.run(coro)


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def warnings(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == logging.WARNING]


@pytest.fixture
def logs():
    logger = logging.getLogger("cortext")
    handler = _Collector()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(old_level)


@pytest.fixture
def no_vec(monkeypatch):
    """Make the sqlite-vec shared library impossible to load."""
    monkeypatch.setattr(sqlite_vec, "loadable_path", lambda: "/nonexistent/vec0")


class RaisingEmbedder:
    def embed(self, text):
        raise EmbeddingUnavailable(FASTEMBED_WARNING)


# --- embedder fallback -------------------------------------------------------


def test_missing_fastembed_logs_and_manager_raises_unavailable(monkeypatch, logs):
    monkeypatch.setitem(sys.modules, "fastembed", None)  # `import fastembed` -> ImportError
    manager = EmbeddingManager()
    with pytest.raises(EmbeddingUnavailable):
        manager.embed("hello")
    with pytest.raises(EmbeddingUnavailable):
        manager.embed("again")
    assert logs.warnings() == [FASTEMBED_WARNING]  # warned once, no retry storm


def test_model_download_failure_is_friendly(logs):
    def factory(name):
        raise OSError("offline: cannot download weights")

    with pytest.raises(EmbeddingUnavailable):
        EmbeddingManager(model_factory=factory).embed("x")
    assert logs.warnings() == [FASTEMBED_WARNING]


def test_create_embedder_returns_none_without_fastembed(monkeypatch, logs):
    monkeypatch.setattr(embed, "fastembed_available", lambda: False)
    assert embed.create_embedder() is None
    assert logs.warnings() == [FASTEMBED_WARNING]


def test_create_embedder_is_lazy_when_available(monkeypatch, logs):
    monkeypatch.setattr(embed, "fastembed_available", lambda: True)
    manager = embed.create_embedder()
    assert isinstance(manager, EmbeddingManager) and not manager.loaded
    assert logs.warnings() == []


def test_build_app_boots_with_embedder_none(tmp_path, monkeypatch, logs):
    monkeypatch.setattr(embed, "fastembed_available", lambda: False)
    captured = {}
    original = daemon.ContextResolver

    class Spy(original):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(daemon, "ContextResolver", Spy)
    assert daemon.build_app(Config(db_path=str(tmp_path / "m.db"))) is not None
    assert captured["embedder"] is None
    assert FASTEMBED_WARNING in logs.warnings()


def test_resolver_falls_back_to_lexical_when_embedder_fails(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await db.upsert_entity("Marcus", "person", "Owner", embedding=unit(1))
            resolver = ContextResolver(db, embedder=RaisingEmbedder())
            lexical = [e.name for e in await resolver.match_entities("ask Marcus")]
            semantic_only = await resolver.match_entities("my automobile")
            return lexical, semantic_only

    lexical, semantic_only = run(go())
    assert lexical == ["Marcus"]
    assert semantic_only == []


def test_upsert_with_failing_embedder_still_writes(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db", embedder=RaisingEmbedder()) as db:
            entity_id = await db.upsert_entity("Atlas", "project", "Main")
            async with db.conn.execute("SELECT name FROM entities WHERE id = ?", (entity_id,)) as cur:
                return (await cur.fetchone())[0]

    assert run(go()) == "Atlas"


# --- sqlite-vec fallback -----------------------------------------------------


def test_vec_load_failure_logs_and_relational_ops_work(tmp_path, no_vec, logs):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            a = await db.upsert_entity("Alice", "person", "Lead dev")
            b = await db.upsert_entity("Atlas", "project", "Main")
            await db.upsert_edge(a, b, "works_on")
            await db.record_episode("s1", "talked about Atlas", [a, b])
            graph = await db.get_entity_subgraph(a)
            async with db.conn.execute("PRAGMA journal_mode") as cur:
                mode = (await cur.fetchone())[0]
            return db.vec_enabled, graph, mode

    enabled, graph, mode = run(go())
    assert enabled is False
    assert [g["relation"] for g in graph] == ["works_on"]
    assert mode == "wal"
    assert logs.warnings() == [VEC_WARNING]


def test_vec_disabled_ignores_embeddings_and_falls_back_to_lexical(tmp_path, no_vec, logs):
    mock = MockEmbeddingManager({"my automobile": 0})

    async def go():
        async with DatabaseManager(tmp_path / "m.db", embedder=mock) as db:
            await db.upsert_entity("Corvette", "vehicle", "Fast car", embedding=unit(0))
            await db.upsert_entity("Marcus", "person", "Owner")  # auto-embed skipped
            similar = await db.find_similar_entities(unit(0))
            resolver = ContextResolver(db, embedder=mock)
            return (
                similar,
                [e.name for e in await resolver.match_entities("my automobile")],
                [e.name for e in await resolver.match_entities("call Marcus")],
            )

    similar, semantic_query, lexical_query = run(go())
    assert similar == []
    assert semantic_query == []
    assert lexical_query == ["Marcus"]
    assert mock.calls == []  # no write-time or query-time embedding without sqlite-vec
    assert logs.warnings() == [VEC_WARNING]


def _seed_alice(path):
    async def seed():
        async with DatabaseManager(path) as db:
            await db.upsert_entity("Alice", "person", "Lead dev")

    run(seed())


def test_vec_disabled_cli_drop_and_backfill(tmp_path, no_vec, capsys):
    path = tmp_path / "cli.db"
    _seed_alice(path)
    assert cli.main(["--db", str(path), "embed-backfill"]) == 1
    assert "sqlite-vec is unavailable" in capsys.readouterr().err
    assert cli.main(["--db", str(path), "drop", "Alice", "--yes"]) == 0


def test_embed_backfill_reports_unavailable_embedder(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.db"
    _seed_alice(path)
    monkeypatch.setattr(cli, "EmbeddingManager", RaisingEmbedder)
    assert cli.main(["--db", str(path), "embed-backfill"]) == 1
    assert FASTEMBED_WARNING in capsys.readouterr().err


# --- cortext doctor ----------------------------------------------------------


@pytest.fixture
def doctor_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "CORTEXT_HOST",
        "CORTEXT_PORT",
        "CORTEXT_SOCKET_PATH",
        "CORTEXT_EXTRACTOR_MODEL",
        "CORTEXT_EXTRACTOR_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "fe_cache"))
    pings = []

    async def fake_ping(url):
        pings.append(url)
        return False

    monkeypatch.setattr(cli, "_ping", fake_ping)
    return tmp_path, pings


def test_doctor_healthy_checklist(doctor_env, monkeypatch, capsys):
    tmp_path, pings = doctor_env
    (tmp_path / "fe_cache" / "models--qdrant--bge-small-en-v1.5-onnx-q").mkdir(parents=True)
    monkeypatch.setattr(cli, "fastembed_available", lambda: True)

    async def reachable(url):
        pings.append(url)
        return True

    monkeypatch.setattr(cli, "_ping", reachable)
    db_path = tmp_path / "doc.db"
    assert cli.main(["--db", str(db_path), "doctor"]) == 0
    out = capsys.readouterr().out
    assert f"{db_path} (journal_mode=wal)" in out
    assert "sqlite-vec: loaded" in out
    assert "fastembed: installed" in out
    assert "model cache: BAAI/bge-small-en-v1.5 cached at" in out
    assert "extractor SLM: http://127.0.0.1:11434/v1" in out and "reachable" in out
    assert f"transport: uds {tmp_path}/run/cortext/cortext.sock" in out
    assert pings == ["http://127.0.0.1:11434/v1/models"]
    assert "[fail]" not in out


def test_doctor_reports_degraded_components(doctor_env, no_vec, monkeypatch, capsys):
    tmp_path, _ = doctor_env
    monkeypatch.setattr(cli, "fastembed_available", lambda: False)
    monkeypatch.setenv("CORTEXT_HOST", "0.0.0.0")
    monkeypatch.setenv("CORTEXT_PORT", "9000")
    monkeypatch.setenv("CORTEXT_EXTRACTOR_MODEL", "qwen")
    assert cli.main(["--db", str(tmp_path / "doc.db"), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "[fail] sqlite-vec: not loaded; vector indexing disabled" in out
    assert "[warn] fastembed: not installed" in out and FASTEMBED_WARNING in out
    assert "[fail] extractor SLM:" in out and "(model qwen) unreachable" in out
    assert "transport: tcp 0.0.0.0:9000" in out


def test_ping_uses_half_second_timeout():
    assert cli.PING_TIMEOUT == 0.5
    assert run(cli._ping("http://127.0.0.1:9/")) is False  # closed local port, offline

