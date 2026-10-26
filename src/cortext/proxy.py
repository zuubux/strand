"""OpenAI-compatible streaming proxy that injects novel graph context into the system prompt."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Config
from .distill import DistillationEngine
from .resolver import ContextResolver

DEFAULT_SESSION_ID = "default"
_UPSTREAM_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


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
    """Prepend `delta` to the first system message, or insert a new one at index 0."""
    out = list(messages)
    for i, message in enumerate(out):
        if isinstance(message, dict) and message.get("role") == "system":
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


def _stream_text(raw: bytes) -> str:
    """Concatenate delta content from a collected SSE stream."""
    parts: list[str] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            delta = json.loads(data)["choices"][0]["delta"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if isinstance(delta, str):
            parts.append(delta)
    return "".join(parts)


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
    engine = distill_engine if distill_engine is not None else DistillationEngine(resolver.db)
    background_tasks: set[asyncio.Task[Any]] = set()

    def schedule_distillation(session_id: str, query: str, response: str) -> None:
        """Fire-and-forget; process_exchange swallows its own errors."""
        if not query or not response:
            return
        task = asyncio.create_task(engine.process_exchange(session_id, query, response))
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

    app = FastAPI(title="Cortext", lifespan=lifespan)
    app.state.background_tasks = background_tasks

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            return _error(400, "Request body must be valid JSON", "invalid_request_error")
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            return _error(400, "'messages' must be a list", "invalid_request_error")

        session_id = str(
            payload.pop("session_id", None) or request.headers.get("x-session-id") or DEFAULT_SESSION_ID
        )
        query = _last_user_query(messages)
        delta = await resolver.resolve(query, session_id) if query else ""
        if delta:
            payload["messages"] = _inject_delta(messages, delta)

        headers = {"Content-Type": "application/json"}
        api_key = config.upstream_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        elif request.headers.get("authorization"):
            headers["Authorization"] = request.headers["authorization"]

        streaming = bool(payload.get("stream"))
        upstream_request = http.build_request("POST", chat_url, content=json.dumps(payload), headers=headers)
        try:
            upstream = await http.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            resolver.ledger.reset(session_id)
            return _error(502, f"Upstream request failed: {exc!r}", "upstream_error")

        content_type = upstream.headers.get("content-type")
        if upstream.status_code >= 400 or not streaming:
            try:
                body = await upstream.aread()
            except httpx.HTTPError as exc:
                resolver.ledger.reset(session_id)
                return _error(502, f"Upstream read failed: {exc!r}", "upstream_error")
            finally:
                await upstream.aclose()
            if upstream.status_code >= 400:
                resolver.ledger.reset(session_id)
            else:
                schedule_distillation(session_id, query, _completion_text(body))
            return Response(body, status_code=upstream.status_code, media_type=content_type)

        async def relay() -> AsyncIterator[bytes]:
            collected: list[bytes] = []
            completed = False
            try:
                async for chunk in upstream.aiter_raw():
                    collected.append(chunk)
                    yield chunk
                completed = True
            except httpx.HTTPError:
                resolver.ledger.reset(session_id)
            finally:
                await upstream.aclose()
            if completed:
                schedule_distillation(session_id, query, _stream_text(b"".join(collected)))

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type=content_type or "text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return app
