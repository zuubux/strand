import asyncio
import json
import os

import httpx
import pytest
from PySide6.QtCore import QCoreApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cortext.config import Config  # noqa: E402
from cortext.ui import bridge as bridge_mod  # noqa: E402
from cortext.ui.bridge import Bridge, parse_sse_line, resolve_endpoint, status_label  # noqa: E402


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
        transport=httpx.MockTransport(handler), base_url="http://cortext"
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
    assert (uds, base) == (path, "http://cortext")
    assert status_label(uds, base) == "Connected: c.sock"


def test_resolve_endpoint_xdg_default_and_tcp_fallback(tmp_path):
    sock = tmp_path / "cortext" / "cortext.sock"
    config = Config()
    assert config.effective_socket_path({"XDG_RUNTIME_DIR": str(tmp_path)}) == str(sock)
    # Missing socket file -> loopback TCP fallback.
    uds, base = resolve_endpoint(config, {"XDG_RUNTIME_DIR": str(tmp_path)})
    assert uds is None and base == "http://127.0.0.1:8088"
    assert status_label(uds, base) == "Connected: 127.0.0.1:8088"


def test_make_client_selects_uds_transport(tmp_path, monkeypatch):
    b = Bridge(config=Config(), model="m")
    b._uds_path, b._base_url = str(tmp_path / "x.sock"), "http://cortext"
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

    from cortext.ui.app import MAIN_QML

    QQuickStyle.setStyle("Basic")
    b = make_bridge(lambda r: httpx.Response(200, json={}))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", b)
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    assert engine.rootObjects()
    assert engine.rootObjects()[0].property("title") == "Cortext"
    engine.deleteLater()


# ---- model resolution: explicit > CORTEXT_UI_MODEL > config model_default > fallback ----


def test_resolve_model_precedence():
    from cortext.ui.bridge import DEFAULT_MODEL, resolve_model

    cfg = Config(model_default="gemini-3.6-flash")
    assert resolve_model(cfg, {"CORTEXT_UI_MODEL": "env-m"}, explicit="arg-m") == "arg-m"
    assert resolve_model(cfg, {"CORTEXT_UI_MODEL": "env-m"}) == "env-m"
    assert resolve_model(cfg, {"CORTEXT_UI_MODEL": "  "}) == "gemini-3.6-flash"
    assert resolve_model(cfg, {}) == "gemini-3.6-flash"
    assert resolve_model(Config(), {}) == DEFAULT_MODEL


def test_bridge_uses_persisted_model_default(qapp, monkeypatch):
    monkeypatch.delenv("CORTEXT_UI_MODEL", raising=False)
    cfg = Config(model_default="gemini-3.6-flash")
    assert Bridge(config=cfg).modelName == "gemini-3.6-flash"
    monkeypatch.setenv("CORTEXT_UI_MODEL", "override")
    assert Bridge(config=cfg).modelName == "override"
