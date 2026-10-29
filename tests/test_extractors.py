import asyncio
import json

import httpx

from strand.db import DatabaseManager
from strand.distill import DistillationEngine
from strand.extractors import LLMExtractor, parse_extraction

QUERY = "I am building the Atlas project with Alice every week."
RESPONSE = "Great, Atlas sounds like a solid project and Alice seems like a strong lead."
GOOD = {
    "entities": [
        {"name": "Alice", "archetype": "person", "summary": "Lead developer"},
        {"name": "Atlas", "archetype": "project", "summary": "Main project"},
    ],
    "edges": [{"source": "Alice", "target": "Atlas", "relation": "works_on"}],
}


def run(coro):
    return asyncio.run(coro)


def extractor_for(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMExtractor("http://slm.test/v1", "tiny", client=client)


def reply(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_valid_json_is_parsed_and_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return reply(json.dumps(GOOD))

    result = run(extractor_for(handler)(QUERY, RESPONSE))
    assert seen["url"] == "http://slm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer ollama"
    assert seen["body"]["model"] == "tiny"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert [e["name"] for e in result["entities"]] == ["Alice", "Atlas"]
    assert result["edges"] == GOOD["edges"]


def test_fenced_json_is_accepted():
    result = run(extractor_for(lambda r: reply("```json\n" + json.dumps(GOOD) + "\n```"))(QUERY, RESPONSE))
    assert len(result["entities"]) == 2 and len(result["edges"]) == 1


def test_invalid_json_falls_back_to_empty():
    for content in ("not json at all", "{broken", "[1, 2]", ""):
        result = run(extractor_for(lambda r, c=content: reply(c))(QUERY, RESPONSE))
        assert result["entities"] == [] and result["edges"] == []


def test_malformed_items_are_dropped():
    result = parse_extraction(
        json.dumps({"entities": [{"name": ""}, "x", {"name": "Ok"}], "edges": [{"source": "a"}, 3]})
    )
    assert [e["name"] for e in result["entities"]] == ["Ok"] and result["edges"] == []


def test_connection_error_and_http_error_fall_back_to_empty():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    for handler in (boom, lambda r: httpx.Response(500, text="oops"), lambda r: httpx.Response(200, json={})):
        result = run(extractor_for(handler)(QUERY, RESPONSE))
        assert result["entities"] == [] and result["edges"] == []


def _distill(tmp_path, extractor):
    async def go():
        async with DatabaseManager(tmp_path / "d.db") as db:
            episode = await DistillationEngine(db, extractor=extractor).process_exchange("s", QUERY, RESPONSE)
            async with db.conn.execute("SELECT name FROM entities ORDER BY name") as cur:
                names = [r[0] for r in await cur.fetchall()]
            async with db.conn.execute("SELECT COUNT(*) FROM edges") as cur:
                edges = (await cur.fetchone())[0]
            return episode, names, edges

    return run(go())


def test_engine_integration_persists_extraction(tmp_path):
    episode, names, edges = _distill(tmp_path, extractor_for(lambda r: reply(json.dumps(GOOD))))
    assert episode is not None and names == ["Alice", "Atlas"] and edges == 1


def test_engine_survives_bad_extractor_output(tmp_path):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    episode, names, edges = _distill(tmp_path, extractor_for(boom))
    assert episode is not None and names == [] and edges == 0
