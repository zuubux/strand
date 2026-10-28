"""QObject bridge between the QML chat window and the local Cortext daemon.

Transport is the daemon's UNIX socket (loopback TCP fallback) via httpx. Each turn
streams on a worker thread with its own asyncio loop; Qt signals emitted there are
delivered to the GUI thread as queued connections.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from PySide6.QtCore import Property, QObject, Signal, Slot

from cortext.config import Config, load_config

DEFAULT_MODEL = "gpt-4o-mini"
UDS_BASE_URL = "http://cortext"
CHAT_PATH = "/v1/chat/completions"


def resolve_model(
    config: Config, env: Mapping[str, str] | None = None, explicit: str | None = None
) -> str:
    """Pick the chat model: explicit > $CORTEXT_UI_MODEL > config `model_default` > fallback."""
    env = os.environ if env is None else env
    for candidate in (explicit, env.get("CORTEXT_UI_MODEL"), config.model_default):
        if candidate and candidate.strip():
            return candidate.strip()
    return DEFAULT_MODEL


def resolve_endpoint(config: Config, env: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """Return `(uds_path, base_url)`. `uds_path` is None when the TCP fallback applies.

    The socket is used only when the daemon's socket path exists and is a socket.
    """
    path = config.effective_socket_path(env)
    if path:
        try:
            if stat.S_ISSOCK(os.stat(path).st_mode):
                return path, UDS_BASE_URL
        except OSError:
            pass
    return None, f"http://{config.host}:{config.port}"


def status_label(uds_path: str | None, base_url: str) -> str:
    return f"Connected: {os.path.basename(uds_path)}" if uds_path else f"Connected: {base_url[7:]}"


def parse_sse_line(line: str) -> tuple[str, str]:
    """Classify one SSE line as ("token", text), ("done", ""), ("error", msg) or ("skip", "")."""
    line = line.strip()
    if not line.startswith("data:"):
        return "skip", ""
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "done", ""
    try:
        data = json.loads(payload)
    except ValueError:
        return "skip", ""
    if not isinstance(data, dict):
        return "skip", ""
    err = data.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return "error", str(msg or "upstream error")
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta") or {}
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str) and content:
            return "token", content
    return "skip", ""


class Bridge(QObject):
    chunkReceived = Signal(str)
    streamFinished = Signal()
    errorOccurred = Signal(str)
    statusChanged = Signal()
    busyChanged = Signal()

    def __init__(
        self,
        config: Config | None = None,
        model: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config if config is not None else load_config()
        self._model = resolve_model(self._config, explicit=model)
        self._session_id = f"ui-{uuid.uuid4().hex[:12]}"
        self._uds_path, self._base_url = resolve_endpoint(self._config)
        self._client_factory = client_factory
        self._history: list[dict[str, str]] = []
        self._status = "Connecting…"
        self._busy = False
        self._lock = threading.Lock()

    # -- properties exposed to QML -------------------------------------------------
    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(str, constant=True)
    def modelName(self) -> str:
        return self._model

    @Property(str, constant=True)
    def sessionId(self) -> str:
        return self._session_id

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @property
    def history(self) -> list[dict[str, str]]:
        return list(self._history)

    # -- transport -------------------------------------------------------------------
    def make_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        timeout = httpx.Timeout(None, connect=5.0)
        if self._uds_path:
            transport = httpx.AsyncHTTPTransport(uds=self._uds_path)
            return httpx.AsyncClient(transport=transport, base_url=self._base_url, timeout=timeout)
        return httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "X-Session-Id": self._session_id}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.statusChanged.emit()

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            self.busyChanged.emit()

    async def _check(self) -> None:
        try:
            async with self.make_client() as client:
                resp = await client.get("/health", timeout=3.0)
            ok = resp.status_code < 500
        except httpx.HTTPError:
            ok = False
        self._set_status(status_label(self._uds_path, self._base_url) if ok else "Disconnected")

    async def _stream(self, messages: list[dict[str, str]]) -> None:
        """Stream one completion; emits chunk/finished/error signals, never raises httpx errors."""
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "session_id": self._session_id,
        }
        parts: list[str] = []
        error: str | None = None
        try:
            async with self.make_client() as client:
                async with client.stream("POST", CHAT_PATH, json=body, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace").strip()
                        error = f"HTTP {resp.status_code}: {detail[:300]}" if detail else f"HTTP {resp.status_code}"
                    else:
                        error = await self._consume(resp, parts)
                        self._set_status(status_label(self._uds_path, self._base_url))
        except httpx.ConnectError as exc:
            self._set_status("Disconnected")
            error = f"Cannot reach Cortext daemon: {exc}"
        except httpx.HTTPError as exc:
            error = f"Request failed: {exc}"
        if error is not None:
            if self._history and self._history[-1]["role"] == "user":
                self._history.pop()
            self.errorOccurred.emit(error)
            return
        self._history.append({"role": "assistant", "content": "".join(parts)})
        self.streamFinished.emit()

    async def _consume(self, resp: httpx.Response, parts: list[str]) -> str | None:
        """Relay SSE tokens; returns an error message, or None on a clean finish."""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        # SSE is split on "\n" only (never splitlines), matching the proxy.
        async for raw in resp.aiter_bytes():
            buffer += decoder.decode(raw)
            *lines, buffer = buffer.split("\n")
            for line in lines:
                kind, value = parse_sse_line(line)
                if kind == "token":
                    parts.append(value)
                    self.chunkReceived.emit(value)
                elif kind == "error":
                    return value
                elif kind == "done":
                    return None
        buffer += decoder.decode(b"", final=True)
        kind, value = parse_sse_line(buffer)
        if kind == "token":
            parts.append(value)
            self.chunkReceived.emit(value)
        elif kind == "error":
            return value
        return None

    def _run_async(
        self, coro_factory: Callable[[], Any], on_done: Callable[[], None] | None = None
    ) -> threading.Thread:
        def target() -> None:
            try:
                asyncio.run(coro_factory())
            finally:
                if on_done:
                    on_done()

        thread = threading.Thread(target=target, name="cortext-ui-io", daemon=True)
        thread.start()
        return thread

    # -- slots ---------------------------------------------------------------------
    @Slot(str)
    def sendMessage(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            if self._busy:
                return
            self._set_busy(True)
        self._history.append({"role": "user", "content": text})
        messages = list(self._history)
        self._run_async(lambda: self._stream(messages), on_done=lambda: self._set_busy(False))

    @Slot()
    def checkConnection(self) -> None:
        self._run_async(self._check)

    @Slot()
    def newConversation(self) -> None:
        if not self._busy:
            self._history.clear()
            self._session_id = f"ui-{uuid.uuid4().hex[:12]}"
