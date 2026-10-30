"""QObject bridge between the QML chat window and the local Strand daemon.

Transport is the daemon's UNIX socket (loopback TCP fallback) via httpx. Each turn
streams on a worker thread with its own asyncio loop; Qt signals emitted there are
delivered to the GUI thread as queued connections.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
from PySide6.QtCore import Property, QObject, Signal, Slot

from strand.config import (
    Config,
    _as_bool,
    _read_file,
    _validate_half_life,
    default_config_path,
    load_config,
)
from strand.onboarding import ONBOARD_TRIGGER, ONBOARDING_HEADER, ONBOARDING_MODE

DEFAULT_MODEL = "gpt-4o-mini"
UDS_BASE_URL = "http://strand"
CHAT_PATH = "/v1/chat/completions"
PRIVATE_HEADER = "X-Strand-Allow-Private"
HISTORY_LIMIT = 100
EXTRACTOR_TIMEOUT = 1.5
# Keys the Settings modal may persist to config.json, with their value kind.
SETTINGS_KEYS: dict[str, str] = {
    "persona_prompt": "str",
    "upstream_url": "str",
    "upstream_api_key": "secret",
    "model_default": "str",
    "extractor_url": "str",
    "extractor_model": "str",
    "decay_enabled": "bool",
    "decay_half_life_days": "float",
    "fail_closed_private": "bool",
    "redact_secrets": "bool",
}
COMMAND_RE = re.compile(r"^/([A-Za-z]+)(?:[ \t]+(.*))?$", re.DOTALL)
FENCE_RE = re.compile(r"^[ \t]{0,3}```[ \t]*([\w+.#-]*)[ \t]*$")
HELP_TEXT = (
    "Commands: /new (fresh session), /private on|off (send X-Strand-Allow-Private), "
    "/model <name> (switch model), /onboard (fresh session, Strand opens the grounding conversation), /help. Ctrl+L new session, Ctrl+D diagnostics, Esc stop/clear."
)


def parse_command(text: str) -> tuple[str, str] | None:
    """Split a client-side slash command into `(name, argument)`, or None for normal prompts.

    Only a single leading `/word` counts, so paths such as `/etc/hosts` are sent upstream.
    """
    match = COMMAND_RE.match(text.strip())
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


def split_markdown(text: str) -> list[dict[str, Any]]:
    """Split text into `{code, lang, text}` segments on ``` fences.

    An unclosed fence (a stream still in progress) stays a code segment to the end.
    """
    segments: list[dict[str, Any]] = []
    buf: list[str] = []
    in_code = False
    lang = ""

    def flush() -> None:
        body = "\n".join(buf)
        if in_code or body.strip():
            segments.append({"code": in_code, "lang": lang if in_code else "", "text": body})
        buf.clear()

    for line in text.split("\n"):
        fence = FENCE_RE.match(line)
        if fence and not in_code:
            flush()
            in_code, lang = True, fence.group(1)
        elif fence and in_code and not fence.group(1):
            flush()
            in_code, lang = False, ""
        else:
            buf.append(line)
    flush()
    return segments


def resolve_model(
    config: Config, env: Mapping[str, str] | None = None, explicit: str | None = None
) -> str:
    """Pick the chat model: explicit > $STRAND_UI_MODEL > config `model_default` > fallback."""
    env = os.environ if env is None else env
    for candidate in (explicit, env.get("STRAND_UI_MODEL"), config.model_default):
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
    modelChanged = Signal()
    sessionChanged = Signal()
    privateChanged = Signal()
    diagnosticsChanged = Signal()
    streamAborted = Signal()
    conversationReset = Signal()
    extractorStatusChanged = Signal(bool, str)

    def __init__(
        self,
        config: Config | None = None,
        model: str | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        parent: QObject | None = None,
        probe_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        super().__init__(parent)
        self._probe_factory = probe_factory
        self._config = config if config is not None else load_config()
        self._model = resolve_model(self._config, explicit=model)
        self._session_id = f"ui-{uuid.uuid4().hex[:12]}"
        self._uds_path, self._base_url = resolve_endpoint(self._config)
        self._client_factory = client_factory
        self._history: list[dict[str, str]] = []
        self._status = "Connecting…"
        self._busy = False
        self._lock = threading.Lock()
        self._private = False
        self._onboarding = False
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._abort_requested = False
        self._first_token_ms = 0
        self._token_count = 0
        self._prompts: list[str] = []
        self._prompt_index: int | None = None
        self._turn: dict[str, Any] = {}
        self._workers: dict[threading.Thread, tuple[asyncio.AbstractEventLoop, asyncio.Task[None]]] = {}

    # -- properties exposed to QML -------------------------------------------------
    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(str, notify=modelChanged)
    def modelName(self) -> str:
        return self._model

    @Property(str, notify=sessionChanged)
    def sessionId(self) -> str:
        return self._session_id

    @Property(bool, notify=privateChanged)
    def privateMode(self) -> bool:
        return self._private

    @Property(str, constant=True)
    def transport(self) -> str:
        return f"unix:{self._uds_path}" if self._uds_path else f"tcp:{self._base_url[7:]}"

    def _transport_detail(self) -> str:
        """Active runtime path plus mode, e.g. `unix:/run/strand/strand.sock (0600)`."""
        if not self._uds_path:
            return f"{self.transport} (loopback)"
        try:
            mode = f"{stat.S_IMODE(os.stat(self._uds_path).st_mode):04o}"
        except OSError:
            mode = "unavailable"
        return f"{self.transport} ({mode})"

    @Property("QVariantMap", notify=diagnosticsChanged)
    def diagnostics(self) -> dict[str, Any]:
        """Session/model/transport plus metrics of the latest turn (empty values before one)."""
        info: dict[str, Any] = {
            "sessionId": self._session_id,
            "model": self._model,
            "transport": self._transport_detail(),
            "status": self._status,
            "private": self._private,
            "promptChars": 0,
            "replyChars": 0,
            "latencyMs": 0,
            "firstTokenMs": 0,
            "ttft_ms": 0,
            "total_ms": 0,
            "token_count": 0,
            "throughput": "0.0 t/s",
            "ledger": "Active" if self._history else "Cached",
            "outcome": "",
        }
        info.update(self._turn)
        return info

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
        if self._private:
            headers[PRIVATE_HEADER] = "true"
        if self._onboarding:
            headers[ONBOARDING_HEADER] = ONBOARDING_MODE
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

    async def _stream(self, messages: list[dict[str, str]], generation: int | None = None) -> None:
        """Stream one completion; emits chunk/finished/error/aborted signals, never raises httpx errors.

        `generation` ties the turn to the conversation it started in: if the conversation was
        reset meanwhile, history is left alone (the new session must stay clean).
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "session_id": self._session_id,
        }
        parts: list[str] = []
        error: str | None = None
        aborted = False
        started = time.perf_counter()
        self._first_token_ms = 0
        self._token_count = 0
        try:
            async with self.make_client() as client:
                async with client.stream("POST", CHAT_PATH, json=body, headers=self._headers()) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace").strip()
                        error = f"HTTP {resp.status_code}: {detail[:300]}" if detail else f"HTTP {resp.status_code}"
                    else:
                        error = await self._consume(resp, parts, started)
                        self._set_status(status_label(self._uds_path, self._base_url))
        except asyncio.CancelledError:
            aborted = True
        except httpx.ConnectError as exc:
            self._set_status("Disconnected")
            error = f"Cannot reach Strand daemon: {exc}"
        except httpx.HTTPError as exc:
            error = f"Request failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - nothing may escape the worker thread
            if self._abort_requested:
                aborted = True  # teardown noise from a cancelled stream is silent
            else:
                error = f"Stream failed: {exc}"
        current = generation is None or generation == self._generation
        if current:
            total = time.perf_counter() - started
            ttft = self._first_token_ms / 1000
            tps = self._token_count / max(0.001, total - ttft)
            self._turn = {
                "throughput": f"{tps:.1f} t/s",
                "ttft_ms": self._first_token_ms,
                "total_ms": int(total * 1000),
                "token_count": self._token_count,
                "promptChars": len(messages[-1]["content"]) if messages else 0,
                "replyChars": sum(len(p) for p in parts),
                "latencyMs": int((time.perf_counter() - started) * 1000),
                "firstTokenMs": self._first_token_ms,
                "outcome": "aborted" if aborted else ("error" if error is not None else "ok"),
            }
            self.diagnosticsChanged.emit()
        if aborted:
            if current:
                if parts:
                    self._history.append({"role": "assistant", "content": "".join(parts)})
                elif self._history and self._history[-1]["role"] == "user":
                    self._history.pop()
            self.streamAborted.emit()
            return
        if error is not None:
            if current and self._history and self._history[-1]["role"] == "user":
                self._history.pop()
            self.errorOccurred.emit(error)
            return
        if current:
            self._history.append({"role": "assistant", "content": "".join(parts)})
        self.streamFinished.emit()

    async def _consume(
        self, resp: httpx.Response, parts: list[str], started: float | None = None
    ) -> str | None:
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
                    self._note_first_token(parts, started)
                    parts.append(value)
                    self.chunkReceived.emit(value)
                elif kind == "error":
                    return value
                elif kind == "done":
                    return None
        buffer += decoder.decode(b"", final=True)
        kind, value = parse_sse_line(buffer)
        if kind == "token":
            self._note_first_token(parts, started)
            parts.append(value)
            self.chunkReceived.emit(value)
        elif kind == "error":
            return value
        return None

    def _note_first_token(self, parts: list[str], started: float | None) -> None:
        self._token_count += 1
        if not parts and started is not None:
            self._first_token_ms = int((time.perf_counter() - started) * 1000)

    def _run_async(
        self,
        coro_factory: Callable[[], Any],
        on_done: Callable[[], None] | None = None,
        cancellable: bool = False,
    ) -> threading.Thread:
        async def runner() -> None:
            with self._lock:
                self._workers[threading.current_thread()] = (
                    asyncio.get_running_loop(),
                    asyncio.current_task(),
                )
            if cancellable:
                with self._lock:
                    self._loop = asyncio.get_running_loop()
                    self._task = asyncio.current_task()
                    if self._abort_requested:
                        self._task.cancel()  # abort arrived before the task existed
            await coro_factory()

        def target() -> None:
            try:
                asyncio.run(runner())
            except (asyncio.CancelledError, httpx.HTTPError, Exception):
                pass  # never let a stream failure or abort reach the Qt runtime
            finally:
                with self._lock:
                    self._workers.pop(threading.current_thread(), None)
                    if cancellable:
                        self._loop = self._task = None
                if on_done:
                    try:
                        on_done()
                    except Exception:  # noqa: BLE001
                        pass

        thread = threading.Thread(target=target, name="strand-ui-io", daemon=True)
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
            self._abort_requested = False
            self._set_busy(True)
        self._remember(text)
        self._start_turn(text, locked=True)

    def _start_turn(self, text: str, locked: bool = False) -> None:
        """Append `text` as a user turn and stream the reply (no-op while busy)."""
        if not locked:
            with self._lock:
                if self._busy:
                    return
                self._abort_requested = False
                self._set_busy(True)
        self._history.append({"role": "user", "content": text})
        messages = list(self._history)
        generation = self._generation
        self._run_async(
            lambda: self._stream(messages, generation),
            on_done=lambda: self._set_busy(False),
            cancellable=True,
        )

    @Slot()
    def checkConnection(self) -> None:
        self._run_async(self._check)

    # -- settings (Settings modal) ---------------------------------------------------
    @Slot(result="QVariantMap")
    def getConfig(self) -> dict[str, Any]:
        """Active settings for the Settings modal. The upstream API key is never returned."""
        cfg = load_config()
        file_values = _read_file(default_config_path())
        return {
            "persona_prompt": cfg.persona_prompt,
            "upstream_url": cfg.upstream_url,
            "model_default": cfg.model_default,
            "extractor_url": cfg.extractor_url,
            "extractor_model": cfg.extractor_model,
            "decay_enabled": cfg.decay_enabled,
            "decay_half_life_days": cfg.decay_half_life_days,
            "upstream_api_key_set": bool(cfg.upstream_api_key),
            "fail_closed_private": _as_bool(file_values.get("fail_closed_private", True)),
            "redact_secrets": _as_bool(file_values.get("redact_secrets", True)),
        }

    @Slot("QVariantMap", result=bool)
    def saveConfig(self, data: dict[str, Any]) -> bool:
        """Merge recognised keys into config.json atomically (dir 0700, file 0600).

        Unknown keys are ignored; an empty `upstream_api_key` keeps the stored key.
        Returns False (file untouched) on invalid input or an I/O error.
        """
        try:
            path = default_config_path()
            current = _read_file(path, strict=True)
            updates: dict[str, Any] = {}
            for key, kind in SETTINGS_KEYS.items():
                if key not in data:
                    continue
                value = data[key]
                if kind == "bool":
                    updates[key] = _as_bool(value)
                elif kind == "float":
                    updates[key] = _validate_half_life(value)
                else:
                    text = "" if value is None else str(value).strip()
                    if kind == "secret" and not text:
                        continue
                    updates[key] = text
            current.update(updates)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except PermissionError:
                pass
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(current, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except (OSError, ValueError, TypeError):
            return False
        return True

    @Slot(str)
    def testExtractor(self, url: str) -> None:
        """Probe the local SLM endpoint off-thread; result arrives as `extractorStatusChanged`."""
        self._run_async(lambda: self._probe_extractor(url))

    async def _probe_extractor(self, url: str) -> None:
        base = url.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            self.extractorStatusChanged.emit(False, "Enter an http(s) URL")
            return
        candidates = [f"{base}/models"]
        if not base.endswith("/v1"):
            candidates.append(f"{base}/v1/models")
        message = "Unreachable"
        try:
            async with self._make_probe_client() as client:
                for target in candidates:
                    resp = await client.get(target)
                    if resp.status_code < 400:
                        self.extractorStatusChanged.emit(True, "Connected")
                        return
                    message = f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            message = "Timed out"
        except httpx.HTTPError as exc:
            message = f"Unreachable: {type(exc).__name__}"
        self.extractorStatusChanged.emit(False, message)

    def _make_probe_client(self) -> httpx.AsyncClient:
        if self._probe_factory is not None:
            return self._probe_factory()
        return httpx.AsyncClient(timeout=EXTRACTOR_TIMEOUT)

    @Slot(result=bool)
    def abort(self) -> bool:
        """Cancel the in-flight stream (if any); partial output is kept. True if one was running."""
        with self._lock:
            if not self._busy:
                return False
            self._abort_requested = True
            if self._loop is not None and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except (RuntimeError, asyncio.CancelledError):
                    pass  # loop already closed: the turn is finishing on its own
            return True

    def shutdown(self, timeout: float = 2.0) -> None:
        """Cancel every in-flight worker (stream and connection check) and join its thread.

        Called when the window closes so no `strand-ui-io` thread outlives the GUI.
        """
        with self._lock:
            self._abort_requested = True
            workers = list(self._workers.items())
        for thread, (loop, task) in workers:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop already closed: the worker is finishing on its own
        for thread, _ in workers:
            thread.join(timeout)

    @Slot()
    def newConversation(self) -> None:
        """Drop history, abort any running turn and start a fresh session id immediately."""
        self._generation += 1
        if self._busy:
            self.abort()
        self._history.clear()
        self._turn = {}
        self._onboarding = False
        self._session_id = f"ui-{uuid.uuid4().hex[:12]}"
        self.sessionChanged.emit()
        self.diagnosticsChanged.emit()
        self.conversationReset.emit()

    @Slot(str)
    def setModel(self, name: str) -> None:
        name = name.strip()
        if name and name != self._model:
            self._model = name
            self.modelChanged.emit()
            self.diagnosticsChanged.emit()

    @Slot(bool)
    def setPrivateMode(self, enabled: bool) -> None:
        if enabled != self._private:
            self._private = enabled
            self.privateChanged.emit()
            self.diagnosticsChanged.emit()

    @Slot(str, result=str)
    def runCommand(self, text: str) -> str:
        """Execute a client-side slash command; returns its feedback, or "" for a normal prompt.

        Commands never reach the daemon or the upstream model.
        """
        parsed = parse_command(text)
        if parsed is None:
            return ""
        name, arg = parsed
        self._remember(text.strip())
        if name == "new":
            self.newConversation()
            return "Started a new session."
        if name == "onboard":
            self.newConversation()
            self._onboarding = True
            self.diagnosticsChanged.emit()
            self._start_turn(ONBOARD_TRIGGER)
            return "Onboarding session started."
        if name == "private":
            value = arg.lower()
            if value not in ("on", "off"):
                state = "on" if self._private else "off"
                return f"Private mode is {state}. Usage: /private on|off"
            self.setPrivateMode(value == "on")
            return f"Private context {'enabled' if self._private else 'disabled'}."
        if name == "model":
            if not arg:
                return f"Model: {self._model}. Usage: /model <name>"
            self.setModel(arg)
            return f"Model set to {self._model}."
        if name == "help":
            return HELP_TEXT
        return f"Unknown command /{name}. {HELP_TEXT}"

    # -- input history (Up/Down in the input bar) ---------------------------------------
    def _remember(self, text: str) -> None:
        if text and (not self._prompts or self._prompts[-1] != text):
            self._prompts.append(text)
            del self._prompts[:-HISTORY_LIMIT]
        self._prompt_index = None

    @Slot(result=str)
    def historyPrevious(self) -> str:
        if not self._prompts:
            return ""
        if self._prompt_index is None:
            self._prompt_index = len(self._prompts) - 1
        else:
            self._prompt_index = max(0, self._prompt_index - 1)
        return self._prompts[self._prompt_index]

    @Slot(result=str)
    def historyNext(self) -> str:
        if self._prompt_index is None:
            return ""
        self._prompt_index += 1
        if self._prompt_index >= len(self._prompts):
            self._prompt_index = None
            return ""
        return self._prompts[self._prompt_index]

    @Slot(str, result="QVariantList")
    def segments(self, text: str) -> list[dict[str, Any]]:
        return split_markdown(text)
