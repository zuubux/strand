"""OpenAI-compatible streaming proxy that injects novel graph context into the system prompt."""

import asyncio
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .common import strip_think
from .config import Config
from .distill import DistillationEngine
from .logging import get_logger
from .resolver import ContextResolver
from .sanitize import sanitize_text
from .session import MAX_SESSION_ID_LEN

logger = get_logger("proxy")
DEFAULT_SESSION_ID = "default"
_UPSTREAM_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
MAX_CONCURRENT_DISTILLATIONS = 4
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # POST /v1/chat/completions body cap (HTTP 413 beyond this)
_QUERY_LOG_CHARS = 80
_CONTEXT_ROLES = ("system", "developer")


def _text_of(content: Any) -> str:
    """Plain text of a message `content` (string or list of OpenAI content parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def _last_user_query(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _text_of(message.get("content"))
    return ""


def _inject_delta(messages: list[Any], delta: str) -> list[Any]:
    """Prepend `delta` to the first system/developer message, or insert a system message at index 0."""
    out = list(messages)
    for i, message in enumerate(out):
        if isinstance(message, dict) and message.get("role") in _CONTEXT_ROLES:
            content = message.get("content")
            if isinstance(content, list):
                merged: Any = [{"type": "text", "text": delta}, *content]
            else:
                merged = f"{delta}\n\n{content}" if content else delta
            out[i] = {**message, "content": merged}
            return out
    return [{"role": "system", "content": delta}, *out]


def _completion_text(body: bytes) -> str:
    """Assistant text from a non-streaming chat completion body ('' if unparseable)."""
    try:
        data = json.loads(body)
        return _text_of(data["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def _delta_text(content: Any) -> str:
    """Text of a streamed `delta.content`: a string, or a list of `{"type": "text", "text": ...}` parts.

    Parts are joined with "" (stream fragments, unlike whole messages, carry their own spacing).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else part["text"]
            for part in content
            if isinstance(part, str)
            or (isinstance(part, dict) and part.get("type", "text") == "text" and isinstance(part.get("text"), str))
        )
    return ""


def _stream_text(raw: bytes) -> str:
    """Concatenate delta content from a collected SSE stream."""
    parts: list[str] = []
    # Split on "\n" only: str.splitlines() also breaks on U+2028/U+0085 and would truncate events.
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            choices = json.loads(data)["choices"]
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    continue
                parts.append(_delta_text(choice["delta"]["content"]))
        except (ValueError, KeyError, TypeError):
            continue
    return strip_think("".join(parts))


def _stream_outcome(raw: bytes) -> tuple[bool, bool]:
    """(finished, errored) for a collected SSE stream.

    `finished` means a `[DONE]` marker or a non-null `finish_reason` was seen; `errored`
    means some data event carried an `error` object.
    """
    finished = errored = False
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            finished = True
            continue
        if '"error"' not in data:
            if "finish_reason" not in data:
                continue
        try:
            event = json.loads(data)
        except ValueError:
            errored = errored or '"error":' in data
            continue
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            errored = True
        for choice in event.get("choices") or []:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                finished = True
    return finished, errored


def _delta_counts(delta: str) -> tuple[int, int]:
    """(entity, edge) line counts in a formatted context delta."""
    counts = {"Entities:": 0, "Relations:": 0}
    section = ""
    for line in delta.splitlines():
        if line in counts:
            section = line
        elif section and line.startswith("- "):
            counts[section] += 1
    return counts["Entities:"], counts["Relations:"]


def _error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def create_app(
    config: Config,
    resolver: ContextResolver,
    client: httpx.AsyncClient | None = None,
    distill_engine: DistillationEngine | None = None,
) -> FastAPI:
    """Build the proxy app. The lifespan connects/closes the resolver's DB and the HTTP client."""
    http = client if client is not None else httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)
    chat_url = config.upstream_url.rstrip("/") + "/chat/completions"
    engine = (
        distill_engine
        if distill_engine is not None
        else DistillationEngine(resolver.db, ledger=resolver.ledger)
    )
    background_tasks: set[asyncio.Task[Any]] = set()
    distill_slots = asyncio.Semaphore(MAX_CONCURRENT_DISTILLATIONS)

    async def _distill(session_id: str, query: str, response: str) -> None:
        async with distill_slots:
            await engine.process_exchange(session_id, query, response)

    def schedule_distillation(session_id: str, query: str, response: str, allow_private: bool = False) -> None:
        """Fire-and-forget; process_exchange swallows its own errors.

        Never distills an exchange served with private egress context (C1): the reply may quote
        private facts, and distilling it would launder them into public, un-flagged entities.
        """
        if allow_private:
            logger.info("Skipping distillation session=%r: private context allowed for this exchange", session_id)
            return
        if not query or not response:
            return
        task = asyncio.create_task(_distill(session_id, query, response))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await resolver.db.connect()
        try:
            yield
        finally:
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            await http.aclose()
            await resolver.db.close()

    # No auto-generated docs/schema routes on the daemon.
    app = FastAPI(title="Cortext", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    if config.api_key:
        expected = f"Bearer {config.api_key}".encode()

        @app.middleware("http")
        async def require_api_key(request: Request, call_next: Any) -> Response:
            if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
                supplied = request.headers.get("authorization", "").encode()
                if not hmac.compare_digest(supplied, expected):
                    return _error(401, "Invalid or missing API key", "authentication_error")
            return await call_next(request)

    app.state.background_tasks = background_tasks

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            return _error(415, "Content-Type must be application/json", "invalid_request_error")
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0  # unparseable header: the streaming cap below still applies
        too_large = _error(413, f"Request body exceeds {MAX_REQUEST_BYTES} bytes", "invalid_request_error")
        if declared > MAX_REQUEST_BYTES:
            return too_large
        received = bytearray()
        async for chunk in request.stream():  # also bounds chunked uploads that send no Content-Length
            received += chunk
            if len(received) > MAX_REQUEST_BYTES:
                return too_large
        try:
            payload = json.loads(bytes(received))
        except (ValueError, RecursionError):  # RecursionError: pathologically nested JSON
            return _error(400, "Request body must be valid JSON", "invalid_request_error")
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            return _error(400, "'messages' must be a list", "invalid_request_error")

        session_id = str(
            payload.pop("session_id", None) or request.headers.get("x-session-id") or DEFAULT_SESSION_ID
        )
        if len(session_id) > MAX_SESSION_ID_LEN:
            return _error(400, f"session_id must be at most {MAX_SESSION_ID_LEN} characters", "invalid_request_error")
        query = _last_user_query(messages)
        allow_private = request.headers.get("x-cortext-allow-private", "").strip().lower() == "true"
        # Sanitize the whole query before cutting the preview: truncating first could leave a
        # partial secret too short for the redaction patterns to match.
        preview = sanitize_text(query)[:_QUERY_LOG_CHARS]
        logger.info("Incoming query session=%r chars=%d: %r", session_id, len(query), preview)
        try:
            delta = await resolver.resolve(query, session_id, allow_private) if query else ""
        except Exception:
            # Fail open: memory problems must never block the user's request.
            logger.error("Context resolution failed session=%r; forwarding without context", session_id, exc_info=True)
            resolver.ledger.reset(session_id)
            delta = ""
        if delta:
            logger.info(
                "Resolved delta session=%r entities=%d edges=%d",
                session_id,
                *_delta_counts(delta),
            )
        else:
            logger.info("Resolved delta session=%r entities=0 edges=0", session_id)
        if delta:
            payload["messages"] = _inject_delta(messages, delta)

        headers = {"Content-Type": "application/json"}
        api_key = config.upstream_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        elif not config.api_key and request.headers.get("authorization"):
            headers["Authorization"] = request.headers["authorization"]

        streaming = bool(payload.get("stream"))
        if streaming:
            # Compressed SSE cannot be line-parsed for distillation; ask for plaintext.
            headers["Accept-Encoding"] = "identity"
        upstream_request = http.build_request("POST", chat_url, content=json.dumps(payload), headers=headers)
        try:
            upstream = await http.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.error("Upstream request failed session=%r: %r; session reset", session_id, exc)
            resolver.ledger.reset(session_id)
            return _error(502, f"Upstream request failed: {exc!r}", "upstream_error")

        content_type = upstream.headers.get("content-type")
        if streaming and upstream.status_code < 400:
            logger.info("Upstream status=%d session=%r (streaming)", upstream.status_code, session_id)
        if upstream.status_code >= 400 or not streaming:
            try:
                body = await upstream.aread()
            except httpx.HTTPError as exc:
                logger.error("Upstream read failed session=%r: %r; session reset", session_id, exc)
                resolver.ledger.reset(session_id)
                return _error(502, f"Upstream read failed: {exc!r}", "upstream_error")
            finally:
                await upstream.aclose()
            logger.info("Upstream status=%d session=%r", upstream.status_code, session_id)
            if upstream.status_code >= 400:
                logger.warning("Upstream error status=%d; session %r reset", upstream.status_code, session_id)
                resolver.ledger.reset(session_id)
            else:
                schedule_distillation(session_id, query, _completion_text(body), allow_private)
            return Response(body, status_code=upstream.status_code, media_type=content_type)

        async def relay() -> AsyncIterator[bytes]:
            collected: list[bytes] = []
            completed = False
            try:
                async for chunk in upstream.aiter_bytes():
                    collected.append(chunk)
                    yield chunk
                completed = True
            except (GeneratorExit, asyncio.CancelledError):
                logger.warning("Client disconnected mid-stream session=%r; session reset", session_id)
                resolver.ledger.reset(session_id)
                await asyncio.shield(upstream.aclose())
                raise
            except httpx.HTTPError as exc:
                logger.error("Upstream stream interrupted session=%r: %r; session reset", session_id, exc)
                resolver.ledger.reset(session_id)
                error = {"error": {"message": f"Upstream stream interrupted: {exc!r}", "type": "upstream_error"}}
                yield f"data: {json.dumps(error)}\n\n".encode()
            finally:
                await asyncio.shield(upstream.aclose())
            if not completed:
                return
            raw = b"".join(collected)
            finished, errored = _stream_outcome(raw)
            if errored or not finished:
                logger.warning(
                    "Stream %s session=%r; not distilling, session reset",
                    "carried an error event" if errored else "ended without finish_reason/[DONE]",
                    session_id,
                )
                resolver.ledger.reset(session_id)
                return
            schedule_distillation(session_id, query, _stream_text(raw), allow_private)

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type=content_type or "text/event-stream",
            headers={"Cache-Control": "no-cache"},
            background=BackgroundTask(upstream.aclose),
        )

    return app
