"""OpenAI-compatible streaming proxy that injects novel graph context into the system prompt."""

import asyncio
import copy
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .common import safe_exc, strip_think
from .config import Config, save_config_value
from .distill import DistillationEngine
from .host import get_host_context, redact_host_context
from .logging import get_logger
from .onboarding import (
    ONBOARD_TRIGGER,
    MODE_DAY_ZERO,
    MODE_TRIAGE,
    ONBOARDING_MODE,
    count_operators,
    is_onboarding_completed,
    onboarding_directive,
    reanchor_generic_user,
    telemetry_preference,
)
from .resolver import ContextResolver
from .sanitize import sanitize_text
from .session import IMAGE_PART_PREFIX, MAX_SESSION_ID_LEN
from .tools.workspace import WORKSPACE_TOOLS, dispatch_tool_call, mutation_record, restore_backup

logger = get_logger("proxy")
DEFAULT_SESSION_ID = "default"
_UPSTREAM_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
MAX_CONCURRENT_DISTILLATIONS = 4
MAX_ONBOARDING_SESSIONS = 1000
MAX_REQUEST_BYTES = 10 * 1024 * 1024  # POST /v1/chat/completions body cap (HTTP 413 beyond this)
MAX_TOOL_ITERATIONS = 5  # local tool rounds per request before a final answer is forced
_LOCAL_TOOL_NAMES = frozenset(t["function"]["name"] for t in WORKSPACE_TOOLS)
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


def _system_text(messages: list[Any]) -> str:
    """Concatenated text of every system/developer message the client sent."""
    return "\n".join(
        _text_of(m.get("content"))
        for m in messages
        if isinstance(m, dict) and m.get("role") in _CONTEXT_ROLES
    )


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


# Response header telling the UI what the resolved context delta held (drives the grounding pulse).
GROUNDING_HEADER = "X-Strand-Grounding"
# Response header carrying this request's snapshot-tracked workspace mutations (JSON list; drives UI undo pills).
MUTATIONS_HEADER = "X-Strand-Mutations"
MAX_MUTATION_RECORDS = 20

_DELTA_SECTIONS = {
    "Entities:": "entities",
    "Relations:": "edges",
    "[Persona Stance]": "traits",
    "[Operational Directives]": "directives",
    "[Interaction Preferences]": "preferences",
    "[Reality Check - Attribute Discrepancy]": "discrepancies",
}


def _delta_counts(delta: str) -> dict[str, int]:
    """Per-category item counts in a formatted context delta.

    `[Working Memory]` is only a label: its `Entities:`/`Relations:` blocks count as entities/edges.
    Persona sections count every body line (the persona anchor line has no `- ` prefix).
    """
    counts = dict.fromkeys(_DELTA_SECTIONS.values(), 0)
    section = ""
    for line in delta.splitlines():
        if line in _DELTA_SECTIONS:
            section = _DELTA_SECTIONS[line]
        elif line == "[Working Memory]" or line.startswith("## "):
            section = ""
        elif section == "discrepancies":
            counts[section] += line.startswith("- Discrepancy on ")
        elif section and line.strip() and (line.startswith("- ") or section in ("traits", "directives", "preferences")):
            counts[section] += 1
    return counts


def _delta_log_fields(delta: str) -> str:
    """`entities=X edges=Y` plus any non-zero personality counts."""
    counts = _delta_counts(delta)
    fields = [f"entities={counts['entities']}", f"edges={counts['edges']}"]
    fields += [f"{key}={counts[key]}" for key in ("traits", "directives", "preferences", "discrepancies") if counts[key]]
    return " ".join(fields)


def _merge_tools(existing: Any) -> list[Any]:
    """Client-declared tools plus WORKSPACE_TOOLS (client definitions win on name clashes)."""
    merged = list(existing) if isinstance(existing, list) else []
    declared = {t["function"].get("name") for t in merged if isinstance(t, dict) and isinstance(t.get("function"), dict)}
    return merged + [t for t in WORKSPACE_TOOLS if t["function"]["name"] not in declared]


_MESSAGE_CORE_KEYS = ("role", "content", "tool_calls", "index")


def _merge_extra(dst: dict[str, Any], src: dict[str, Any], skip: tuple[str, ...], concat: bool = False) -> None:
    """Copy provider-specific fields (thought_signature, reasoning_content, extra_content...) verbatim.

    Nested dicts merge recursively; with `concat` string deltas append (streamed reasoning text),
    otherwise later values replace earlier ones (signatures arrive whole).
    """
    for key, value in src.items():
        if key in skip:
            continue
        current = dst.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _merge_extra(current, value, (), concat)
        elif concat and isinstance(value, str) and isinstance(current, str):
            dst[key] = current + value
        else:
            dst[key] = copy.deepcopy(value)


def _parse_tool_calls(body: bytes, streaming: bool) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """(assistant text, normalized tool calls, extra assistant message fields) from a completion or SSE body.

    Provider-specific fields on the message and on each tool call are preserved so the assistant turn can
    be echoed upstream losslessly (Gemini rejects tool results whose call lost its thought_signature).
    """
    calls: dict[int, dict[str, Any]] = {}
    text: list[str] = []
    extras: dict[str, Any] = {}

    def absorb(items: Any, merge: bool) -> None:
        for pos, item in enumerate(items if isinstance(items, list) else []):
            if not isinstance(item, dict):
                continue
            index = item.get("index", pos) if merge and isinstance(item.get("index", pos), int) else pos
            call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            call["id"] = item.get("id") or call["id"]
            _merge_extra(call, item, ("id", "type", "function", "index"))
            function = item.get("function")
            if isinstance(function, dict):
                _merge_extra(call["function"], function, ("name", "arguments"))
                call["function"]["name"] += function.get("name") or ""
                arguments = function.get("arguments")
                call["function"]["arguments"] += arguments if isinstance(arguments, str) else json.dumps(arguments or {})

    try:
        if streaming:
            for line in body.decode("utf-8", errors="replace").split("\n"):
                line = line.strip()
                data = line[5:].strip() if line.startswith("data:") else ""
                if not data or data == "[DONE]":
                    continue
                try:
                    choices = json.loads(data)["choices"]
                except (ValueError, KeyError, TypeError):
                    continue
                for choice in choices if isinstance(choices, list) else []:
                    delta = choice.get("delta") if isinstance(choice, dict) and choice.get("index", 0) == 0 else None
                    if isinstance(delta, dict):
                        text.append(_delta_text(delta.get("content")))
                        _merge_extra(extras, delta, _MESSAGE_CORE_KEYS, concat=True)
                        absorb(delta.get("tool_calls"), True)
        else:
            message = json.loads(body)["choices"][0]["message"]
            text.append(_text_of(message.get("content")))
            _merge_extra(extras, message, _MESSAGE_CORE_KEYS)
            absorb(message.get("tool_calls"), False)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return "", [], {}
    ordered = [calls[i] for i in sorted(calls)]
    for n, call in enumerate(ordered):
        call["id"] = call["id"] or f"call_{n}"
    return "".join(text), [c for c in ordered if c["function"]["name"]], extras


class _ReplayStream(httpx.AsyncByteStream):
    """Already-buffered chunks followed by the rest of a still-open upstream stream."""

    def __init__(self, head: list[bytes], rest: AsyncIterator[bytes], upstream: httpx.Response) -> None:
        self._head, self._rest, self._upstream = head, rest, upstream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._head:
            yield chunk
        async for chunk in self._rest:
            yield chunk

    async def aclose(self) -> None:
        await self._upstream.aclose()


async def _buffer_until_decided(upstream: httpx.Response) -> tuple[bytes, httpx.Response | None]:
    """Buffer an SSE turn until it is either a tool-call turn or a plain answer.

    Returns (body, None) with the stream fully consumed when tool-call deltas appear (they are
    never relayed). As soon as assistant text or a finish marker is seen without tool-call
    deltas, returns (b"", live): a response replaying the buffered head, then the remainder.
    """
    head: list[bytes] = []
    seen = b""
    iterator = upstream.aiter_bytes()
    try:
        async for chunk in iterator:
            head.append(chunk)
            seen += chunk
            if b'"tool_calls"' in seen:
                async for rest in iterator:
                    head.append(rest)
                return b"".join(head), None
            if _stream_text(seen) or any(_stream_outcome(seen)):
                headers = {"content-type": upstream.headers["content-type"]} if "content-type" in upstream.headers else None
                stream = _ReplayStream(head, iterator, upstream)
                return b"", httpx.Response(upstream.status_code, headers=headers, stream=stream)
    except BaseException:
        await asyncio.shield(upstream.aclose())
        raise
    await upstream.aclose()
    return b"".join(head), None


async def _run_tool_call_full(
    call: dict[str, Any], roots: list[Path], redact: bool = True
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Execute one tool call locally: the ephemeral `role: tool` message plus an optional image turn.

    `read_image` results are multimodal: the tool message keeps only lightweight metadata (tool
    messages must be text) and the `data_uri` travels upstream as an `image_url` content block in
    a follow-up user message. Both are runtime-only; `SessionLedger.compact_history` stubs them.
    """
    function = call["function"]
    try:
        arguments = json.loads(function["arguments"] or "{}")
    except ValueError:
        arguments = None  # dispatch_tool_call reports invalid_arguments
    result = await asyncio.to_thread(dispatch_tool_call, function["name"], arguments, roots)
    followup: dict[str, Any] | None = None
    data_uri = result.get("data_uri") if function["name"] == "read_image" else None
    if isinstance(data_uri, str):
        result = {k: v for k, v in result.items() if k != "data_uri"}
        result["note"] = "The image is attached in the next message."
        followup = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{IMAGE_PART_PREFIX}{result.get('path', '')}"},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    content = json.dumps(result)
    if redact:  # SEC-PRIV-007: scrub secrets from tool output before it goes upstream
        content = sanitize_text(content)
    return {"role": "tool", "tool_call_id": call["id"], "content": content}, followup


async def _run_tool_call(
    call: dict[str, Any], roots: list[Path], redact: bool = True
) -> dict[str, Any]:
    """Execute one tool call locally; the result is an ephemeral `role: tool` message."""
    return (await _run_tool_call_full(call, roots, redact))[0]


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
    onboarding_sessions: dict[str, str] = {}  # session -> onboarding mode; insertion-ordered, bounded
    telemetry_choices: dict[str, bool] = {}  # session -> applied onboarding hardware choice; bounded
    distill_slots = asyncio.Semaphore(MAX_CONCURRENT_DISTILLATIONS)

    async def _distill(session_id: str, query: str, response: str) -> None:
        async with distill_slots:
            await engine.process_exchange(session_id, query, response)
            try:  # Model B may have declared the operator: fold any generic `User` node into it
                await reanchor_generic_user(resolver.db)
            except Exception:
                logger.warning("Operator re-anchor failed session=%r", session_id, exc_info=True)
            if session_id in onboarding_sessions:
                await _apply_telemetry_choice(session_id)

    async def _apply_telemetry_choice(session_id: str) -> None:
        """SEC-PRIV-002: honour the operator's conversational hardware opt-in / opt-out.

        Model B distills `(<Operator>) --[prefers]--> (Host Telemetry Enabled | On-Demand)`. The
        choice is applied to the live session (so the very next turn carries, or withholds, the
        specs) and persisted as `enable_host_telemetry` in config.json (atomic, 0600).
        """
        try:
            choice = await telemetry_preference(resolver.db)
            if choice is None or telemetry_choices.get(session_id) is choice:
                return
            telemetry_choices[session_id] = choice
            while len(telemetry_choices) > MAX_ONBOARDING_SESSIONS:
                del telemetry_choices[next(iter(telemetry_choices))]
            resolver.ledger.set_allow_host_telemetry(session_id, choice)
            await asyncio.to_thread(save_config_value, "enable_host_telemetry", choice)
            logger.info("Host telemetry choice session=%r enabled=%s", session_id, choice)
        except Exception:
            logger.warning("Host telemetry choice failed session=%r", session_id, exc_info=True)

    def schedule_distillation(session_id: str, query: str, response: str, allow_private: bool = False) -> None:
        """Fire-and-forget; process_exchange swallows its own errors.

        Never distills an exchange served with private egress context (C1): the reply may quote
        private facts, and distilling it would launder them into public, un-flagged entities.
        """
        if allow_private:
            logger.info("Skipping distillation session=%r: private context allowed for this exchange", session_id)
            return
        if not query or not response or query == ONBOARD_TRIGGER:
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
    app = FastAPI(title="Strand", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

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

    async def post_upstream(payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        request = http.build_request("POST", chat_url, content=json.dumps(payload), headers=headers)
        return await http.send(request, stream=True)

    async def run_tool_loop(
        payload: dict[str, Any], headers: dict[str, str], streaming: bool, mutations: list[dict[str, str]]
    ) -> httpx.Response:
        """JIT tool loop: run workspace tool calls locally and re-post until a final answer.

        Each upstream turn is fully buffered, so tool-call deltas never reach the client; the
        returned response is pre-read and carries only the final turn. Tool calls naming tools
        Strand does not own are passed through to the client untouched. After
        MAX_TOOL_ITERATIONS tool rounds a last request is made with `tool_choice: none`.
        """
        roots = config.resolved_workspace_roots()
        convo = list(payload["messages"])
        for round_no in range(MAX_TOOL_ITERATIONS + 1):
            guard = round_no == MAX_TOOL_ITERATIONS
            body_payload = {**payload, "messages": convo}
            if guard:
                logger.warning("Tool loop hit %d iterations; forcing a final answer", MAX_TOOL_ITERATIONS)
                body_payload["tool_choice"] = "none"
            upstream = await post_upstream(body_payload, headers)
            if streaming and upstream.status_code < 400 and not guard:
                body, live = await _buffer_until_decided(upstream)
                if live is not None:  # plain answer: relay it live, tool deltas never appeared
                    return live
            else:
                try:
                    body = await upstream.aread()
                finally:
                    await upstream.aclose()
            text, calls, extras = "", [], {}
            if upstream.status_code < 400 and not guard:
                text, calls, extras = _parse_tool_calls(body, streaming)
            if not calls or any(c["function"]["name"] not in _LOCAL_TOOL_NAMES for c in calls):
                break
            logger.info("Executing %d local tool call(s) round=%d", len(calls), round_no + 1)
            convo.append({**extras, "role": "assistant", "content": text or None, "tool_calls": calls})
            images: list[dict[str, Any]] = []
            for call in calls:
                tool_message, followup = await _run_tool_call_full(call, roots, config.redact_secrets)
                convo.append(tool_message)
                try:
                    record = mutation_record(call["function"]["name"], json.loads(tool_message["content"]))
                except ValueError:
                    record = None
                if record is not None and len(mutations) < MAX_MUTATION_RECORDS:
                    mutations.append(record)
                if followup is not None:
                    images.append(followup)
            convo.extend(images)  # tool results must stay contiguous after the assistant tool_calls
        content_type = upstream.headers.get("content-type")
        return httpx.Response(
            upstream.status_code, headers={"content-type": content_type} if content_type else None, content=body
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/backups/restore")
    async def restore_workspace_backup(request: Request) -> Response:
        """Undo one journaled workspace mutation (UI undo pill); same sandbox as the `restore_backup` tool."""
        try:
            body = await request.json()
        except (ValueError, RecursionError):
            return _error(400, "Request body must be valid JSON", "invalid_request_error")
        backup_id = body.get("backup_id") if isinstance(body, dict) else None
        if not isinstance(backup_id, str) or not backup_id.strip():
            return _error(400, "'backup_id' must be a non-empty string", "invalid_request_error")
        roots = config.resolved_workspace_roots()
        try:
            result = await asyncio.to_thread(restore_backup, backup_id, roots)
        except Exception as exc:  # noqa: BLE001 - fail closed, never leak a traceback
            return _error(500, f"restore failed: {type(exc).__name__}", "restore_error")
        if "error" in result:
            return _error(409, str(result.get("message", result["error"])), str(result["error"]))
        return JSONResponse(result)

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
        allow_private = request.headers.get("x-strand-allow-private", "").strip().lower() == "true"
        # Sanitize the whole query before cutting the preview: truncating first could leave a
        # partial secret too short for the redaction patterns to match.
        preview = sanitize_text(query)[:_QUERY_LOG_CHARS]
        logger.info("Incoming query length=%d session=%r", len(query), session_id)
        logger.debug("Incoming query preview session=%r: %r", session_id, preview)
        resolution_failed = False
        try:
            delta = await resolver.resolve(query, session_id, allow_private) if query else ""
        except Exception:
            resolution_failed = True
            # Fail open: memory problems must never block the user's request.
            logger.error("Context resolution failed session=%r; forwarding without context", session_id, exc_info=True)
            resolver.ledger.reset(session_id)
            delta = ""
        grounding = _delta_log_fields(delta) if delta else "entities=0 edges=0"
        if not resolution_failed:
            # ERG-CTX-005: clients resend only user/assistant history, so replay earlier blocks
            # the ledger marked seen but the client's system prompt no longer carries.
            replay = resolver.ledger.replay_context(session_id, _system_text(messages), exclude=delta)
            if replay:
                delta = "\n\n".join([*replay, delta] if delta else replay)
                logger.info("Replayed %d context block(s) session=%r", len(replay), session_id)
        logger.info("Resolved delta session=%r %s", session_id, grounding)
        mode = request.headers.get("x-strand-mode", "").strip().lower()
        try:
            explicit = mode == ONBOARDING_MODE
            telemetry = request.headers.get("x-strand-host-telemetry", "").strip().lower()
            if telemetry in ("1", "true", "yes", "on", "0", "false", "no", "off"):
                resolver.ledger.set_allow_host_telemetry(session_id, telemetry in ("1", "true", "yes", "on"))
            graduated = not resolution_failed and not explicit and session_id not in onboarding_sessions and await is_onboarding_completed(resolver.db)
            if resolution_failed or graduated:
                pass  # fail open / already onboarded: no directive, no onboarding flag
            elif session_id not in onboarding_sessions and (explicit or await count_operators(resolver.db) == 0):
                # No explicit operator yet: Day Zero. /onboard with one on record: intent triage.
                has_operator = await count_operators(resolver.db) > 0
                onboarding_sessions[session_id] = MODE_TRIAGE if has_operator else MODE_DAY_ZERO
                logger.info("Onboarding session=%r mode=%s", session_id, onboarding_sessions[session_id])
            while len(onboarding_sessions) > MAX_ONBOARDING_SESSIONS:
                del onboarding_sessions[next(iter(onboarding_sessions))]
            if session_id in onboarding_sessions and not resolution_failed:
                host_info = get_host_context()
                if not (config.enable_host_telemetry is True or resolver.ledger.allow_host_telemetry(session_id)):
                    host_info = redact_host_context(host_info)  # SEC-PRIV-002: fail closed
                user_turns = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")
                decision: bool | None = None
                if user_turns >= 4 and onboarding_sessions[session_id] == MODE_DAY_ZERO:
                    if config.enable_host_telemetry is True or resolver.ledger.allow_host_telemetry(session_id):
                        decision = True
                    else:
                        decision = await telemetry_preference(resolver.db)
                directive = onboarding_directive(
                    host_info, onboarding_sessions[session_id], user_turns <= 1, user_turns, decision
                )
                delta = f"{directive}\n\n{delta}" if delta else directive
        except Exception:
            logger.error("Onboarding setup failed session=%r; continuing without it", session_id, exc_info=True)
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
        # Earlier-turn raw tool output and media are transient: keep only the syntheses.
        payload["messages"] = resolver.ledger.compact_history(payload["messages"])
        mutations: list[dict[str, str]] = []
        try:
            if config.tools_enabled:
                payload["tools"] = _merge_tools(payload.get("tools"))
                upstream = await run_tool_loop(payload, headers, streaming, mutations)
            else:
                upstream = await post_upstream(payload, headers)
        except httpx.HTTPError as exc:
            logger.error("Upstream request failed session=%r: %s; session reset", session_id, safe_exc(exc))
            resolver.ledger.reset(session_id)
            return _error(502, f"Upstream request failed: {safe_exc(exc)}", "upstream_error")

        out_headers = {GROUNDING_HEADER: grounding}
        if mutations:
            out_headers[MUTATIONS_HEADER] = json.dumps(mutations, separators=(",", ":"))
        content_type = upstream.headers.get("content-type")
        if streaming and upstream.status_code < 400:
            logger.info("Upstream status=%d session=%r (streaming)", upstream.status_code, session_id)
        if upstream.status_code >= 400 or not streaming:
            try:
                body = await upstream.aread()
            except httpx.HTTPError as exc:
                logger.error("Upstream read failed session=%r: %s; session reset", session_id, safe_exc(exc))
                resolver.ledger.reset(session_id)
                return _error(502, f"Upstream read failed: {safe_exc(exc)}", "upstream_error")
            finally:
                await upstream.aclose()
            logger.info("Upstream status=%d session=%r", upstream.status_code, session_id)
            if upstream.status_code >= 400:
                logger.warning("Upstream error status=%d; session %r reset", upstream.status_code, session_id)
                resolver.ledger.reset(session_id)
            else:
                schedule_distillation(session_id, query, _completion_text(body), allow_private)
            return Response(
                body,
                status_code=upstream.status_code,
                media_type=content_type,
                headers=out_headers,
            )

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
                logger.error("Upstream stream interrupted session=%r: %s; session reset", session_id, safe_exc(exc))
                resolver.ledger.reset(session_id)
                error = {"error": {"message": f"Upstream stream interrupted: {safe_exc(exc)}", "type": "upstream_error"}}
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
            headers={"Cache-Control": "no-cache", **out_headers},
            background=BackgroundTask(upstream.aclose),
        )

    return app
