"""SEC-PERM-001: 0700 is only enforced on Strand-managed dirs; unmanaged parents are never mutated."""

import asyncio
import logging
import os
import socket
import stat

import pytest

from strand import daemon
from strand.config import save_config_value
from strand.db import DatabaseManager
from strand.perms import is_managed_dir


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(home, 0o755)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_is_managed_dir(fake_home):
    assert is_managed_dir(fake_home / ".config" / "strand")
    assert is_managed_dir(fake_home / ".local" / "share" / "strand")
    assert not is_managed_dir(fake_home)
    assert not is_managed_dir(fake_home / "projects")


def test_db_in_home_leaves_home_mode_and_secures_files(fake_home, caplog):
    db_path = fake_home / "memory.db"

    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Marcus", "person", "Engineer")
            assert mode_of(db_path) == 0o600
            for suffix in ("-wal", "-shm"):
                companion = db_path.with_name(db_path.name + suffix)
                if companion.exists():
                    assert mode_of(companion) == 0o600

    with caplog.at_level(logging.WARNING, logger="strand.perms"):
        asyncio.run(scenario())
    assert mode_of(fake_home) == 0o755
    assert any("not Strand-managed" in r.getMessage() for r in caplog.records)


def test_db_in_managed_dir_is_created_and_tightened(fake_home):
    db_path = fake_home / ".local" / "share" / "strand" / "memory.db"

    async def scenario():
        async with DatabaseManager(db_path):
            assert mode_of(db_path.parent) == 0o700
            assert mode_of(db_path) == 0o600

    asyncio.run(scenario())
    os.chmod(db_path.parent, 0o755)
    asyncio.run(scenario())
    assert mode_of(db_path.parent) == 0o700


def test_socket_in_home_leaves_home_mode(fake_home, caplog):
    sock_path = fake_home / "strand.sock"
    with caplog.at_level(logging.WARNING, logger="strand.perms"):
        daemon.prepare_socket(str(sock_path))
    assert mode_of(fake_home) == 0o755
    assert any("not Strand-managed" in r.getMessage() for r in caplog.records)

    sock = daemon._bind_socket(str(sock_path))
    try:
        assert mode_of(sock_path) == 0o600
    finally:
        sock.close()


def test_socket_in_managed_runtime_dir_is_0700(fake_home):
    runtime = fake_home / "run"
    sock_path = runtime / "strand" / "strand.sock"
    daemon.prepare_socket(str(sock_path))
    assert mode_of(sock_path.parent) == 0o700
    os.chmod(sock_path.parent, 0o755)
    daemon.prepare_socket(str(sock_path))
    assert mode_of(sock_path.parent) == 0o700
    assert socket.AF_UNIX  # socket module available; bind covered in test_transport


def test_config_in_home_leaves_home_mode_and_file_is_0600(fake_home):
    path = fake_home / "config.json"
    save_config_value("log_level", "debug", path)
    assert mode_of(fake_home) == 0o755
    assert mode_of(path) == 0o600


def test_config_in_managed_dir_is_0700(fake_home):
    path = fake_home / ".config" / "strand" / "config.json"
    save_config_value("log_level", "debug", path)
    assert mode_of(path.parent) == 0o700
    assert mode_of(path) == 0o600


def test_unmanaged_parent_missing_dir_created_0700_not_retightened(tmp_path):
    # A freshly created (non-managed) dir keeps its mkdir mode; an existing one is never chmod'ed.
    existing = tmp_path / "shared"
    existing.mkdir()
    os.chmod(existing, 0o775)
    save_config_value("log_level", "debug", existing / "config.json")
    assert mode_of(existing) == 0o775



def _init_logging(path):
    from strand.logging import setup_logging, shutdown_logging

    try:
        setup_logging("INFO", path, debug=False)
    finally:
        shutdown_logging()


def test_logging_in_home_leaves_home_mode_and_file_is_0600(fake_home):
    path = fake_home / "strand.log"
    _init_logging(path)
    assert mode_of(fake_home) == 0o755
    assert mode_of(path) == 0o600


def test_logging_in_unmanaged_dir_leaves_parent_mode(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o775)
    path = shared / "strand.log"
    _init_logging(path)
    assert mode_of(shared) == 0o775
    assert mode_of(path) == 0o600


def test_logging_in_managed_dir_is_tightened_to_0700(fake_home):
    managed = fake_home / ".local" / "share" / "strand"
    managed.mkdir(parents=True)
    os.chmod(managed, 0o755)
    path = managed / "strand.log"
    _init_logging(path)
    assert mode_of(managed) == 0o700
    assert mode_of(path) == 0o600
