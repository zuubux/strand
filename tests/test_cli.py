import asyncio
import json
import sqlite3

import pytest
import sqlite_vec

from strand.cli import main
from strand.db import EMBEDDING_DIM, DatabaseManager


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "cli.db"

    async def seed():
        async with DatabaseManager(path) as db:
            alice = await db.upsert_entity(
                "Alice", "person", "Lead dev", {"aliases": ["Al"]}, embedding=[0.1] * EMBEDDING_DIM
            )
            proj = await db.upsert_entity("Atlas", "project", "Main project")
            guess = await db.upsert_entity(
                "Maybe", "concept", "Dubious", origin="model_inferred", confidence=0.3
            )
            await db.upsert_edge(alice, proj, "works_on", weight=2.0)
            await db.upsert_edge(proj, guess, "mentions", origin="model_inferred", confidence=0.3)
            await db.record_episode("s1", "talked about Atlas", [alice, proj])

    asyncio.run(seed())
    return path


def _run(path, capsys, *argv):
    code = main(["--db", str(path), *argv])
    return code, capsys.readouterr()


def _count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_stats(seeded, capsys):
    code, out = _run(seeded, capsys, "stats")
    assert code == 0
    lines = out.out.splitlines()
    assert lines == [
        "entities: 3",
        "  user_explicit: 2",
        "  model_inferred: 1",
        "edges: 2",
        "episodes: 1",
    ]


def test_list_and_archetype_filter(seeded, capsys):
    code, out = _run(seeded, capsys, "list")
    assert code == 0
    assert out.out.splitlines()[0].split() == ["ID", "NAME", "ARCHETYPE", "CONF", "ORIGIN", "EDGES"]
    assert "Alice" in out.out and "Atlas" in out.out and "Maybe" in out.out
    atlas_row = next(line for line in out.out.splitlines() if "Atlas" in line)
    assert atlas_row.split() == ["2", "Atlas", "project", "1.00", "user_explicit", "2"]

    code, out = _run(seeded, capsys, "list", "--archetype", "person")
    assert "Alice" in out.out and "Atlas" not in out.out

    code, out = _run(seeded, capsys, "list", "--limit", "1")
    assert len(out.out.splitlines()) == 2

    code, out = _run(seeded, capsys, "list", "--archetype", "vehicle")
    assert out.out.strip() == "no entities found"


def test_inspect_shows_one_hop_edges(seeded, capsys):
    code, out = _run(seeded, capsys, "inspect", "Atlas")
    assert code == 0
    text = out.out
    assert "Main project" in text
    assert "outgoing edges (1)" in text and "incoming edges (1)" in text
    assert "-[mentions]-> Maybe  weight=1.00 origin=model_inferred confidence=0.30" in text
    assert "<-[works_on]- Alice  weight=2.00 origin=user_explicit confidence=1.00" in text

    code, out = _run(seeded, capsys, "inspect", "1")
    assert code == 0
    assert "aliases:    Al" in out.out


def test_inspect_missing(seeded, capsys):
    code, out = _run(seeded, capsys, "inspect", "nobody")
    assert code == 1
    assert "not found" in out.err


def test_drop_cascades_edges(seeded, capsys):
    code, out = _run(seeded, capsys, "drop", "Atlas", "--yes")
    assert code == 0
    assert "2 edge(s)" in out.out
    assert _count(seeded, "entities") == 2
    assert _count(seeded, "edges") == 0
    assert _count(seeded, "episodes") == 1


def test_drop_removes_embedding(seeded, capsys):
    conn = sqlite3.connect(seeded)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    assert conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0] == 1
    conn.close()

    _run(seeded, capsys, "drop", "1", "--yes")

    conn = sqlite3.connect(seeded)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM entity_embeddings").fetchone()[0] == 0
    finally:
        conn.close()


def test_drop_requires_confirmation(seeded, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code, out = _run(seeded, capsys, "drop", "Atlas")
    assert code == 1
    assert "aborted" in out.out
    assert _count(seeded, "entities") == 3

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    code, _ = _run(seeded, capsys, "drop", "Atlas")
    assert code == 0
    assert _count(seeded, "entities") == 2


def test_prune_stale(seeded, capsys):
    code, out = _run(seeded, capsys, "prune-stale")
    assert code == 0
    assert "pruned 1 entit" in out.out and "1 edge(s)" in out.out
    assert _count(seeded, "entities") == 2
    assert _count(seeded, "edges") == 1


def test_prune_keeps_user_explicit_and_connected(seeded, capsys):
    code, out = _run(seeded, capsys, "prune-stale", "--confidence-below", "0.1")
    assert "pruned 0 entit" in out.out
    assert _count(seeded, "entities") == 3
    assert _count(seeded, "edges") == 2


def test_db_path_from_env(seeded, capsys, monkeypatch):
    monkeypatch.setenv("STRAND_DB_PATH", str(seeded))
    assert main(["stats"]) == 0
    assert "entities: 3" in capsys.readouterr().out


# -- strand chat ---------------------------------------------------------------------
import httpx  # noqa: E402

from strand import cli as cli_mod  # noqa: E402
from strand.cli import build_parser, run_chat  # noqa: E402
from strand.config import Config  # noqa: E402


def _sse(*tokens):
    lines = [f'data: {{"choices":[{{"delta":{{"content":{t!r}}}}}]}}'.replace("'", '"') for t in tokens]
    return ("\n\n".join(lines) + "\n\ndata: [DONE]\n\n").encode()


def _client(captured, payload=None, status=200):
    def handler(request):
        captured.append(request)
        return httpx.Response(status, content=payload if payload is not None else _sse("Hi", " there"))

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://strand")


def _reader(*lines):
    it = iter(lines)

    def read(_prompt):
        try:
            item = next(it)
        except StopIteration:
            raise EOFError
        if isinstance(item, BaseException):
            raise item
        return item

    return read


def _chat_args(*argv):
    return build_parser().parse_args(["chat", *argv])


def test_chat_parser_defaults_and_flags():
    args = _chat_args()
    assert args.command == "chat" and args.local is True
    assert args.model is None and args.private is False
    args = _chat_args("--model", "m1", "--private")
    assert args.model == "m1" and args.private is True


def test_chat_streams_tokens_and_sends_session_headers(capsys):
    seen = []
    code = run_chat(_chat_args("--model", "m1"), _client(seen), Config(), read=_reader("hello", "quit"))
    out = capsys.readouterr().out
    assert code == 0 and "Hi there" in out
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["model"] == "m1" and body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert seen[0].headers["x-session-id"].startswith("cli-")
    assert "x-strand-allow-private" not in seen[0].headers


def test_chat_private_toggle_and_new_session(capsys):
    seen = []
    read = _reader("/private on", "a", "/private off", "b", "/new", "c", "/private maybe", "exit")
    run_chat(_chat_args(), _client(seen), Config(), read=read)
    out = capsys.readouterr().out
    assert seen[0].headers["x-strand-allow-private"] == "true"
    assert "x-strand-allow-private" not in seen[1].headers
    assert seen[1].headers["x-session-id"] != seen[2].headers["x-session-id"]
    assert len(json.loads(seen[1].content)["messages"]) == 3  # history kept until /new
    assert len(json.loads(seen[2].content)["messages"]) == 1
    assert "Usage: /private on|off" in out


def test_chat_help_unknown_and_no_upstream_for_commands(capsys):
    seen = []
    run_chat(_chat_args(), _client(seen), Config(), read=_reader("/help", "/bogus", "", "quit"))
    out = capsys.readouterr().out
    assert "/private on|off" in out and "Unknown command /bogus" in out
    assert seen == []


def test_chat_stats_command(seeded, capsys):
    args = build_parser().parse_args(["--db", str(seeded), "chat"])
    run_chat(args, _client([]), Config(), read=_reader("/stats", "quit"))
    assert "entities: 3" in capsys.readouterr().out


def test_chat_ctrl_c_aborts_stream_then_second_exits(capsys, monkeypatch):
    def boom(client, body, headers):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_chat_stream", boom)
    code = run_chat(_chat_args(), _client([]), Config(), read=_reader("hi", KeyboardInterrupt()))
    assert code == 0
    assert "[aborted]" in capsys.readouterr().out


def test_chat_ctrl_c_at_prompt_needs_two(capsys):
    seen = []
    read = _reader(KeyboardInterrupt(), "hi", KeyboardInterrupt(), KeyboardInterrupt())
    assert run_chat(_chat_args(), _client(seen), Config(), read=read) == 0
    assert len(seen) == 1  # counter reset after a normal line


def test_chat_http_error_rolls_back_history(capsys):
    seen = []
    read = _reader("one", "two", "quit")
    run_chat(_chat_args(), _client(seen, payload=b"nope", status=500), Config(), read=read)
    captured = capsys.readouterr()
    assert "HTTP 500" in captured.err
    assert len(json.loads(seen[1].content)["messages"]) == 1


def test_chat_connect_error_is_reported(capsys):
    def handler(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://strand")
    assert run_chat(_chat_args(), client, Config(), read=_reader("hi", "quit")) == 0
    assert "cannot reach Strand daemon" in capsys.readouterr().err


def test_doctor_masks_url_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "fe_cache"))
    monkeypatch.setenv("STRAND_UPSTREAM_URL", "https://user:secretpass@proxy.internal/v1")
    monkeypatch.setenv("STRAND_EXTRACTOR_URL", "http://admin:token123@127.0.0.1:11434")
    pings = []

    async def fake_ping(url):
        pings.append(url)
        return False

    async def fake_probe(config):
        return True, "strand.sock"

    monkeypatch.setattr(cli_mod, "_ping", fake_ping)
    monkeypatch.setattr(cli_mod, "_probe_daemon", fake_probe)
    assert main(["--db", str(tmp_path / "d.db"), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "upstream: https://user:********@proxy.internal/v1" in out
    assert "extractor SLM: http://admin:********@127.0.0.1:11434" in out
    assert "secretpass" not in out and "token123" not in out
    assert pings == ["http://admin:token123@127.0.0.1:11434/models"]


@pytest.mark.parametrize("raw", ["{not-json", "{broken: true", "[1, 2]", '"text"'])
def test_inspect_corrupt_metadata_is_graceful(seeded, capsys, raw):
    conn = sqlite3.connect(seeded)
    try:
        conn.execute("UPDATE entities SET metadata = ? WHERE name = 'Atlas'", (raw,))
        conn.commit()
    finally:
        conn.close()

    code, out = _run(seeded, capsys, "inspect", "Atlas")
    assert code == 0
    assert "Traceback" not in out.err
    text = out.out
    assert "[2] Atlas" in text
    assert "archetype:  project" in text
    assert "origin:     user_explicit" in text
    assert "confidence: 1.00" in text
    assert "aliases:    -" in text
    assert "metadata:   {corrupted}" in text
    assert "-[mentions]-> Maybe" in text
    assert "<-[works_on]- Alice" in text


def test_inspect_valid_metadata_still_formats(seeded, capsys):
    code, out = _run(seeded, capsys, "inspect", "Alice")
    assert code == 0
    assert 'metadata:   {"aliases": ["Al"]}' in out.out
    assert "{corrupted}" not in out.out


def _visible_names(path, query):
    from strand.resolver import ContextResolver

    async def go():
        async with DatabaseManager(path) as db:
            resolver = ContextResolver(db)
            default = [e.name for e in await resolver.match_entities(query)]
            allowed = [e.name for e in await resolver.match_entities(query, allow_private=True)]
            return default, allowed

    return asyncio.run(go())


@pytest.mark.parametrize("target", ["2", "Atlas", "atlas"])
def test_mark_private_by_id_or_name(seeded, capsys, target):
    assert _visible_names(seeded, "tell me about Atlas")[0] == ["Atlas"]
    code, out = _run(seeded, capsys, "mark-private", target)
    assert code == 0
    assert out.out.strip() == "Marked entity 'Atlas' (id=2) as private."
    default, allowed = _visible_names(seeded, "tell me about Atlas")
    assert default == [] and allowed == ["Atlas"]


def test_mark_private_preserves_metadata_and_resolves_alias(seeded, capsys):
    code, _ = _run(seeded, capsys, "mark-private", "Al")
    assert code == 0
    conn = sqlite3.connect(seeded)
    try:
        meta = json.loads(conn.execute("SELECT metadata FROM entities WHERE id = 1").fetchone()[0])
    finally:
        conn.close()
    assert meta == {"aliases": ["Al"], "private": True}


def test_mark_private_unknown_entity_fails(seeded, capsys):
    code, out = _run(seeded, capsys, "mark-private", "Nonexistent")
    assert code != 0
    assert "not found" in out.err and out.out == ""


def test_daemon_subcommand_runs_outside_an_event_loop(monkeypatch):
    import asyncio

    from strand import daemon

    def fake_main(argv):
        async def noop():
            return None

        asyncio.run(noop())  # raises RuntimeError if an outer loop is already running
        seen.append(list(argv))

    seen = []
    monkeypatch.setattr(daemon, "main", fake_main)
    assert main(["daemon", "--diag"]) == 0
    assert seen == [["--diag"]]


def _doctor_env(tmp_path, monkeypatch, sock):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("STRAND_HOST", "STRAND_PORT", "STRAND_EXTRACTOR_MODEL", "STRAND_EXTRACTOR_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STRAND_SOCKET_PATH", str(sock))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "fe_cache"))

    async def fake_ping(url):
        return False

    monkeypatch.setattr(cli_mod, "_ping", fake_ping)


def test_doctor_daemon_unreachable_fails(tmp_path, monkeypatch, capsys):
    _doctor_env(tmp_path, monkeypatch, tmp_path / "none.sock")
    assert main(["--db", str(tmp_path / "d.db"), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "[fail] daemon: unreachable (run 'systemctl --user status strand'" in out
    assert "journalctl --user -u strand -n 20" in out


def test_doctor_daemon_active_over_uds(tmp_path, monkeypatch, capsys):
    import uvicorn
    from fastapi import FastAPI

    sock = tmp_path / "strand.sock"
    _doctor_env(tmp_path, monkeypatch, sock)
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    async def scenario():
        server = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="critical"))
        task = asyncio.create_task(server.serve())
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        try:
            return await asyncio.to_thread(main, ["--db", str(tmp_path / "d.db"), "doctor"])
        finally:
            server.should_exit = True
            await task

    assert asyncio.run(scenario()) == 0
    assert "[ok]   daemon: active (listening on strand.sock)" in capsys.readouterr().out


def test_setup_gemini_saves_0600_and_restarts(tmp_path, monkeypatch, capsys):
    import getpass
    import stat
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "AIza-secret-key-0123456789")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert main(["setup"]) == 0
    path = tmp_path / ".config" / "strand" / "config.json"
    data = json.loads(path.read_text())
    assert data["upstream_api_key"] == "AIza-secret-key-0123456789"
    assert data["model_default"] == "gemini-3.6-flash"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert ["systemctl", "--user", "restart", "strand"] in calls
    assert "AIza-secret-key-0123456789" not in capsys.readouterr().out


def test_setup_custom_and_invalid_choice(tmp_path, monkeypatch):
    import getpass
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3)  # service inactive
    )
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
    answers = iter(["3", "http://localhost:9000/v1", "my-model"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert main(["setup"]) == 0
    data = json.loads((tmp_path / ".config" / "strand" / "config.json").read_text())
    assert data["upstream_url"] == "http://localhost:9000/v1" and data["model_default"] == "my-model"
    monkeypatch.setattr("builtins.input", lambda prompt="": "9")
    assert main(["setup"]) == 1
