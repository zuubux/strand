import json
import logging

import pytest

from strand.cli import main, mask_secret
from strand.config import Config, load_config, save_config_value
from strand.logging import get_logger, setup_logging, shutdown_logging


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "STRAND_UPSTREAM_URL",
        "STRAND_UPSTREAM_API_KEY",
        "STRAND_HOST",
        "STRAND_PORT",
        "STRAND_DB_PATH",
        "STRAND_LOG_LEVEL",
        "STRAND_LOG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_defaults_when_no_file(home):
    assert load_config() == Config()
    assert Config().log_level == "INFO"


def test_file_overrides_defaults_and_env_overrides_file(home, monkeypatch):
    path = home / ".config" / "strand" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"upstream_url": "http://file/v1", "port": 9001, "log_level": "debug"}))
    config = load_config()
    assert (config.upstream_url, config.port, config.log_level) == ("http://file/v1", 9001, "DEBUG")
    assert config.host == "127.0.0.1"

    monkeypatch.setenv("STRAND_UPSTREAM_URL", "http://env/v1")
    monkeypatch.setenv("STRAND_PORT", "7000")
    config = load_config()
    assert (config.upstream_url, config.port, config.log_level) == ("http://env/v1", 7000, "DEBUG")


def test_corrupt_file_is_ignored(home):
    path = home / ".config" / "strand" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert load_config() == Config()


def test_save_config_value_creates_dirs_and_merges(home):
    save_config_value("upstream_api_key", "sk-abc")
    save_config_value("port", "9100")
    path = home / ".config" / "strand" / "config.json"
    assert json.loads(path.read_text()) == {"port": 9100, "upstream_api_key": "sk-abc"}
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(KeyError):
        save_config_value("bogus", 1)


def test_mask_secret():
    assert mask_secret("sk-1234567890abcd") == "sk-...abcd"
    assert mask_secret("short") == "****"
    assert mask_secret("") == ""


def test_cli_config_set_get_show(home, capsys):
    assert main(["config", "set", "upstream_api_key", "sk-1234567890abcd"]) == 0
    assert "sk-...abcd" in capsys.readouterr().out
    assert main(["config", "set", "port", "9200"]) == 0
    capsys.readouterr()

    assert main(["config", "get", "port"]) == 0
    assert capsys.readouterr().out.strip() == "9200"
    assert main(["config", "get", "upstream_api_key"]) == 0
    assert capsys.readouterr().out.strip() == "sk-1234567890abcd"

    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "upstream_api_key: sk-...abcd" in out
    assert "1234567890" not in out
    assert "port: 9200" in out

    assert main(["config", "get", "nope"]) == 1
    assert main(["config", "set", "nope", "x"]) == 1


def test_logs_missing_file(home, capsys):
    assert main(["logs"]) == 1
    assert "no log file" in capsys.readouterr().err


def test_rotating_logger_writes_and_cli_tails(home, capsys, monkeypatch):
    log_path = home / "logs" / "strand.log"
    monkeypatch.setenv("STRAND_LOG_PATH", str(log_path))
    try:
        logger = setup_logging("INFO", debug=False)
        get_logger("proxy").info("hello %d", 1)
        for i in range(5):
            get_logger("db").warning("line %d", i)
        get_logger("db").debug("hidden")
        for handler in logger.handlers:
            handler.flush()
    finally:
        shutdown_logging()

    text = log_path.read_text()
    assert "[INFO] [proxy] hello 1" in text
    assert "[WARNING] [db] line 4" in text
    assert "hidden" not in text
    assert text.split(" [")[0].count("-") == 2  # YYYY-MM-DD HH:MM:SS prefix
    assert not any(getattr(h, "_strand_managed", False) for h in logging.getLogger("strand").handlers)

    assert main(["logs", "--tail", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[-1].endswith("[db] line 4")
    assert main(["logs"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 6


def test_dedup_distance_threshold_config(tmp_path):
    from strand.config import ENV_VARS, Config, load_config

    assert Config().dedup_distance_threshold == 0.45
    assert ENV_VARS["dedup_distance_threshold"] == "STRAND_DEDUP_DISTANCE_THRESHOLD"
    assert Config.from_env({}, {"dedup_distance_threshold": 0.3}).dedup_distance_threshold == 0.3
    env = {"STRAND_DEDUP_DISTANCE_THRESHOLD": "0.2"}
    assert load_config(env, tmp_path / "none.json").dedup_distance_threshold == 0.2


def test_model_default_precedence_and_cli_persistence(home, monkeypatch, capsys):
    assert Config().model_default == ""
    assert load_config().model_default == ""
    assert main(["config", "set", "model_default", "  gemini-3.6-flash "]) == 0
    capsys.readouterr()
    assert load_config().model_default == "gemini-3.6-flash"
    assert main(["config", "get", "model_default"]) == 0
    assert capsys.readouterr().out.strip() == "gemini-3.6-flash"
    monkeypatch.setenv("STRAND_MODEL_DEFAULT", "env-model")
    assert load_config().model_default == "env-model"
    assert load_config({}).model_default == "gemini-3.6-flash"


def test_uvicorn_records_use_server_component(tmp_path):
    from strand.logging import uvicorn_log_handler

    log_path = tmp_path / "strand.log"
    setup_logging("INFO", log_path, debug=False)
    handler = uvicorn_log_handler()
    uv = logging.getLogger("uvicorn.error")
    uv.addHandler(handler)
    old_level = uv.level
    uv.setLevel(logging.INFO)
    try:
        uv.info("Started server process")
        uv.error("boom")
        get_logger("proxy").info("hello")
    finally:
        uv.removeHandler(handler)
        uv.setLevel(old_level)
        shutdown_logging()
    text = log_path.read_text()
    assert "[INFO] [server] Started server process" in text
    assert "[ERROR] [server] boom" in text
    assert "[INFO] [proxy] hello" in text
    assert "[error]" not in text


def test_enable_host_telemetry_default_env_and_file(home):
    from strand.config import ENV_VARS

    assert ENV_VARS["enable_host_telemetry"] == "STRAND_ENABLE_HOST_TELEMETRY"
    assert Config().enable_host_telemetry is False
    assert Config.from_env({}, {}).enable_host_telemetry is False
    for raw in ("1", "true", "TRUE", "Yes"):
        assert Config.from_env({"STRAND_ENABLE_HOST_TELEMETRY": raw}, {}).enable_host_telemetry is True
    assert Config.from_env({"STRAND_ENABLE_HOST_TELEMETRY": "0"}, {"enable_host_telemetry": True}).enable_host_telemetry is False
    assert Config.from_env({}, {"enable_host_telemetry": True}).enable_host_telemetry is True
    assert Config.from_env({"STRAND_ENABLE_HOST_TELEMETRY": ""}, {"enable_host_telemetry": True}).enable_host_telemetry is True


def test_cli_config_get_set_enable_host_telemetry(home, capsys):
    assert main(["config", "get", "enable_host_telemetry"]) == 0
    assert capsys.readouterr().out.strip() == "False"
    assert main(["config", "set", "enable_host_telemetry", "yes"]) == 0
    capsys.readouterr()
    path = home / ".config" / "strand" / "config.json"
    assert json.loads(path.read_text()) == {"enable_host_telemetry": True}
    assert load_config().enable_host_telemetry is True
    assert main(["config", "get", "enable_host_telemetry"]) == 0
    assert capsys.readouterr().out.strip() == "True"
