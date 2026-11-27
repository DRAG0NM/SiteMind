"""配置分层、缺配置报错、密钥与日志不泄露。"""

import io
import logging

import pytest

from plantkg.core.config import ConfigError, check_runtime_secrets, configure_logging, load_settings, run_id_var

SECRET = "sk-THISISAREALLOOKINGSECRET0123456789"
CTX = "3f9c2a7e5b1d4c8e9a0b6d2f7e1c4a5b"


def test_layering_and_sources(settings):
    assert settings.plant_id == "demo-electronics"
    assert settings.sources == ("defaults.yaml", "plant.yaml")
    assert settings.planning.solver_budget.time_limit_s == 20


def test_missing_config_dir(tmp_path):
    with pytest.raises(ConfigError, match="defaults.yaml"):
        load_settings(profile="mock", config_dir=tmp_path)


def test_missing_required_key(config_dir, edit_config):
    edit_config("plant.yaml", lambda d: d.pop("plant_id"))
    with pytest.raises(ConfigError, match="plant_id"):
        load_settings(profile="mock", config_dir=config_dir)


def test_missing_scenario_file(config_dir, edit_config):
    with pytest.raises(ConfigError, match="scenarios"):
        load_settings(profile="mock", config_dir=config_dir, scenario="nope")


def test_security_context_not_overridable():
    for key in ("plant_id", "security.default_ctx.enabled", "nexent.api_key_env"):
        with pytest.raises(ConfigError, match="不能从命令行覆盖"):
            load_settings(profile="mock", overrides={key: "x"})
    assert load_settings(profile="mock", overrides={"server.port": "8710"}).server.port == 8710


def test_prod_rejects_default_ctx(config_dir, edit_config):
    edit_config("defaults.yaml", lambda d: d["security"]["default_ctx"].update(enabled=True))
    with pytest.raises(ConfigError, match="default_ctx"):
        load_settings(profile="prod", config_dir=config_dir)
    assert load_settings(profile="dev", config_dir=config_dir).security.default_ctx.enabled


def test_missing_secret_names_variable_not_value(config_dir, edit_config):
    edit_config("defaults.yaml", lambda d: d["nexent"].update(enabled=True))
    settings = load_settings(profile="dev", config_dir=config_dir)
    with pytest.raises(ConfigError) as excinfo:
        check_runtime_secrets(settings, environ={"DASHSCOPE_API_KEY": SECRET})
    assert "NEXENT_API_KEY" in str(excinfo.value) and SECRET not in str(excinfo.value)
    check_runtime_secrets(settings, environ={"NEXENT_API_KEY": SECRET})


def test_mock_needs_no_secrets(settings):
    check_runtime_secrets(settings, environ={})


def test_logs_carry_run_id_and_redact_secrets(settings):
    configure_logging(settings)
    stream = io.StringIO()
    logger = logging.getLogger("plantkg")
    logger.handlers[0].stream = stream
    token = run_id_var.set("run-day-20260701")
    try:
        logging.getLogger("plantkg.test").info("ctx=%s key=%s auth=Bearer abcdefghijklmnop", CTX, SECRET)
        try:
            raise RuntimeError(f"boom {CTX}")
        except RuntimeError:
            logging.getLogger("plantkg.test").exception("failed")
    finally:
        run_id_var.reset(token)
    out = stream.getvalue()
    assert "run=run-day-20260701" in out
    assert CTX not in out and SECRET not in out and "abcdefghijklmnop" not in out
    # 64 位内容散列不被误伤
    logging.getLogger("plantkg.test").info("hash=%s", "a" * 64)
    assert "a" * 64 in stream.getvalue()
