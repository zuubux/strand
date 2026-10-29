import asyncio
import os
import stat

import pytest

from strand import daemon
from strand.config import Config, load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("STRAND_HOST", "STRAND_PORT", "STRAND_SOCKET_PATH", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_default_is_tcp_without_xdg(home):
    config = load_config()
    assert config.socket_path == ""
    assert not config.is_socket_mode()
    assert config.get_bind_target() == ("tcp", "127.0.0.1")


def test_explicit_socket_path_selects_uds(home, monkeypatch):
    monkeypatch.setenv("STRAND_SOCKET_PATH", "/tmp/x/c.sock")
    monkeypatch.setenv("STRAND_PORT", "9000")
    config = load_config()
    assert config.is_socket_mode()
    assert config.get_bind_target() == ("uds", "/tmp/x/c.sock")


def test_socket_path_from_config_file(home):
    path = home / ".config" / "strand" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"socket_path": "/tmp/f.sock"}')
    assert load_config().effective_socket_path() == "/tmp/f.sock"


def test_xdg_runtime_dir_defaults_to_uds(home, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    config = load_config()
    assert config.is_socket_mode()
    assert config.get_bind_target() == ("uds", "/run/user/1000/strand/strand.sock")


@pytest.mark.parametrize("name,value", [("STRAND_HOST", "0.0.0.0"), ("STRAND_PORT", "8088")])
def test_host_or_port_override_forces_tcp(home, monkeypatch, name, value):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv(name, value)
    config = load_config()
    assert not config.is_socket_mode()
    assert config.get_bind_target()[0] == "tcp"


def test_file_port_forces_tcp(home, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    path = home / ".config" / "strand" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"port": 9001}')
    assert not load_config().is_socket_mode()


def test_plain_config_has_no_socket():
    assert Config().socket_path == ""


def test_prepare_socket_creates_private_dir_and_unlinks_stale(tmp_path):
    sock = tmp_path / "run" / "strand" / "strand.sock"
    sock.parent.mkdir(parents=True, mode=0o755)
    import socket

    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock))
    dead.close()  # leaves a stale socket file with no listener
    assert sock.exists()
    daemon.prepare_socket(str(sock))
    assert not sock.exists()
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700


def test_prepare_socket_creates_missing_parent(tmp_path):
    sock = tmp_path / "a" / "b" / "c.sock"
    daemon.prepare_socket(str(sock))
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700


def test_serve_uds_sets_0600_and_cleans_up(tmp_path, monkeypatch):
    import uvicorn
    from fastapi import FastAPI

    sock = tmp_path / "d" / "c.sock"
    daemon.prepare_socket(str(sock))

    async def run():
        app = FastAPI()
        server_holder = {}
        real = uvicorn.Server

        class Spy(real):
            def __init__(self, cfg):
                super().__init__(cfg)
                server_holder["s"] = self

        monkeypatch.setattr(daemon.uvicorn, "Server", Spy)
        task = asyncio.create_task(daemon._serve_uds(app, str(sock)))
        for _ in range(500):
            s = server_holder.get("s")
            if s is not None and s.started:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        server_holder["s"].should_exit = True
        await task
        return mode

    assert asyncio.run(run()) == 0o600
    assert not sock.exists()


def test_daemon_quiet_by_default_and_diag_flag(monkeypatch, tmp_path):
    import logging

    from strand.logging import shutdown_logging

    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("XDG_RUNTIME_DIR", "STRAND_SOCKET_PATH", "STRAND_PORT", "STRAND_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRAND_HOST", "127.0.0.1")
    monkeypatch.setenv("STRAND_LOG_PATH", str(tmp_path / "c.log"))
    monkeypatch.setattr(daemon, "build_app", lambda config: object())
    seen = []
    monkeypatch.setattr(
        daemon.uvicorn,
        "run",
        lambda app, **kw: seen.append(dict(daemon.uvicorn.config.LOGGING_CONFIG["handlers"]["default"])),
    )
    original = dict(daemon.uvicorn.config.LOGGING_CONFIG["handlers"]["default"])
    logger = logging.getLogger("strand")
    try:
        daemon.main([])
        assert seen[0] == {"()": "strand.logging.uvicorn_log_handler"}
        assert daemon.uvicorn.config.LOGGING_CONFIG["handlers"]["default"] == original
        assert not any(type(h) is logging.StreamHandler for h in logger.handlers)
        daemon.main(["--diag"])
        assert seen[1] == original
        assert logger.level == logging.DEBUG
        assert any(type(h) is logging.StreamHandler for h in logger.handlers)
    finally:
        shutdown_logging()
    assert "Starting Strand" in (tmp_path / "c.log").read_text()
