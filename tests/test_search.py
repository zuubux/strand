import asyncio
import json
import sqlite3
from urllib.parse import parse_qs

import httpx

from strand.config import Config
from strand.db import DatabaseManager
from strand.proxy import create_app
from strand.resolver import ContextResolver
from strand.session import SessionLedger
from strand.tools import search
from strand.tools.search import UNAVAILABLE, WEB_SEARCH_TOOL, web_search
from strand.tools.workspace import dispatch_tool_call

SAMPLE_HTML = """
<html><body>
<div class="result results_links"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fcar%3Fa%3D1&amp;rut=abc">Example &amp; <b>Car</b> Specs</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fcar">Horsepower is <b>300</b> hp.</a></div>
<div class="result result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Sponsored</a>
<a class="result__snippet" href="x">ad text</a></div>
<div class="result"><a class="result__a" href="https://second.test/page">Second</a>
<a class="result__snippet" href="https://second.test/page">Second snippet</a></div>
</body></html>
"""


def patch_client(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        search,
        "_make_client",
        lambda: httpx.Client(transport=httpx.MockTransport(wrapped), timeout=search.SEARCH_TIMEOUT),
    )
    return seen


def test_web_search_parses_titles_urls_snippets(monkeypatch):
    seen = patch_client(monkeypatch, lambda r: httpx.Response(200, text=SAMPLE_HTML))
    results = web_search("car specs")
    assert results == [
        {"title": "Example & Car Specs", "url": "https://example.com/car?a=1", "snippet": "Horsepower is 300 hp."},
        {"title": "Second", "url": "https://second.test/page", "snippet": "Second snippet"},
    ]
    request = seen[0]
    assert request.method == "POST" and str(request.url) == search.SEARCH_URL
    assert parse_qs(request.content.decode()) == {"q": ["car specs"]}
    assert request.headers["user-agent"].startswith("Mozilla/5.0")


def test_web_search_respects_max_results(monkeypatch):
    patch_client(monkeypatch, lambda r: httpx.Response(200, text=SAMPLE_HTML))
    assert len(web_search("x", max_results=1)) == 1


def test_web_search_scrubs_query_with_sanitize_text(monkeypatch):
    seen = patch_client(monkeypatch, lambda r: httpx.Response(200, text=SAMPLE_HTML))
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"
    web_search(f"how to use {secret} key")
    sent = parse_qs(seen[0].content.decode())["q"][0]
    assert secret not in sent and "[REDACTED_SECRET]" in sent


def test_web_search_graceful_failures(monkeypatch):
    for status in (202, 429, 500):
        patch_client(monkeypatch, lambda r, s=status: httpx.Response(s, text=SAMPLE_HTML))
        assert web_search("q") == {"error": UNAVAILABLE}
    patch_client(monkeypatch, lambda r: httpx.Response(200, text="<html>nothing</html>"))
    assert web_search("q") == {"error": UNAVAILABLE}

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    patch_client(monkeypatch, boom)
    assert web_search("q") == {"error": UNAVAILABLE}
    assert web_search("   ") == {"error": UNAVAILABLE}


def test_schema_and_dispatch(monkeypatch):
    assert WEB_SEARCH_TOOL["function"]["name"] == "web_search"
    assert WEB_SEARCH_TOOL["function"]["parameters"]["required"] == ["query"]
    patch_client(monkeypatch, lambda r: httpx.Response(200, text=SAMPLE_HTML))
    result = dispatch_tool_call("web_search", {"query": "car"}, [])
    assert result["results"][0]["url"] == "https://example.com/car?a=1"
    assert dispatch_tool_call("web_search", {}, [])["error"] == "invalid_arguments"


def _dump(db_path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        names = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
        ).fetchall()
        return "".join(repr(r) for (n,) in names for r in conn.execute(f'SELECT * FROM "{n}"'))
    finally:
        conn.close()


def test_proxy_executes_web_search_tool_call_and_never_persists(tmp_path, monkeypatch):
    patch_client(monkeypatch, lambda r: httpx.Response(200, text=SAMPLE_HTML))
    seen: list[dict] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            call = {
                "id": "call_ws",
                "type": "function",
                "function": {"name": "web_search", "arguments": json.dumps({"query": "car specs"})},
            }
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "The car makes 300 hp."}, "stop"
        return httpx.Response(200, json={"choices": [{"index": 0, "message": message, "finish_reason": finish}]})

    async def scenario():
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        db_path = tmp_path / "m.db"
        async with DatabaseManager(db_path) as db:
            config = Config(upstream_url="http://upstream.test/v1", upstream_api_key="sk-test", web_search_enabled=True)
            app = create_app(config, ContextResolver(db, SessionLedger()), upstream)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://strand") as client:
                body = {"messages": [{"role": "user", "content": "How strong is the car?"}]}
                response = await client.post("/v1/chat/completions", json=body)
                tasks = list(app.state.background_tasks)
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.aclose()
        return response, _dump(db_path)

    response, dump = asyncio.run(scenario())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "The car makes 300 hp."
    assert "web_search" in {t["function"]["name"] for t in seen[0]["tools"]}
    assert len(seen) == 2
    tool = seen[1]["messages"][-1]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_ws"
    assert "Horsepower is 300 hp." in tool["content"]
    assert "Horsepower is 300 hp." not in dump  # ephemeral boundary
