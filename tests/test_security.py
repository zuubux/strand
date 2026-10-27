import asyncio
import os
import stat
from contextlib import asynccontextmanager

import httpx

from cortext.config import Config, load_config
from cortext.db import DatabaseManager
from cortext.logging import setup_logging, shutdown_logging
from cortext.proxy import create_app
from cortext.resolver import ContextResolver
from cortext.session import SessionLedger


def run(coro):
    return asyncio.run(coro)


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# --- POSIX storage permissions -------------------------------------------------


def test_db_creates_private_directory_and_files(tmp_path):
    db_path = tmp_path / "new" / "state" / "memory.db"

    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Marcus", "person", "Engineer")
            assert mode_of(db_path.parent) == 0o700
            assert mode_of(db_path) == 0o600
            for suffix in ("-wal", "-shm"):
                companion = db_path.with_name(db_path.name + suffix)
                if companion.exists():
                    assert mode_of(companion) == 0o600

    run(scenario())


def test_db_tightens_existing_directory_and_file(tmp_path):
    parent = tmp_path / "loose"
    parent.mkdir()
    os.chmod(parent, 0o755)
    db_path = parent / "memory.db"
    db_path.touch()
    os.chmod(db_path, 0o644)

    async def scenario():
        async with DatabaseManager(db_path):
            pass

    run(scenario())
    assert mode_of(parent) == 0o700
    assert mode_of(db_path) == 0o600


def test_log_directory_and_file_are_private(tmp_path):
    log_path = tmp_path / "logs" / "cortext.log"
    try:
        setup_logging("INFO", log_path, debug=False)
        assert mode_of(log_path.parent) == 0o700
        assert mode_of(log_path) == 0o600
    finally:
        shutdown_logging()


# --- Config --------------------------------------------------------------------


def test_api_key_config_precedence():
    assert Config().api_key == ""
    assert Config.from_env({}, {"api_key": "file"}).api_key == "file"
    assert Config.from_env({"CORTEXT_API_KEY": "env"}, {"api_key": "file"}).api_key == "env"


def test_load_config_reads_cortext_api_key_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEXT_API_KEY", "from-env")
    assert load_config(config_path=tmp_path / "missing.json").api_key == "from-env"


# --- Inbound bearer authentication ----------------------------------------------

BODY = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
AUTH_ERROR = {"error": {"message": "Invalid or missing API key", "type": "authentication_error"}}


@asynccontextmanager
async def auth_client(db_path, api_key, upstream_api_key="sk-up"):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with DatabaseManager(db_path) as db:
        config = Config(
            upstream_url="http://upstream.test/v1", upstream_api_key=upstream_api_key, api_key=api_key
        )
        app = create_app(config, ContextResolver(db, SessionLedger()), upstream)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://cortext") as client:
            try:
                yield client, seen
            finally:
                await upstream.aclose()


def post(tmp_path, api_key, headers=None, upstream_api_key="sk-up"):
    async def scenario():
        async with auth_client(tmp_path / "m.db", api_key, upstream_api_key) as (client, seen):
            response = await client.post("/v1/chat/completions", json=BODY, headers=headers or {})
            return response, seen

    return run(scenario())


def test_missing_bearer_returns_401(tmp_path):
    response, seen = post(tmp_path, "secret")
    assert response.status_code == 401
    assert response.json() == AUTH_ERROR
    assert seen == []


def test_wrong_bearer_returns_401(tmp_path):
    response, seen = post(tmp_path, "secret", {"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json() == AUTH_ERROR
    assert seen == []


def test_valid_bearer_proxies_upstream_with_upstream_key(tmp_path):
    response, seen = post(tmp_path, "secret", {"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer sk-up"


def test_inbound_key_is_not_leaked_upstream(tmp_path):
    response, seen = post(tmp_path, "secret", {"Authorization": "Bearer secret"}, upstream_api_key="")
    assert response.status_code == 200
    assert "authorization" not in seen[0].headers


def test_health_is_unauthenticated_when_key_set(tmp_path):
    async def scenario():
        async with auth_client(tmp_path / "m.db", "secret") as (client, _):
            return await client.get("/health")

    response = run(scenario())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_empty_api_key_allows_unauthenticated_requests(tmp_path):
    response, seen = post(tmp_path, "")
    assert response.status_code == 200
    assert len(seen) == 1
