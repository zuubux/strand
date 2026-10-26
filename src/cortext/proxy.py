"""OpenAI-compatible streaming proxy that injects novel graph context into the system prompt."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Config
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


def _error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def create_app(
    config: Config,
    resolver: ContextResolver,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the proxy app. The lifespan connects/closes the resolver's DB and the HTTP client."""
    http = client if client is not None else httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)
    chat_url = config.upstream_url.rstrip("/") + "/chat/completions"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await resolver.db.connect()
        try:
            yield
        finally:
            await http.aclose()
            await resolver.db.close()

    app = FastAPI(title="Cortext", lifespan=lifespan)

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
            return Response(body, status_code=upstream.status_code, media_type=content_type)

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError:
                resolver.ledger.reset(session_id)
            finally:
                await upstream.aclose()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type=content_type or "text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return app
