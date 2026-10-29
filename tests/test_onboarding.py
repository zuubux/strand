import asyncio
import json
import os
import time

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from strand import host as host_mod  # noqa: E402
from strand.config import Config  # noqa: E402
from strand.db import DatabaseManager  # noqa: E402
from strand.host import format_host_context, get_host_context  # noqa: E402
from strand.onboarding import ONBOARD_TRIGGER, onboarding_directive, seed_operator_anchor  # noqa: E402
from strand.proxy import create_app  # noqa: E402
from strand.resolver import ContextResolver  # noqa: E402
from strand.session import SessionLedger  # noqa: E402
from tests.test_ui import make_bridge, sse  # noqa: E402

HOST = {
    "user": "nic",
    "hostname": "ONYX",
    "os": "Fedora Linux 44",
    "desktop": "KDE",
    "session": "wayland",
    "cpu": "Test CPU",
    "ram_gb": 32.0,
}
KEYS = {"user", "hostname", "os", "desktop", "session", "cpu", "ram_gb"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


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
    assert "ONYX" in text and "Fedora Linux 44" in text and "KDE / wayland" in text and "32 GB RAM" in text
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
    assert [(e[0], e[1]) for e in entities] == [("Nic", "person"), ("ONYX", "system")]
    assert "User" in json.loads(entities[0][3])["aliases"]
    assert entities[0][2] == "Primary operator of this workstation"
    assert entities[1][2] == "Primary workstation running Fedora Linux 44"
    assert edges == [(first[0], first[1], "operates_host", 1.0, 1.0, "user_explicit")]


def test_seed_operator_anchor_keeps_existing_operator(tmp_path):
    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            nic = await db.upsert_entity("Nic", "person", "Custom summary")
            user_id, host_id = await seed_operator_anchor(db, HOST)
            again = await seed_operator_anchor(db, HOST)
            async with db.conn.execute("SELECT summary FROM entities WHERE id = ?", (nic,)) as cur:
                summary = (await cur.fetchone())[0]
            return nic, user_id, host_id, again, summary

    nic, user_id, host_id, again, summary = run(go())
    assert user_id == nic and again == (user_id, host_id)
    assert summary == "Custom summary"


def test_directive_text():
    text = onboarding_directive(HOST)
    assert text.startswith("[Onboarding Directive: Grounding & Resonance]")
    assert "Ambient Host Context: user nic, host ONYX" in text
    assert "exactly ONE question per turn" in text


@pytest.fixture
def pinned_host(monkeypatch):
    import strand.proxy as proxy

    monkeypatch.setattr(proxy, "get_host_context", lambda: dict(HOST))


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


def test_header_triggers_onboarding_directive_and_seeds(tmp_path, pinned_host):
    seen, names = _post(tmp_path, {"X-Strand-Mode": "onboarding"}, with_person=True)
    system = _system_text(seen[0])
    assert "[Onboarding Directive: Grounding & Resonance]" in system
    assert "host ONYX, Fedora Linux 44" in system
    assert {"Nic", "ONYX"} <= set(names)


def test_day_zero_flags_first_session_without_seeding(tmp_path, pinned_host):
    seen, names = _post(tmp_path)
    assert "[Onboarding Directive" in _system_text(seen[0])
    assert "Nic" not in names


def test_no_directive_when_person_exists_and_no_header(tmp_path, pinned_host):
    seen, _ = _post(tmp_path, with_person=True)
    assert "[Onboarding Directive" not in _system_text(seen[0])


def test_onboarding_session_persists_without_header(tmp_path, pinned_host):
    seen, _ = _post(tmp_path, {"X-Strand-Mode": "onboarding"}, with_person=True, times=2)
    assert len(seen) == 2
    assert all("[Onboarding Directive" in _system_text(r) for r in seen)


def test_onboard_command_resets_session_and_sends_trigger(qapp):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=sse("Hello, ", "Nic."))

    b = make_bridge(handler)
    old = b.sessionId
    b._history.append({"role": "user", "content": "stale"})
    resets = []
    b.conversationReset.connect(lambda: resets.append(1))
    assert b.runCommand("/onboard") == "Onboarding session started."
    assert b.sessionId != old and resets == [1]
    deadline = time.time() + 5
    while (b.busy or not requests) and time.time() < deadline:
        time.sleep(0.01)
    assert len(requests) == 1
    request = requests[0]
    assert request.headers["x-strand-mode"] == "onboarding"
    assert request.headers["x-session-id"] == b.sessionId
    assert json.loads(request.content)["messages"] == [{"role": "user", "content": ONBOARD_TRIGGER}]


def test_new_command_clears_onboarding_mode(qapp):
    b = make_bridge(lambda r: httpx.Response(200, content=sse("ok")))
    b._onboarding = True
    assert b.runCommand("/new")
    assert "X-Strand-Mode" not in b._headers()
