import asyncio
import json
import os

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from strand import host as host_mod  # noqa: E402
from strand.config import Config  # noqa: E402
from strand.db import DatabaseManager  # noqa: E402
from strand.host import format_host_context, get_host_context  # noqa: E402
from strand.onboarding import seed_operator_anchor  # noqa: E402
from strand.proxy import create_app  # noqa: E402
from strand.resolver import ContextResolver  # noqa: E402
from strand.session import SessionLedger  # noqa: E402

HOST = {
    "user": "operator",
    "hostname": "workstation-01",
    "os": "Fedora Linux 44",
    "desktop": "KDE",
    "session": "wayland",
    "cpu": "Test CPU",
    "ram_gb": 32.0,
}
KEYS = {"user", "hostname", "os", "desktop", "session", "cpu", "ram_gb"}


def run(coro):
    return asyncio.run(coro)


def test_host_context_keys_and_proc_parsing(tmp_path, monkeypatch):
    cpu = tmp_path / "cpuinfo"
    cpu.write_text("processor\t: 0\nmodel name\t: Fancy  CPU @ 5GHz\n")
    mem = tmp_path / "meminfo"
    mem.write_text("MemTotal:       16777216 kB\nMemFree: 1 kB\n")
    monkeypatch.setattr(host_mod.Path, "read_text", _fake_read({host_mod.PROC_CPUINFO: cpu, host_mod.PROC_MEMINFO: mem}))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    info = get_host_context()
    assert set(info) == KEYS
    assert info["cpu"] == "Fancy CPU @ 5GHz" and info["ram_gb"] == 16.0
    assert info["desktop"] == "KDE" and info["session"] == "wayland"
    assert info["user"] and info["hostname"] and info["os"]


def _fake_read(mapping):
    real = host_mod.Path.read_text

    def read_text(self, *args, **kwargs):
        target = mapping.get(str(self))
        return real(target, *args, **kwargs) if target is not None else real(self, *args, **kwargs)

    return read_text


def test_host_context_graceful_without_proc(tmp_path):
    missing = str(tmp_path / "nope")
    assert host_mod._cpu_model(missing) == ""
    assert host_mod._total_ram_gb(missing) == 0.0
    bad = tmp_path / "meminfo"
    bad.write_text("MemTotal: lots kB\n")
    assert host_mod._total_ram_gb(str(bad)) == 0.0


def test_host_os_falls_back_to_platform(monkeypatch):
    def boom():
        raise OSError("no os-release")

    monkeypatch.setattr(host_mod.platform, "freedesktop_os_release", boom)
    monkeypatch.setattr(host_mod.platform, "platform", lambda: "Plat-1")
    assert host_mod._os_release() == "Plat-1"


def test_format_host_context():
    text = format_host_context(HOST)
    assert "workstation-01" in text and "Fedora Linux 44" in text and "KDE / wayland" in text and "32 GB RAM" in text
    assert format_host_context({}) == "unknown host"


def test_seed_operator_anchor_creates_and_is_idempotent(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            first = await seed_operator_anchor(db, HOST)
            second = await seed_operator_anchor(db, HOST)
            async with db.conn.execute("SELECT name, archetype, summary, metadata FROM entities ORDER BY id") as cur:
                entities = await cur.fetchall()
            async with db.conn.execute(
                "SELECT source_id, target_id, relation, weight, confidence, origin FROM edges"
            ) as cur:
                edges = [tuple(r) for r in await cur.fetchall()]
            return first, second, entities, edges

    first, second, entities, edges = run(go())
    assert first == second
    assert [(e[0], e[1]) for e in entities] == [("Operator", "person"), ("workstation-01", "system")]
    assert "User" in json.loads(entities[0][3])["aliases"]
    assert entities[0][2] == "Primary operator of this workstation"
    assert entities[1][2] == "Primary workstation running Fedora Linux 44"
    assert edges == [(first[0], first[1], "operates_host", 1.0, 1.0, "user_explicit")]


def test_seed_operator_anchor_keeps_existing_operator(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            operator = await db.upsert_entity("Operator", "person", "Custom summary")
            user_id, host_id = await seed_operator_anchor(db, HOST)
            again = await seed_operator_anchor(db, HOST)
            async with db.conn.execute("SELECT summary FROM entities WHERE id = ?", (operator,)) as cur:
                summary = (await cur.fetchone())[0]
            return operator, user_id, host_id, again, summary

    operator, user_id, host_id, again, summary = run(go())
    assert user_id == operator and again == (user_id, host_id)
    assert summary == "Custom summary"


def _post(tmp_path, headers=None, with_person=False, times=1):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    async def go():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with DatabaseManager(tmp_path / "m.db") as db:
            if with_person:
                await db.upsert_entity("Marcus", "person", "Staff engineer")
            resolver = ContextResolver(db, SessionLedger())
            app = create_app(Config(upstream_url="http://upstream.test/v1"), resolver, upstream)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://strand") as client:
                for _ in range(times):
                    payload = {"messages": [{"role": "user", "content": "hello there"}], "session_id": "s1"}
                    await client.post("/v1/chat/completions", json=payload, headers=headers or {})
                await asyncio.gather(*app.state.background_tasks)
            await upstream.aclose()
            async with db.conn.execute("SELECT name FROM entities ORDER BY id") as cur:
                return [r[0] for r in await cur.fetchall()]

    names = run(go())
    return seen, names


def _system_text(request):
    messages = json.loads(request.content)["messages"]
    return messages[0]["content"] if messages[0]["role"] == "system" else ""


def test_fresh_session_has_no_onboarding_directive(tmp_path):
    for with_person in (False, True):
        seen, names = _post(tmp_path / str(with_person), {"X-Strand-Mode": "onboarding"}, with_person=with_person)
        system = _system_text(seen[0])
        assert "Onboarding Directive" not in system and "What should I call you?" not in system
        assert set(names) == ({"Marcus"} if with_person else set())


def test_telemetry_preference_reads_latest_active_edge(tmp_path):
    from strand.onboarding import telemetry_preference

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            assert await telemetry_preference(db) is None
            alice = await db.upsert_entity("Alice", "person", "Operator")
            on = await db.upsert_entity("Host Telemetry On-Demand", "preference", "x")
            await db.upsert_edge(alice, on, "prefers")
            first = await telemetry_preference(db)
            yes = await db.upsert_entity("Host Telemetry Enabled", "preference", "x")
            await db.upsert_edge(alice, yes, "prefers")
            return first, await telemetry_preference(db)

    assert run(go()) == (False, True)


def test_telemetry_preference_rejects_model_inferred_enable_edge(tmp_path):
    from strand.onboarding import telemetry_preference

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            alice = await db.upsert_entity("Alice", "person", "Operator")
            yes = await db.upsert_entity("Host Telemetry Enabled", "preference", "x")
            await db.upsert_edge(alice, yes, "prefers", origin="model_inferred", confidence=0.6)
            inferred = await telemetry_preference(db)
            # even a user_explicit edge below full confidence is not enough
            await db.upsert_edge(alice, yes, "prefers", origin="user_explicit", confidence=0.9)
            weak = await telemetry_preference(db)
            await db.upsert_edge(alice, yes, "prefers", origin="user_explicit", confidence=1.0)
            return inferred, weak, await telemetry_preference(db)

    assert run(go()) == (None, None, True)


def test_telemetry_preference_requires_authenticated_operator_source(tmp_path):
    from strand.onboarding import telemetry_preference

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            ghost = await db.upsert_entity(
                "Mallory", "person", "Injected", origin="model_inferred", confidence=0.6
            )
            yes = await db.upsert_entity("Host Telemetry Enabled", "preference", "x")
            await db.upsert_edge(ghost, yes, "prefers", origin="user_explicit", confidence=1.0)
            return await telemetry_preference(db)

    assert run(go()) is None
