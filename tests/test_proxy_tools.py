import asyncio
import json
from contextlib import asynccontextmanager

import httpx

from strand.config import Config
from strand.db import DatabaseManager
from strand.proxy import MAX_TOOL_ITERATIONS, create_app
from strand.resolver import ContextResolver
from strand.session import TOOL_EVICT_MIN_CHARS, SessionLedger

SECRET_LINE = "ZEBRAQUARTZ_UNIQUE_PAYLOAD_LINE"


def run(coro):
    return asyncio.run(coro)


def tool_call_body(name="read_file", arguments=None, call_id="call_1"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {})}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def text_body(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def sse_tool_call(name, arguments, call_id="call_s"):
    half = len(arguments) // 2
    first = {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments[:half]}}
    second = {"index": 0, "function": {"arguments": arguments[half:]}}
    return sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [first]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [second]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )


def sse_text(text):
    return sse(
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    )


@asynccontextmanager
async def tool_proxy(tmp_path, responder, tools_enabled=True):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        return responder(len(seen), payload)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(f"{SECRET_LINE}\nsecond line\n")
    (workspace / "quokka_listing_only.dat").write_text("x")
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    db_path = tmp_path / "m.db"
    async with DatabaseManager(db_path) as db:
        config = Config(
            upstream_url="http://upstream.test/v1",
            upstream_api_key="sk-test",
            workspace_roots=[str(workspace)],
            tools_enabled=tools_enabled,
        )
        app = create_app(config, ContextResolver(db, SessionLedger()), upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://strand") as client:
            try:
                yield client, seen, workspace, db_path, app
            finally:
                await upstream.aclose()


async def drain(app):
    tasks = list(app.state.background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def dump_db(db_path) -> str:
    """Every row of every ordinary table as text (virtual vec0 tables need the extension; skipped)."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
        ).fetchall()
        rows: list[str] = []
        for (name,) in tables:
            try:
                rows += [repr(r) for r in conn.execute(f'SELECT * FROM "{name}"')]
            except sqlite3.OperationalError:
                continue
        return "\n".join(rows)
    finally:
        conn.close()


QUESTION = "Please show me what is inside the file notes.txt in my Strand workspace project"


def test_read_file_loop_nonstreaming(tmp_path):
    def responder(n, payload):
        if n == 1:
            return httpx.Response(200, json=tool_call_body(arguments={"path": str(tmp_path / "ws" / "notes.txt")}))
        return httpx.Response(200, json=text_body("Here is the file content: it starts with a marker line."))

    async def scenario():
        async with tool_proxy(tmp_path, responder) as (client, seen, _, db_path, app):
            response = await client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": QUESTION}]}
            )
            await drain(app)
            return response, seen, dump_db(db_path)

    response, seen, dump = run(scenario())
    assert response.status_code == 200
    assert "Here is the file content" in response.json()["choices"][0]["message"]["content"]
    assert len(seen) == 2
    assert {t["function"]["name"] for t in seen[0]["tools"]} == {"read_file", "list_directory", "grep_workspace", "write_file", "patch_file", "restore_backup"}
    roles = [m["role"] for m in seen[1]["messages"]]
    assert roles[-2:] == ["assistant", "tool"]
    assistant, tool = seen[1]["messages"][-2:]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert tool["tool_call_id"] == "call_1"
    assert SECRET_LINE in json.loads(tool["content"])["content"]
    assert SECRET_LINE not in dump  # raw tool output never reaches memory.db


def test_list_directory_loop_streaming(tmp_path):
    def responder(n, payload):
        if n == 1:
            body = sse_tool_call("list_directory", json.dumps({"path": str(tmp_path / "ws")}))
        else:
            body = sse_text("Here is the file content listing: notes.txt.")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def scenario():
        async with tool_proxy(tmp_path, responder) as (client, seen, _, db_path, app):
            response = await client.post(
                "/v1/chat/completions",
                json={"stream": True, "messages": [{"role": "user", "content": QUESTION}]},
            )
            await drain(app)
            return response, seen, dump_db(db_path)

    response, seen, dump = run(scenario())
    assert response.status_code == 200
    assert "tool_calls" not in response.text.replace('"finish_reason": "stop"', "")
    assert "Here is the file content listing" in response.text
    assert len(seen) == 2
    tool = seen[1]["messages"][-1]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_s"
    assert "quokka_listing_only.dat" in tool["content"]
    assert "quokka_listing_only" not in dump


def test_tools_disabled_not_attached_or_executed(tmp_path):
    def responder(n, payload):
        return httpx.Response(200, json=tool_call_body(arguments={"path": str(tmp_path / "ws" / "notes.txt")}))

    async def scenario():
        async with tool_proxy(tmp_path, responder, tools_enabled=False) as (client, seen, *_):
            response = await client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": QUESTION}]}
            )
            return response, seen

    response, seen = run(scenario())
    assert len(seen) == 1 and "tools" not in seen[0]
    assert response.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"


def test_loop_guard_caps_iterations(tmp_path):
    def responder(n, payload):
        if payload.get("tool_choice") == "none":
            return httpx.Response(200, json=text_body("Giving up looping; final answer here."))
        return httpx.Response(200, json=tool_call_body(arguments={"path": str(tmp_path / "ws" / "notes.txt")}, call_id=f"c{n}"))

    async def scenario():
        async with tool_proxy(tmp_path, responder) as (client, seen, *_):
            response = await client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": QUESTION}]}
            )
            return response, seen

    response, seen = run(scenario())
    assert len(seen) == MAX_TOOL_ITERATIONS + 1
    assert seen[-1]["tool_choice"] == "none"
    assert "final answer" in response.json()["choices"][0]["message"]["content"]


def test_path_outside_workspace_denied(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_SECRET")

    def responder(n, payload):
        if n == 1:
            return httpx.Response(200, json=tool_call_body(arguments={"path": str(outside)}))
        return httpx.Response(200, json=text_body("I could not read that file."))

    async def scenario():
        async with tool_proxy(tmp_path, responder) as (client, seen, *_):
            await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": QUESTION}]})
            return seen

    seen = run(scenario())
    assert "OUTSIDE_SECRET" not in seen[1]["messages"][-1]["content"]


def test_ledger_compacts_earlier_tool_output():
    big = json.dumps({"content": "x" * (TOOL_EVICT_MIN_CHARS * 2)})
    messages = [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": big},
        {"role": "assistant", "content": "It is a long x file."},
        {"role": "user", "content": [{"type": "text", "text": "now?"}, {"type": "image_url", "image_url": {"url": "data:AAAA"}}]},
        {"role": "tool", "tool_call_id": "b", "content": big},
    ]
    out = SessionLedger.compact_history(messages)
    assert out[2]["tool_call_id"] == "a" and "evicted" in out[2]["content"] and "xxxx" not in out[2]["content"]
    assert out[3] == messages[3]  # the model's synthesis is retained
    assert out[5] == messages[5]  # current-turn output is untouched
    assert out[4] == messages[4]
    assert messages[2]["content"] == big  # input not mutated
    earlier_media = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:AAAA"}}]}, {"role": "user", "content": "next"}]
    assert "data:AAAA" not in json.dumps(SessionLedger.compact_history(earlier_media))


def test_next_turn_sends_placeholder_upstream(tmp_path):
    big = json.dumps({"content": "y" * (TOOL_EVICT_MIN_CHARS * 2)})
    messages = [
        {"role": "user", "content": "read the file"},
        {"role": "tool", "tool_call_id": "a", "content": big},
        {"role": "assistant", "content": "Summary: it is a file of y characters."},
        {"role": "user", "content": "thanks, anything else worth noting?"},
    ]

    async def scenario():
        async with tool_proxy(tmp_path, lambda n, p: httpx.Response(200, json=text_body("No."))) as (client, seen, *_):
            await client.post("/v1/chat/completions", json={"messages": messages})
            return seen

    seen = run(scenario())
    sent = json.dumps(seen[0]["messages"])
    assert "yyyy" not in sent and "Summary: it is a file" in sent


def test_provider_metadata_preserved_nonstreaming(tmp_path):
    def responder(n, payload):
        if n == 1:
            body = tool_call_body(arguments={"path": str(tmp_path / "ws" / "notes.txt")})
            message = body["choices"][0]["message"]
            message["reasoning_content"] = "thinking it over"
            message["tool_calls"][0]["thought_signature"] = "SIG_ABC"
            message["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "SIG_NESTED"}}
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=text_body("done"))

    async def scenario():
        async with tool_proxy(tmp_path, responder) as (client, seen, _, _, app):
            response = await client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": QUESTION}]}
            )
            await drain(app)
            return response, seen

    response, seen = run(scenario())
    assert response.status_code == 200
    assistant = seen[1]["messages"][-2]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "thinking it over"
    call = assistant["tool_calls"][0]
    assert call["thought_signature"] == "SIG_ABC"
    assert call["extra_content"] == {"google": {"thought_signature": "SIG_NESTED"}}
    assert call["id"] == "call_1" and call["function"]["name"] == "read_file"


def test_provider_metadata_preserved_streaming(tmp_path):
    def responder(n, payload):
        if n == 1:
            args = json.dumps({"path": str(tmp_path / "ws" / "notes.txt")})
            first = {
                "index": 0,
                "id": "call_s",
                "type": "function",
                "extra_content": {"google": {"thought_signature": "SIG_STREAM"}},
                "function": {"name": "read_file", "arguments": args},
            }
            body = sse(
                {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [first]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [], "thought_signature": "MSG_SIG"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            )
        else:
            body = sse_text("done")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def scenario():
        async with tool_proxy(tmp_path, responder) as (client, seen, _, _, app):
            await client.post(
                "/v1/chat/completions",
                json={"stream": True, "messages": [{"role": "user", "content": QUESTION}]},
            )
            await drain(app)
            return seen

    seen = run(scenario())
    assistant = seen[1]["messages"][-2]
    assert assistant["thought_signature"] == "MSG_SIG"
    assert assistant["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "SIG_STREAM"}}


def test_sensitive_files_denied_and_source_allowed(tmp_path):
    from strand.tools.workspace import is_denied_file, read_file

    root = tmp_path.resolve()
    (root / ".git").mkdir()
    (root / "src").mkdir()
    (root / "docs").mkdir()
    denied = [".env", ".env.local", "ID_RSA", "deploy.key", ".git/config", "api_token.txt", "cert.PEM"]
    for name in denied:
        (root / name).write_text("TOPSECRET")
        assert is_denied_file(name)
        result = read_file(str(root / name), [root])
        assert result["error"] == "Access denied: sensitive file policy"
        assert "TOPSECRET" not in json.dumps(result)
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "docs" / "guide.md").write_text("# Guide\n")
    for name in ("src/main.py", "docs/guide.md"):
        assert not is_denied_file(name)
        assert "content" in read_file(str(root / name), [root])


def test_tool_output_redacted_before_upstream(tmp_path):
    def responder(n, payload):
        if n == 1:
            return httpx.Response(200, json=tool_call_body(arguments={"path": "leaky.txt"}))
        return httpx.Response(200, json=text_body("done"))

    async def scenario():
        async with tool_proxy(tmp_path, responder) as (client, seen, workspace, *_):
            (workspace / "leaky.txt").write_text(
                "key=sk-live-abcdefghijklmnopqrstuvwx\nAuthorization: Bearer abcdefghij1234567890\n"
            )
            await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": QUESTION}]})
            return seen

    seen = run(scenario())
    tool_msg = seen[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "abcdefghijklmnopqrstuvwx" not in tool_msg["content"]
    assert "abcdefghij1234567890" not in tool_msg["content"]
    assert "[REDACTED_SECRET]" in tool_msg["content"]


def test_run_tool_call_redact_toggle(tmp_path):
    from strand.proxy import _run_tool_call

    (tmp_path / "leaky.txt").write_text("Authorization: Bearer abcdefghij1234567890\n")
    call = {"id": "c", "function": {"name": "read_file", "arguments": json.dumps({"path": "leaky.txt"})}}
    roots = [tmp_path.resolve()]
    assert "abcdefghij1234567890" not in run(_run_tool_call(call, roots))["content"]
    assert "abcdefghij1234567890" in run(_run_tool_call(call, roots, redact=False))["content"]
