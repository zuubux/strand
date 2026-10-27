import asyncio

from cortext.db import DatabaseManager
from cortext.resolver import ContextResolver
from cortext.session import SessionLedger
from tests.test_proxy import body_of, ok_json, proxy_client


def run(coro):
    return asyncio.run(coro)


async def seed(db):
    public = await db.upsert_entity("Marcus", "person", "Staff engineer")
    by_archetype = await db.upsert_entity("Vault", "private", "Secret vault code")
    by_meta = await db.upsert_entity("Diary", "concept", "Personal diary", metadata={"private": True})
    await db.upsert_edge(public, by_archetype, "guards")
    await db.upsert_edge(public, by_meta, "writes")
    return public


def test_private_entities_and_edges_excluded_by_default(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            resolver = ContextResolver(db, SessionLedger())
            return (
                await resolver.resolve("Marcus Vault Diary", "s1"),
                await resolver.resolve("Vault", "s2"),
                await resolver.resolve("Diary", "s3"),
            )

    mixed, vault_only, diary_only = run(scenario())
    assert "Marcus (person)" in mixed
    for leaked in ("Vault", "Diary", "guards", "writes", "Secret"):
        assert leaked not in mixed
    assert vault_only == ""
    assert diary_only == ""


def test_allow_private_returns_private_entities(tmp_path):
    async def scenario():
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
            resolver = ContextResolver(db, SessionLedger())
            return await resolver.resolve("Marcus Vault Diary", "s1", allow_private=True)

    delta = run(scenario())
    assert "Vault (private): Secret vault code" in delta
    assert "Diary (concept): Personal diary" in delta
    assert "Marcus -guards-> Vault" in delta


def private_body():
    return {"model": "m", "messages": [{"role": "user", "content": "Tell me about Marcus and the Vault"}]}


async def proxy_post(tmp_path, headers):
    async with proxy_client(tmp_path / "m.db", ok_json) as (client, seen):
        # proxy_client seeds Marcus; the app's DB is the same file, so add private data via a second handle.
        async with DatabaseManager(tmp_path / "m.db") as db:
            await seed(db)
        response = await client.post("/v1/chat/completions", json=private_body(), headers=headers)
        assert response.status_code == 200
        (request,) = seen
        return str(body_of(request)["messages"])


def test_proxy_withholds_private_context_by_default(tmp_path):
    payload = run(proxy_post(tmp_path, {}))
    assert "Marcus (person)" in payload
    assert "Secret vault code" not in payload
    assert "guards" not in payload


def test_proxy_header_enables_private_context_case_insensitively(tmp_path):
    payload = run(proxy_post(tmp_path, {"x-cortext-allow-private": "True"}))
    assert "Secret vault code" in payload


def test_proxy_other_header_values_do_not_enable_private(tmp_path):
    payload = run(proxy_post(tmp_path, {"X-Cortext-Allow-Private": "false"}))
    assert "Secret vault code" not in payload
