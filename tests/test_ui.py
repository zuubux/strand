import asyncio
import json
import os

import httpx
import pytest
from PySide6.QtCore import QCoreApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from strand.config import Config  # noqa: E402
from strand.ui import bridge as bridge_mod  # noqa: E402
from strand.ui.bridge import Bridge, parse_sse_line, resolve_endpoint, status_label  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() or QGuiApplication([])


def sse(*tokens, done=True):
    out = "".join(
        "data: " + json.dumps({"choices": [{"delta": {"content": t}}]}, ensure_ascii=False) + "\n\n" for t in tokens
    )
    return (out + ("data: [DONE]\n\n" if done else "")).encode("utf-8")


def make_bridge(handler, config=None):
    factory = lambda: httpx.AsyncClient(  # noqa: E731
        transport=httpx.MockTransport(handler), base_url="http://strand"
    )
    return Bridge(config=config or Config(), model="test-model", client_factory=factory)


def collect(b):
    events = []
    b.chunkReceived.connect(lambda t: events.append(("chunk", t)))
    b.streamFinished.connect(lambda: events.append(("done", None)))
    b.errorOccurred.connect(lambda m: events.append(("error", m)))
    return events


def test_parse_sse_line():
    assert parse_sse_line('data: {"choices":[{"delta":{"content":"hi"}}]}') == ("token", "hi")
    assert parse_sse_line("data: [DONE]") == ("done", "")
    assert parse_sse_line(": keepalive") == ("skip", "")
    assert parse_sse_line("data: not-json") == ("skip", "")
    assert parse_sse_line('data: {"choices":[{"delta":{"role":"assistant"}}]}') == ("skip", "")
    assert parse_sse_line('data: {"error":{"message":"overloaded"}}') == ("error", "overloaded")


def test_resolve_endpoint_uses_existing_socket(tmp_path):
    import socket

    path = str(tmp_path / "c.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    try:
        uds, base = resolve_endpoint(Config(socket_path=path))
    finally:
        srv.close()
    assert (uds, base) == (path, "http://strand")
    assert status_label(uds, base) == "Connected: c.sock"


def test_resolve_endpoint_xdg_default_and_tcp_fallback(tmp_path):
    sock = tmp_path / "strand" / "strand.sock"
    config = Config()
    assert config.effective_socket_path({"XDG_RUNTIME_DIR": str(tmp_path)}) == str(sock)
    # Missing socket file -> loopback TCP fallback.
    uds, base = resolve_endpoint(config, {"XDG_RUNTIME_DIR": str(tmp_path)})
    assert uds is None and base == "http://127.0.0.1:8088"
    assert status_label(uds, base) == "Connected: 127.0.0.1:8088"


def test_make_client_selects_uds_transport(tmp_path, monkeypatch):
    b = Bridge(config=Config(), model="m")
    b._uds_path, b._base_url = str(tmp_path / "x.sock"), "http://strand"
    seen = {}
    real = httpx.AsyncHTTPTransport

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(bridge_mod.httpx, "AsyncHTTPTransport", spy)
    client = b.make_client()
    assert seen == {"uds": str(tmp_path / "x.sock")}
    asyncio.run(client.aclose())


def test_streams_chunks_and_keeps_session_and_history():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=sse("Hel", "lo"), headers={"content-type": "text/event-stream"})

    b = make_bridge(handler, Config(api_key="k"))
    events = collect(b)
    for text in ("first", "second"):
        b._history.append({"role": "user", "content": text})
        asyncio.run(b._stream(b.history))
    assert events == [("chunk", "Hel"), ("chunk", "lo"), ("done", None)] * 2
    bodies = [json.loads(r.content) for r in requests]
    assert {x["session_id"] for x in bodies} == {b.sessionId}
    assert requests[0].headers["x-session-id"] == b.sessionId
    assert requests[0].headers["authorization"] == "Bearer k"
    assert requests[0].url.path == "/v1/chat/completions"
    assert bodies[0]["stream"] is True and bodies[0]["model"] == "test-model"
    assert [m["content"] for m in bodies[1]["messages"]] == ["first", "Hello", "second"]


def test_chunk_split_across_network_reads():
    data = sse("héllo")
    cut = data.index("é".encode()) + 1  # split inside the multi-byte character

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data[:cut]
            yield data[cut:]

    b = make_bridge(lambda r: httpx.Response(200, stream=Stream()))
    events = collect(b)
    asyncio.run(b._stream([{"role": "user", "content": "x"}]))
    assert events == [("chunk", "héllo"), ("done", None)]


def test_http_error_emits_error_and_rolls_back_history():
    b = make_bridge(lambda r: httpx.Response(500, text="boom"))
    events = collect(b)
    b._history.append({"role": "user", "content": "hi"})
    asyncio.run(b._stream(b.history))
    assert events == [("error", "HTTP 500: boom")]
    assert b.history == []


def test_connect_error_marks_disconnected():
    def handler(request):
        raise httpx.ConnectError("no socket")

    b = make_bridge(handler)
    events = collect(b)
    asyncio.run(b._stream([{"role": "user", "content": "x"}]))
    assert events[0][0] == "error" and "no socket" in events[0][1]
    assert b.status == "Disconnected"


def test_in_stream_error_event():
    body = sse("par", done=False) + b'data: {"error":{"message":"overloaded"}}\n\n'
    b = make_bridge(lambda r: httpx.Response(200, content=body))
    events = collect(b)
    asyncio.run(b._stream([{"role": "user", "content": "x"}]))
    assert events == [("chunk", "par"), ("error", "overloaded")]


def test_send_message_dispatches_on_worker_thread(qapp):
    b = make_bridge(lambda r: httpx.Response(200, content=sse("ok")))
    events = collect(b)
    b.sendMessage("   ")  # ignored
    assert not b.busy
    threads = []
    orig = b._run_async
    b._run_async = lambda *a, **k: threads.append(orig(*a, **k)) or threads[-1]
    b.sendMessage("hello")
    threads[0].join(5)
    QCoreApplication.processEvents()  # deliver the queued cross-thread signals
    assert events == [("chunk", "ok"), ("done", None)]
    assert b.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "ok"},
    ]
    assert not b.busy


def test_check_connection_status():
    b = make_bridge(lambda r: httpx.Response(200, json={"status": "ok"}))
    asyncio.run(b._check())
    assert b.status.startswith("Connected: ")
    b2 = make_bridge(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))
    asyncio.run(b2._check())
    assert b2.status == "Disconnected"


def test_qml_loads_offscreen(qapp):
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle

    from strand.ui.app import MAIN_QML

    QQuickStyle.setStyle("Basic")
    b = make_bridge(lambda r: httpx.Response(200, json={}))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", b)
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    assert engine.rootObjects()
    assert engine.rootObjects()[0].property("title") == "Strand"
    engine.deleteLater()


# ---- model resolution: explicit > STRAND_UI_MODEL > config model_default > fallback ----


def test_resolve_model_precedence():
    from strand.ui.bridge import DEFAULT_MODEL, resolve_model

    cfg = Config(model_default="gemini-3.6-flash")
    assert resolve_model(cfg, {"STRAND_UI_MODEL": "env-m"}, explicit="arg-m") == "arg-m"
    assert resolve_model(cfg, {"STRAND_UI_MODEL": "env-m"}) == "env-m"
    assert resolve_model(cfg, {"STRAND_UI_MODEL": "  "}) == "gemini-3.6-flash"
    assert resolve_model(cfg, {}) == "gemini-3.6-flash"
    assert resolve_model(Config(), {}) == DEFAULT_MODEL


def test_bridge_uses_persisted_model_default(qapp, monkeypatch):
    monkeypatch.delenv("STRAND_UI_MODEL", raising=False)
    cfg = Config(model_default="gemini-3.6-flash")
    assert Bridge(config=cfg).modelName == "gemini-3.6-flash"
    monkeypatch.setenv("STRAND_UI_MODEL", "override")
    assert Bridge(config=cfg).modelName == "override"


# ---- slash commands, session reset, abort, input history, drawer -----------------------


def test_parse_command():
    from strand.ui.bridge import parse_command

    assert parse_command("/new") == ("new", "")
    assert parse_command("  /Private   ON ") == ("private", "ON")
    assert parse_command("/model gemini-3.6-flash") == ("model", "gemini-3.6-flash")
    assert parse_command("hello /new") is None
    assert parse_command("/etc/hosts is a file") is None  # paths are normal prompts
    assert parse_command("/") is None


def test_new_conversation_resets_session_and_history(qapp):
    b = make_bridge(lambda r: httpx.Response(200, content=sse("ok")))
    seen = []
    b.sessionChanged.connect(lambda: seen.append(b.sessionId))
    resets = []
    b.conversationReset.connect(lambda: resets.append(1))
    old = b.sessionId
    b._history.append({"role": "user", "content": "x"})
    b.newConversation()
    assert b.sessionId != old and b.sessionId.startswith("ui-")
    assert seen == [b.sessionId] and resets == [1]
    assert b.history == []
    assert b.diagnostics["sessionId"] == b.sessionId


def test_run_command_new_private_model(qapp):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=sse("ok"))

    b = make_bridge(handler)
    old = b.sessionId
    assert b.runCommand("/new") and b.sessionId != old
    assert b.runCommand("hello") == ""  # not a command: caller sends it upstream
    assert b.runCommand("/private") .startswith("Private mode is off")
    assert b.runCommand("/private maybe").startswith("Private mode is off")
    assert not b.privateMode

    asyncio.run(b._stream([{"role": "user", "content": "a"}]))
    assert "x-strand-allow-private" not in requests[-1].headers
    assert b.runCommand("/private on") and b.privateMode
    asyncio.run(b._stream([{"role": "user", "content": "a"}]))
    assert requests[-1].headers["x-strand-allow-private"] == "true"
    assert b.runCommand("/private OFF") and not b.privateMode
    asyncio.run(b._stream([{"role": "user", "content": "a"}]))
    assert "x-strand-allow-private" not in requests[-1].headers

    assert "test-model" in b.runCommand("/model")
    assert "gemini-x" in b.runCommand("/model  gemini-x ")
    assert b.modelName == "gemini-x"
    asyncio.run(b._stream([{"role": "user", "content": "a"}]))
    assert json.loads(requests[-1].content)["model"] == "gemini-x"
    assert "Unknown command /bogus" in b.runCommand("/bogus")
    assert len(requests) == 4  # commands never hit the transport


def test_commands_are_never_sent_upstream(qapp):
    requests = []
    b = make_bridge(lambda r: requests.append(r) or httpx.Response(200, content=sse("ok")))
    for text in ("/new", "/private on", "/model m2", "/help", "/nope"):
        assert b.runCommand(text)
    assert requests == [] and b.history == []


def test_input_history_navigation(qapp):
    b = make_bridge(lambda r: httpx.Response(200, content=sse("ok")))
    assert b.historyPrevious() == "" and b.historyNext() == ""
    for text in ("one", "two", "three"):
        b._remember(text)
    b._remember("three")  # consecutive duplicate ignored
    assert [b.historyPrevious() for _ in range(4)] == ["three", "two", "one", "one"]
    assert [b.historyNext() for _ in range(3)] == ["two", "three", ""]
    assert b.historyNext() == ""
    assert b.historyPrevious() == "three"  # restarts from the newest


def test_diagnostics_record_latest_turn(qapp):
    b = make_bridge(lambda r: httpx.Response(200, content=sse("Hel", "lo")))
    changed = []
    b.diagnosticsChanged.connect(lambda: changed.append(1))
    assert b.diagnostics["outcome"] == "" and b.diagnostics["promptChars"] == 0
    asyncio.run(b._stream([{"role": "user", "content": "hello!"}]))
    d = b.diagnostics
    assert d["promptChars"] == 6 and d["replyChars"] == 5 and d["outcome"] == "ok"
    assert d["latencyMs"] >= d["firstTokenMs"] >= 0
    assert d["model"] == "test-model" and d["transport"].startswith(("unix:", "tcp:"))
    assert changed
    asyncio.run(b._stream([{"role": "user", "content": "x"}]))
    b2 = make_bridge(lambda r: httpx.Response(500, text="boom"))
    asyncio.run(b2._stream([{"role": "user", "content": "x"}]))
    assert b2.diagnostics["outcome"] == "error"


def test_diagnostics_fiber_telemetry_metrics(qapp):
    b = make_bridge(lambda r: httpx.Response(200, content=sse("Hel", "lo", "!")))
    d = b.diagnostics
    assert d["throughput"] == "0.0 t/s" and d["ttft_ms"] == 0 and d["total_ms"] == 0
    asyncio.run(b._stream([{"role": "user", "content": "hello!"}]))
    d = b.diagnostics
    assert d["token_count"] == 3
    assert d["total_ms"] >= d["ttft_ms"] >= 0
    assert d["throughput"].endswith(" t/s") and float(d["throughput"].split()[0]) > 0


def test_diagnostics_transport_reports_mode(tmp_path):
    import socket as socketlib

    path = str(tmp_path / "m.sock")
    srv = socketlib.socket(socketlib.AF_UNIX)
    srv.bind(path)
    try:
        os.chmod(path, 0o600)
        b = Bridge(config=Config(socket_path=path), model="m")
        assert b.diagnostics["transport"] == f"unix:{path} (0600)"
    finally:
        srv.close()


def test_transport_property_reports_socket_path(tmp_path):
    import socket as socketlib

    path = str(tmp_path / "c.sock")
    srv = socketlib.socket(socketlib.AF_UNIX)
    srv.bind(path)
    try:
        b = Bridge(config=Config(socket_path=path), model="m")
        assert b.transport == f"unix:{path}"
    finally:
        srv.close()
    assert Bridge(config=Config(socket_path=str(tmp_path / "none")), model="m").transport.startswith("tcp:")


class _HangingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield sse("par", done=False)
        await asyncio.sleep(30)
        yield b""


def _wait(predicate, timeout=5.0):
    import time

    end = time.time() + timeout
    while time.time() < end and not predicate():
        QCoreApplication.processEvents()
        time.sleep(0.01)
    return predicate()


def test_abort_stops_stream_keeps_partial_and_frees_input(qapp):
    b = make_bridge(lambda r: httpx.Response(200, stream=_HangingStream()))
    events = collect(b)
    aborted = []
    b.streamAborted.connect(lambda: aborted.append(1))
    assert b.abort() is False  # nothing running
    b.sendMessage("hello")
    assert _wait(lambda: any(e[0] == "chunk" for e in events))
    assert b.busy and b.abort() is True
    assert _wait(lambda: not b.busy)
    QCoreApplication.processEvents()
    assert aborted == [1]
    assert not any(e[0] in ("done", "error") for e in events)
    assert b.history[-1] == {"role": "assistant", "content": "par"}
    assert b.diagnostics["outcome"] == "aborted"


def test_new_conversation_during_stream_aborts_and_keeps_new_session_clean(qapp):
    b = make_bridge(lambda r: httpx.Response(200, stream=_HangingStream()))
    events = collect(b)
    b.sendMessage("hello")
    assert _wait(lambda: any(e[0] == "chunk" for e in events))
    b.newConversation()
    assert _wait(lambda: not b.busy)
    QCoreApplication.processEvents()
    assert b.history == []  # the aborted turn did not leak into the new session


def test_split_markdown_code_fences():
    from strand.ui.bridge import split_markdown

    segs = split_markdown("Intro\n```python\nprint(1)\n```\nOutro")
    assert [(s["code"], s["lang"], s["text"]) for s in segs] == [
        (False, "", "Intro"),
        (True, "python", "print(1)"),
        (False, "", "Outro"),
    ]
    open_fence = split_markdown("a\n```\npartial")  # mid-stream: still a code block
    assert [(s["code"], s["text"]) for s in open_fence] == [(False, "a"), (True, "partial")]
    assert split_markdown("no code") == [{"code": False, "lang": "", "text": "no code"}]
    assert split_markdown("") == []


def test_geometry_roundtrip_with_qsettings(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    from strand.ui import app as app_mod

    class Rect:
        def intersects(self, other):
            return True

    class Screen:
        def availableGeometry(self):
            return Rect()

    class Win:
        def __init__(self, geom=(10, 20, 800, 600)):
            self.geom = geom

        x = lambda self: self.geom[0]  # noqa: E731
        y = lambda self: self.geom[1]  # noqa: E731
        width = lambda self: self.geom[2]  # noqa: E731
        height = lambda self: self.geom[3]  # noqa: E731
        minimumWidth = lambda self: 420  # noqa: E731
        minimumHeight = lambda self: 360  # noqa: E731
        screen = lambda self: Screen()  # noqa: E731

        def setGeometry(self, *geom):
            self.geom = geom

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    assert app_mod.restore_geometry(Win(), settings) is False  # nothing saved yet
    app_mod.save_geometry(Win((11, 22, 900, 700)), settings)
    reopened = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    fresh = Win((0, 0, 1, 1))
    assert app_mod.restore_geometry(fresh, reopened) is True
    assert fresh.geom == (11, 22, 900, 700)
    settings.setValue(app_mod.GEOMETRY_KEY, [0, 0, 10, 10])  # below the minimum size
    assert app_mod.restore_geometry(Win(), settings) is False
    settings.setValue(app_mod.GEOMETRY_KEY, "garbage")
    assert app_mod.restore_geometry(Win(), settings) is False


def test_size_persistence_roundtrip_and_clamp(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    from strand.ui import app as app_mod

    class Win:
        def __init__(self, w=800, h=700):
            self.w, self.h = w, h

        width = lambda self: self.w  # noqa: E731
        height = lambda self: self.h  # noqa: E731

        def setWidth(self, v):
            self.w = v

        def setHeight(self, v):
            self.h = v

    path = str(tmp_path / "size.ini")
    settings = QSettings(path, QSettings.Format.IniFormat)
    assert app_mod.restore_size(Win(), settings) is False  # nothing saved
    app_mod.save_size(Win(910, 730), settings)
    reopened = QSettings(path, QSettings.Format.IniFormat)  # proves the sync() flush
    fresh = Win(1, 1)
    assert app_mod.restore_size(fresh, reopened) is True
    assert (fresh.w, fresh.h) == (910, 730)
    app_mod.save_size(Win(10, 20), settings)
    small = Win(1, 1)
    assert app_mod.restore_size(small, settings) is True
    assert (small.w, small.h) == (app_mod.MIN_WIDTH, app_mod.MIN_HEIGHT)
    settings.setValue(app_mod.WIDTH_KEY, "junk")
    assert app_mod.restore_size(Win(), settings) is False


def test_restore_window_wayland_skips_position_and_survives_errors(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    from strand.ui import app as app_mod

    class Win:
        w, h = 1, 1
        positioned = False

        def setWidth(self, v):
            self.w = v

        def setHeight(self, v):
            self.h = v

        def setGeometry(self, *a):
            self.positioned = True

        def minimumWidth(self):
            raise RuntimeError("compositor rejected")

    settings = QSettings(str(tmp_path / "w.ini"), QSettings.Format.IniFormat)
    settings.setValue(app_mod.WIDTH_KEY, 900)
    settings.setValue(app_mod.HEIGHT_KEY, 800)
    settings.setValue(app_mod.GEOMETRY_KEY, [5, 5, 900, 800])
    win = Win()
    app_mod.restore_window(win, settings, "wayland")
    assert (win.w, win.h) == (900, 800) and not win.positioned
    win = Win()
    app_mod.restore_window(win, settings, "xcb")  # positioning raises: swallowed, size kept
    assert (win.w, win.h) == (900, 800)


class _ExplodingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield sse("par", done=False)
        raise RuntimeError("boom during teardown")


def test_unexpected_stream_exception_is_isolated(qapp):
    b = make_bridge(lambda r: httpx.Response(200, stream=_ExplodingStream()))
    events = collect(b)
    b.sendMessage("hello")
    assert _wait(lambda: any(e[0] == "error" for e in events))
    assert _wait(lambda: not b.busy)
    assert b.history == []  # rolled back like any failed turn


def test_abort_after_loop_closed_is_silent(qapp):
    class DeadLoop:
        def call_soon_threadsafe(self, fn):
            raise RuntimeError("Event loop is closed")

    b = make_bridge(lambda r: httpx.Response(200))
    b._busy = True
    b._loop, b._task = DeadLoop(), type("T", (), {"cancel": lambda self: None})()
    assert b.abort() is True  # no exception escapes
    b._busy = False


def _load_window(b):
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle

    from strand.ui.app import MAIN_QML

    QQuickStyle.setStyle("Basic")
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", b)
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    assert engine.rootObjects()
    return engine, engine.rootObjects()[0]


def test_qml_hotkey_actions_and_drawer(qapp):
    from PySide6.QtCore import QMetaObject

    b = make_bridge(lambda r: httpx.Response(200, content=sse("ok")))
    engine, win = _load_window(b)
    assert win.property("drawerOpen") is False  # hidden by default
    QMetaObject.invokeMethod(win, "toggleDrawer")
    assert win.property("drawerOpen") is True
    QMetaObject.invokeMethod(win, "toggleDrawer")
    assert win.property("drawerOpen") is False

    old = b.sessionId
    QMetaObject.invokeMethod(win, "newSession")  # what Ctrl+L triggers
    assert b.sessionId != old

    input_item = win.findChild(object, "messageInput")
    input_item.setProperty("text", "draft")
    QMetaObject.invokeMethod(win, "handleEscape")  # idle Esc clears the input
    assert input_item.property("text") == ""
    engine.deleteLater()


def test_qml_slash_command_is_handled_locally(qapp):
    from PySide6.QtCore import QMetaObject

    requests = []
    b = make_bridge(lambda r: requests.append(r) or httpx.Response(200, content=sse("ok")))
    engine, win = _load_window(b)
    input_item = win.findChild(object, "messageInput")
    assert input_item is not None
    input_item.setProperty("text", "/model gemini-3.6-flash")
    QMetaObject.invokeMethod(win, "send")
    assert b.modelName == "gemini-3.6-flash"
    assert input_item.property("text") == ""
    assert b.history == [] and requests == []
    engine.deleteLater()


def test_qml_note_bubbles_do_not_collapse(qapp):
    """Local /help and /private feedback must wrap at a sane width, not one word per line."""
    from PySide6.QtCore import QMetaObject, Qt, Q_ARG
    from strand.ui.bridge import HELP_TEXT

    b = make_bridge(lambda r: httpx.Response(200, json={}))
    engine, win = _load_window(b)
    win.setWidth(760)
    win.setHeight(640)
    win.show()
    for text in (b.runCommand("/help"), b.runCommand("/private on"), "ok"):
        QMetaObject.invokeMethod(win, "note", Qt.ConnectionType.DirectConnection, Q_ARG("QVariant", text))
    for _ in range(5):
        QCoreApplication.processEvents()

    def walk(item):
        for child in item.childItems():
            yield child
            yield from walk(child)

    bubbles = [i for i in walk(win.contentItem()) if i.objectName() == "bubble"]
    assert len(bubbles) == 3
    by_len = sorted(bubbles, key=lambda i: i.property("naturalWidth"))
    short, medium, long_ = by_len[0], by_len[1], by_len[2]
    list_width = 760 - 2
    for bubble in bubbles:
        assert 60 <= bubble.width() <= list_width  # valid, bounded by the list
    assert long_.width() >= 400  # the long help text uses the available width
    assert long_.width() > 4 * short.width() or short.width() >= 120
    # Wrapped, so it is several lines tall but nowhere near one-word-per-line.
    words = len(HELP_TEXT.split())
    line_height = 14 * 1.2
    assert long_.height() < words * line_height * 0.5
    assert long_.height() > 2 * line_height  # it did wrap into multiple lines
    win.hide()
    engine.deleteLater()


def test_shutdown_cancels_hanging_stream_and_joins_worker(qapp):
    class Hang(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(60)
            yield b""

    b = make_bridge(lambda r: httpx.Response(200, stream=Hang()))
    threads = []
    orig = b._run_async
    b._run_async = lambda *a, **k: threads.append(orig(*a, **k)) or threads[-1]
    b.sendMessage("hello")
    import time

    deadline = time.time() + 5
    while not b._workers and time.time() < deadline:
        time.sleep(0.01)
    assert threads[0].is_alive()
    b.shutdown()
    assert not threads[0].is_alive()
    assert not b._workers
    b.shutdown()  # idempotent


def test_shutdown_without_workers_is_noop(qapp):
    b = make_bridge(lambda r: httpx.Response(200, content=sse("ok")))
    b.shutdown()


def _walk(item):
    for child in item.childItems():
        yield child
        yield from _walk(child)


def test_qml_typography_cascade_and_turn_geometry(qapp):
    from PySide6.QtCore import QMetaObject, Qt, Q_ARG

    b = make_bridge(lambda r: httpx.Response(200, json={}))
    engine, win = _load_window(b)
    win.setWidth(760)
    win.setHeight(640)
    win.show()
    assert win.property("monoFamilies").toVariant() == ["JetBrains Mono", "Fira Code", "monospace"]
    assert win.property("uiFamilies").toVariant()[1:] == ["Inter", "-apple-system", "Segoe UI", "Noto Sans", "sans-serif"]
    assert win.property("narrativeLineHeight") == 1.65
    assert win.property("uiFamily") and win.property("monoFamily")
    QMetaObject.invokeMethod(win, "note", Qt.ConnectionType.DirectConnection, Q_ARG("QVariant", "ok"))
    for _ in range(5):
        QCoreApplication.processEvents()
    notes = [i for i in _walk(win.contentItem()) if i.objectName() == "bubble"]
    assert len(notes) == 1 and notes[0].x() < 40  # left-aligned, unboxed
    win.hide()
    engine.deleteLater()


def test_qml_diff_helpers(qapp):
    from PySide6.QtCore import QMetaObject, Qt, Q_ARG, Q_RETURN_ARG

    b = make_bridge(lambda r: httpx.Response(200, json={}))
    engine, win = _load_window(b)
    diff = "--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n-old <1>\n+new"

    def call(name, *args):
        return QMetaObject.invokeMethod(
            win, name, Qt.ConnectionType.DirectConnection, Q_RETURN_ARG("QVariant"),
            *[Q_ARG("QVariant", a) for a in args],
        )

    assert call("diffTitle", diff) == "src/x.py"
    assert call("isDiff", "diff", "") is True and call("isDiff", "python", "x") is False
    html = call("diffHtml", diff)
    assert "#f87171\">-old &lt;1&gt;" in html and "#34d399\">+new" in html
    engine.deleteLater()


# ---- Settings modal: config bridge and extractor probe ----


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in bridge_mod.load_config.__globals__["ENV_VARS"].values():
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_get_config_defaults_and_file(qapp, home):
    b = make_bridge(lambda r: httpx.Response(200, json={}))
    cfg = b.getConfig()
    assert cfg["persona_prompt"] == ""
    assert cfg["upstream_url"] == "https://api.openai.com/v1"
    assert cfg["decay_enabled"] is False
    assert cfg["fail_closed_private"] is True
    assert cfg["upstream_api_key_set"] is False
    assert "upstream_api_key" not in cfg  # the secret is never handed to QML
    path = home / ".config" / "strand" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model_default": "m1", "upstream_api_key": "sk-x"}))
    cfg = b.getConfig()
    assert cfg["model_default"] == "m1"
    assert cfg["upstream_api_key_set"] is True


def test_save_config_atomic_modes_and_merge(qapp, home):
    b = make_bridge(lambda r: httpx.Response(200, json={}))
    path = home / ".config" / "strand" / "config.json"
    assert b.saveConfig({"persona_prompt": "  Dry wit.  ", "decay_enabled": True, "bogus": 1}) is True
    data = json.loads(path.read_text())
    assert data == {"persona_prompt": "Dry wit.", "decay_enabled": True}
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert b.saveConfig({"upstream_api_key": "sk-1", "fail_closed_private": False}) is True
    assert b.saveConfig({"upstream_api_key": "", "model_default": "m2"}) is True  # blank keeps key
    data = json.loads(path.read_text())
    assert data["upstream_api_key"] == "sk-1"
    assert data["persona_prompt"] == "Dry wit."
    assert data["fail_closed_private"] is False and data["model_default"] == "m2"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]  # no temp leftovers
    assert b.getConfig()["fail_closed_private"] is False


def test_save_config_rejects_bad_input_and_corrupt_file(qapp, home):
    b = make_bridge(lambda r: httpx.Response(200, json={}))
    assert b.saveConfig({"decay_enabled": "maybe"}) is False
    path = home / ".config" / "strand" / "config.json"
    assert not path.exists()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert b.saveConfig({"persona_prompt": "x"}) is False
    assert path.read_text() == "{not json"  # never clobbers an unreadable file


def _probe(handler, url):
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: E731
    b = Bridge(config=Config(), model="m", probe_factory=factory)
    results = []
    b.extractorStatusChanged.connect(lambda ok, msg: results.append((ok, msg)))
    asyncio.run(b._probe_extractor(url))
    return results


def test_extractor_probe_success_fallback_and_failures(qapp):
    seen = []

    def ok(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": []})

    assert _probe(ok, "http://127.0.0.1:1234/v1/") == [(True, "Connected")]
    assert seen == ["http://127.0.0.1:1234/v1/models"]

    def only_v1(request):
        return httpx.Response(200 if request.url.path == "/v1/models" else 404)

    assert _probe(only_v1, "http://127.0.0.1:1234") == [(True, "Connected")]
    assert _probe(lambda r: httpx.Response(500), "http://h/v1") == [(False, "HTTP 500")]

    def timeout(request):
        raise httpx.ConnectTimeout("slow")

    assert _probe(timeout, "http://h") == [(False, "Timed out")]

    def refuse(request):
        raise httpx.ConnectError("no")

    assert _probe(refuse, "http://h") == [(False, "Unreachable: ConnectError")]
    assert _probe(ok, "  ") == [(False, "Enter an http(s) URL")]


def test_test_extractor_slot_emits_signal_from_worker(qapp):
    import time

    from PySide6.QtCore import QCoreApplication

    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))  # noqa: E731
    b = Bridge(config=Config(), model="m", probe_factory=factory)
    results = []
    b.extractorStatusChanged.connect(lambda ok, msg: results.append((ok, msg)))
    b.testExtractor("http://h/v1")
    deadline = time.time() + 5
    while not results and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    b.shutdown()
    assert results == [(True, "Connected")]


def test_qml_settings_modal_open_close(qapp, home):
    from PySide6.QtCore import QMetaObject

    b = make_bridge(lambda r: httpx.Response(200, json={}))
    engine, win = _load_window(b)
    modal = win.findChild(object, "settingsModal")
    assert modal is not None and modal.property("visible") is False
    assert win.property("settingsOpen") is False
    QMetaObject.invokeMethod(win, "openSettings")
    assert win.property("settingsOpen") is True and modal.property("visible") is True
    QMetaObject.invokeMethod(win, "handleEscape")  # what Esc triggers
    assert win.property("settingsOpen") is False
    QMetaObject.invokeMethod(win, "openSettings")
    QMetaObject.invokeMethod(win, "closeSettings")  # backdrop / ✕ handler
    assert win.property("settingsOpen") is False
    engine.deleteLater()


def test_extractor_model_read_and_save(qapp, home):
    b = make_bridge(lambda r: httpx.Response(200, json={}))
    assert b.getConfig()["extractor_model"] == ""
    assert b.saveConfig({"extractor_model": "  qwen2.5:3b "}) is True
    path = home / ".config" / "strand" / "config.json"
    assert json.loads(path.read_text())["extractor_model"] == "qwen2.5:3b"
    assert b.getConfig()["extractor_model"] == "qwen2.5:3b"
    assert b.saveConfig({"extractor_url": "http://127.0.0.1:11434/v1"}) is True
    assert b.getConfig()["extractor_model"] == "qwen2.5:3b"  # merge keeps it


@pytest.mark.parametrize(
    ("segment", "enabled", "days", "whisper"),
    [
        ("horizonSprint", True, 7.0, "Aggressive pruning: unreferenced strands fade after 7 days."),
        ("horizonBalanced", True, 30.0, "Standard working memory: unreferenced strands fade over 30 days."),
        ("horizonPermanent", False, None, "Time is frozen: all strands retain permanent baseline weight."),
    ],
)
def test_qml_decay_horizon_segments_persist(qapp, home, segment, enabled, days, whisper):
    from PySide6.QtCore import QMetaObject

    b = make_bridge(lambda r: httpx.Response(200, json={}))
    engine, win = _load_window(b)
    assert win.findChild(object, "decayBox") is None  # checkbox replaced
    QMetaObject.invokeMethod(win, "openSettings")
    if segment == "horizonSprint":  # start from a different state so the click must change it
        assert b.saveConfig({"decay_enabled": False}) is True
    win.findChild(object, segment).click()
    path = home / ".config" / "strand" / "config.json"
    data = json.loads(path.read_text())
    assert data["decay_enabled"] is enabled
    if days is not None:
        assert data["decay_half_life_days"] == days
    assert win.findChild(object, "horizonWhisper").property("text") == whisper
    assert (path.stat().st_mode & 0o777) == 0o600
    cfg = b.getConfig()
    assert cfg["decay_enabled"] is enabled
    assert cfg["decay_half_life_days"] == (days if days is not None else 30.0)
    engine.deleteLater()


def test_save_config_decay_half_life_validation(qapp, home):
    b = make_bridge(lambda r: httpx.Response(200, json={}))
    assert b.saveConfig({"decay_enabled": True, "decay_half_life_days": 7}) is True
    path = home / ".config" / "strand" / "config.json"
    assert json.loads(path.read_text()) == {"decay_enabled": True, "decay_half_life_days": 7.0}
    for bad in (0, -1, "nan", "abc"):
        assert b.saveConfig({"decay_half_life_days": bad}) is False
    assert json.loads(path.read_text())["decay_half_life_days"] == 7.0
