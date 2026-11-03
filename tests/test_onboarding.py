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


def test_directive_text():
    text = onboarding_directive(HOST)
    assert text.startswith("[Onboarding Directive: Grounding & Resonance]")
    assert "Ambient Host Context: user operator, host workstation-01" in text
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


def test_header_triggers_onboarding_directive_without_seeding_from_user(tmp_path, pinned_host):
    seen, names = _post(
        tmp_path, {"X-Strand-Mode": "onboarding", "X-Strand-Host-Telemetry": "true"}, with_person=True
    )
    system = _system_text(seen[0])
    assert "[Onboarding Directive: Grounding & Resonance]" in system
    assert "host workstation-01, Fedora Linux 44" in system
    assert "Intent Triage" in system
    assert set(names) == {"Marcus"}  # the $USER account never becomes a person node


def test_day_zero_flags_first_session_without_seeding(tmp_path, pinned_host):
    seen, names = _post(tmp_path)
    assert "[Onboarding Directive" in _system_text(seen[0])
    assert "Operator" not in names


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
        return httpx.Response(200, content=sse("Hello, ", "Operator."))

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


def test_onboarding_has_no_procedural_name_anchor():
    import strand.onboarding as onboarding

    assert not hasattr(onboarding, "anchor_operator_name")
    assert onboarding.OPENING_LINE == "Hi, I'm Strand. What should I call you?"
    text = onboarding.onboarding_directive({"hostname": "workstation-01"})
    assert onboarding.OPENING_LINE in text and "Do not reflexively default to the local account username ($USER)" in text
    assert "you may conversationally suggest or offer a cleaned-up name" in text and "'whatever'" in text


def test_telemetry_gate_directive_offers_opt_in_on_turn_four():
    from strand.onboarding import TELEMETRY_DECLINED_ACK, TELEMETRY_GATE_QUESTION

    for turn in (4, 5):
        text = onboarding_directive(HOST, first_turn=False, turn=turn)
        assert TELEMETRY_GATE_QUESTION in text
        assert "Should I check out your machine specs, or keep that strictly on-demand?" in text
        assert "I don't review your local computer's details unless you want me to." in text
        assert TELEMETRY_DECLINED_ACK in text and "machine specs are now active" in text


def test_telemetry_gate_absent_on_turns_one_to_three_and_hostname_note():
    assert "hardware" not in onboarding_directive(HOST, first_turn=True, turn=1)
    for turn in (2, 3):
        text = onboarding_directive(HOST, first_turn=False, turn=turn)
        assert "Telemetry Gate" not in text
        assert "Looks like I'm running locally on workstation-01" in text
        assert "strip out sensitive info before anything goes out" in text
        assert "What do you usually find yourself working on, creating, or exploring at this computer?" in text


def test_telemetry_gate_resolved_does_not_reask():
    from strand.onboarding import TELEMETRY_GATE_QUESTION

    for decision in (True, False):
        text = onboarding_directive(HOST, first_turn=False, turn=5, telemetry_decision=decision)
        assert TELEMETRY_GATE_QUESTION not in text and "Telemetry Gate (resolved)" in text


def test_directive_has_no_cpu_ram_tokens_when_telemetry_disabled():
    from strand.host import redact_host_context

    redacted = redact_host_context(HOST)
    for turn in (1, 2, 4):
        text = onboarding_directive(redacted, first_turn=turn <= 1, turn=turn)
        for token in ("Test CPU", "32 GB", "Fedora", "KDE", "wayland"):
            assert token not in text


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


def test_is_onboarding_completed_requires_operator_and_telemetry_choice(tmp_path):
    from strand.onboarding import is_onboarding_completed

    async def go():
        async with DatabaseManager(tmp_path / "m.db") as db:
            states = [await is_onboarding_completed(db)]
            alice = await db.upsert_entity(
                "Alice", "person", "Operator", origin="user_explicit", confidence=1.0
            )
            states.append(await is_onboarding_completed(db))  # operator only
            on = await db.upsert_entity("Host Telemetry On-Demand", "preference", "x")
            await db.upsert_edge(alice, on, "prefers")
            states.append(await is_onboarding_completed(db))  # both
        async with DatabaseManager(tmp_path / "n.db") as db:
            bob = await db.upsert_entity("Bob", "person", "x", origin="model_inferred", confidence=0.6)
            on = await db.upsert_entity("Host Telemetry On-Demand", "preference", "x")
            await db.upsert_edge(bob, on, "prefers")
            states.append(await is_onboarding_completed(db))  # telemetry only, no explicit operator
        return states

    assert run(go()) == [False, False, True, False]


def test_directive_carries_task_yield_and_question_budget():
    from strand.onboarding import QUESTION_BUDGET_RULE, TASK_YIELD_RULE

    for kwargs in ({}, {"turn": 2}, {"turn": 4}):
        text = onboarding_directive(HOST, **kwargs)
        assert TASK_YIELD_RULE in text and QUESTION_BUDGET_RULE in text
    assert "TASK-YIELD CONSTRAINT: When the operator provides an imperative command" in TASK_YIELD_RULE
    assert "Default question budget = 0." in QUESTION_BUDGET_RULE


def test_telemetry_agreement_acknowledges_specs_and_yields():
    text = onboarding_directive(HOST, turn=4)
    assert "'sure', 'yes', 'go ahead'" in text
    assert "hostname, RAM, cores" in text
    assert "no follow-up question" in text
