"""Issue #244 隔离准备脚本的专项测试：只读快照脱敏、账本口径与工作空间确认闸门。

不联网、不调用任何模型。
"""

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "nexent_assets" / "probes" / "p_issue244" / "setup_isolated.py"

WS_KEY = "sk-ws-" + "a" * 80
JWT = "eyJhbGciOiJIUzI1NiJ9." + "b" * 40
NB_KEY = "nexent-" + "c" * 24


def _load():
    spec = importlib.util.spec_from_file_location("issue244_setup_isolated", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module():
    return _load()


def test_status_summary_drops_credentials(module):
    users = [{"id": "u1", "username": "issue244-eval@example.com", "role": "USER",
              "password": "Sup3r-Secret-Passw0rd!", "access_token": JWT}]
    models = [{"model_id": 9, "model_name": "qwen4-max", "base_url": "https://x/compatible-mode/v1",
               "api_key": WS_KEY, "extra_params": {"api_key": WS_KEY}, "access_token": JWT}]
    agents = [{"agent_id": 3, "name": "i244_plantkg_main", "model_ids": [9],
               "token": NB_KEY, "access_key": NB_KEY}]

    summary = module.build_status_summary(users, models, agents, True, "E:/tmp/evidence",
                                          key_fp={"sha256_16": "0123456789abcdef"})
    text = json.dumps(summary, ensure_ascii=False)

    assert WS_KEY not in text
    assert JWT not in text
    assert NB_KEY not in text
    assert "Sup3r-Secret" not in text
    assert module.scan_secret_fields(summary) == []
    # 白名单之外的结构字段不会原样带进来
    assert "extra_params" not in text
    assert summary["models"][0]["model_name"] == "qwen4-max"


def test_redact_masks_secret_looking_values(module):
    redacted = module.redact({"note": WS_KEY, "nested": [{"jwt": JWT}]})
    text = json.dumps(redacted)
    assert WS_KEY not in text and JWT not in text
    assert "<redacted len=" in text


def test_scan_detects_leaked_fields(module):
    found = module.scan_secret_fields({"models": [{"api_key": "x"}]})
    assert found == ["models[0].api_key"]


def test_status_writes_sanitized_snapshot(module, monkeypatch, tmp_path):
    state_file = tmp_path / "state" / "isolated_env.json"
    monkeypatch.setattr(module, "STATE_FILE", state_file)
    monkeypatch.setattr(module, "admin_token", lambda: "token-not-used")
    monkeypatch.setattr(module, "current_user", lambda token: {
        "user_id": "u0", "user_email": "i244-admin@example.com", "tenant_id": "tenant-1",
        "group_ids": [3], "role": "ADMIN"})
    monkeypatch.setattr(module, "tenant_users", lambda token: [
        {"id": "u1", "username": "issue244-eval@example.com", "role": "USER", "password": "pw-should-vanish"}])
    monkeypatch.setattr(module, "list_agents", lambda token: [
        {"agent_id": 13, "name": "i244_plantkg_main", "model_ids": [9], "token": NB_KEY}])
    monkeypatch.setattr(module, "list_models", lambda token: [
        {"model_id": 9, "model_name": "qwen4-max", "api_key": WS_KEY,
         "base_url": "https://ws-73zmipsobetrem4t.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"}])

    module.cmd_status(None)

    text = (state_file.parent / "status.json").read_text(encoding="utf-8")
    assert WS_KEY not in text and NB_KEY not in text and "pw-should-vanish" not in text
    assert "api_key" not in text
    summary = json.loads(text)
    assert summary["models"][0]["model_name"] == "qwen4-max"
    assert summary["users"][0]["username"] == "issue244-eval@example.com"


def test_save_state_refuses_credentials(module, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "state" / "isolated_env.json")
    with pytest.raises(SystemExit):
        module.save_state({"new_key": {"api_key": WS_KEY}})
    assert not (tmp_path / "state" / "isolated_env.json").exists()


def test_cloud_blocks_without_workspace_confirmation(module, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "state" / "isolated_env.json")
    monkeypatch.setattr(module, "check_key_env", lambda: WS_KEY)
    monkeypatch.setattr(module, "admin_token", lambda: "token-not-used")
    monkeypatch.setattr(module, "_catalog_probe", lambda key, host: (200, {"output": {"total": 1}}))
    created = []

    def fake_http(method, path, body=None, token=None, base=None, timeout=30):
        created.append((method, path))
        return 200, {}

    monkeypatch.setattr(module, "http", fake_http)

    args = Namespace(dry_run=True, with_agents=False, display_name="i244 Qwen4-Max（Issue #244 隔离）",
                     confirm_workspace=None)
    with pytest.raises(SystemExit) as excinfo:
        module.cmd_cloud(args)

    assert excinfo.value.code == 3
    assert ("POST", "/model/create") not in created


def test_cloud_uses_confirmed_workspace_domain(module, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "STATE_FILE", tmp_path / "state" / "isolated_env.json")
    monkeypatch.setattr(module, "check_key_env", lambda: WS_KEY)
    monkeypatch.setattr(module, "admin_token", lambda: "token-not-used")
    monkeypatch.setattr(module, "_catalog_probe", lambda key, host: (200, {"output": {"total": 1}}))
    monkeypatch.setattr(module, "http", lambda *a, **k: (200, {}))
    monkeypatch.setattr(module, "list_models", lambda token: [])

    args = Namespace(dry_run=True, with_agents=False, display_name="i244 Qwen4-Max（Issue #244 隔离）",
                     confirm_workspace="ws-other-12345678")
    with pytest.raises(SystemExit) as excinfo:
        module.cmd_cloud(args)

    assert excinfo.value.code == 0
    state = json.loads((tmp_path / "state" / "isolated_env.json").read_text(encoding="utf-8"))
    assert state["new_key"]["workspace_confirmed"] is True
    assert state["new_key"]["confirmed_workspace"] == "ws-other-12345678"


def test_clone_plan_reuses_existing_agent_and_binds_model(module, monkeypatch):
    """重复执行必须复用同名 Agent（否则后端重名自动改名会造副本），且模型覆盖只留目标模型。"""
    twin = {"tools": [{"tool_id": 1}, {"tool_id": 2}], "skills": [{"skill_id": 7}],
            "group_ids": [2], "max_steps": 10, "description": "twin",
            "sub_agent_id_list": [9, 10], "related_agents": []}
    monkeypatch.setattr(module, "http", lambda method, path, body=None, token=None, base=None,
                        timeout=30: (200, {"agent_id": 13, "latest_version_no": 3}))
    monkeypatch.setattr(module, "agent_detail", lambda token, agent_id, version: twin)
    monkeypatch.setattr(module, "load_asset", lambda name: {
        "prompt": "asset-prompt", "display_name": "i244 main",
        "tools": ["validate"], "skills": [{"name": "triage"}]})
    monkeypatch.setattr(module, "find_agent_id", lambda token, name: (14, None))

    plan = module._clone_plan("token-not-used", "b33_cloud_plantkg_main", 8,
                             tools_by_name={"plantkg_validate": 3},
                             skills_by_name={"triage": 7})

    payload = plan["payload"]
    assert payload["agent_id"] == 14
    assert payload["name"] == "i244_plantkg_main"
    assert payload["model_ids"] == [8] and payload["business_logic_model_id"] == 8
    assert payload["model_params_override"] == {"8": {**module.SAMPLING_OVERRIDE, "extra_params": {}}}
    # 关联字段取自发布快照的 sub_agent_id_list（related_agents 是空旧字段）
    assert payload["related_agent_ids"] == [9, 10]
    # twin 已启用的工具为基线，资产声明但 twin 漏掉的工具按名字补入
    assert payload["enabled_tool_ids"] == [1, 2, 3]
    assert payload["enabled_skill_ids"] == [7]
    assert plan["published_version"] is None

    # 本地小模型口径：--disable-reasoning 落进 extra_params，--requested-output-tokens 落到 Agent 级；
    # verify 的 tuning_ok 就是比这两处，不比就会读到旧快照。
    tuned = module._clone_plan("token-not-used", "b33_cloud_plantkg_main", 8,
                               tools_by_name={"plantkg_validate": 3},
                               skills_by_name={"triage": 7},
                               requested_output_tokens=8192, disable_reasoning=True)
    assert tuned["payload"]["model_params_override"]["8"]["extra_params"] == module.REASONING_OFF
    assert tuned["payload"]["requested_output_tokens"] == 8192


# --- 单次真实调用探针：安全否决（不联网） ---------------------------------

ONCE_PATH = ROOT / "nexent_assets" / "probes" / "p_issue244" / "probe_qwen4max_once.py"
WS_KEY_116 = "sk-ws-" + "a" * 110


def _load_once():
    spec = importlib.util.spec_from_file_location("issue244_probe_once", ONCE_PATH)
    once = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(once)
    return once


def _forbid_socket(monkeypatch, once):
    def boom(*args, **kwargs):
        raise AssertionError("socket opened but the probe must not send here")

    monkeypatch.setattr(once.http.client, "HTTPSConnection", boom)


def test_once_probe_refuses_when_attempt_already_recorded(monkeypatch, tmp_path):
    """账本里已有 pre_send 就必须拒绝：无论上次成功、失败还是结果未知。"""
    once = _load_once()
    monkeypatch.setattr(once, "key_from_user_env", lambda: WS_KEY_116)
    monkeypatch.setattr(once, "sha16", lambda key: once.KEY_SHA16)
    _forbid_socket(monkeypatch, once)

    ledger = tmp_path / "state" / "live-call-attempts.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"event": "pre_send"}) + "\n", encoding="utf-8")

    assert once.main(["--evidence-root", str(tmp_path)]) == 4


def test_once_probe_dry_run_sends_nothing(monkeypatch, tmp_path):
    once = _load_once()
    monkeypatch.setattr(once, "key_from_user_env", lambda: WS_KEY_116)
    monkeypatch.setattr(once, "sha16", lambda key: once.KEY_SHA16)
    _forbid_socket(monkeypatch, once)

    assert once.main(["--evidence-root", str(tmp_path), "--dry-run"]) == 0
    assert not (tmp_path / "state" / "live-call-attempts.jsonl").exists()


def test_once_probe_refuses_key_fingerprint_mismatch(monkeypatch, tmp_path):
    once = _load_once()
    monkeypatch.setattr(once, "key_from_user_env", lambda: "sk-ws-" + "b" * 110)
    _forbid_socket(monkeypatch, once)

    assert once.main(["--evidence-root", str(tmp_path)]) == 3
    assert not (tmp_path / "state").exists()


def test_once_probe_classifies_quota_exhaustion():
    once = _load_once()
    assert once.classify(403, {"code": "AllocationQuota.FreeTierOnly"}) == "quota_exhausted"
    assert once.classify(404, {"code": "model_not_found"}) == "route_or_model_not_found"
    assert once.classify(200, None) == "ok"


# --- 隔离 Agent 资产：B23/B33/D54 传 --agents-dir 指向这里，启动阶段就会 load_assets ---

ASSETS_DIR = ROOT / "nexent_assets" / "probes" / "p_issue244" / "agents"
EXPERTS = {
    "i244_plantkg_expert_energy",
    "i244_plantkg_expert_elevator",
    "i244_plantkg_expert_hvac",
    "i244_plantkg_expert_equipment",
}


def test_isolated_agents_load_and_link_experts():
    """用仓库真实的 load_assets / participants / check_assets 校验生成物，防止再交出启动即失败的配置。"""
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.integrations.nexent.assets import check_assets, load_assets, participants

    assets = load_assets(ASSETS_DIR)
    assert set(assets) == EXPERTS | {"i244_plantkg_main"}

    main = assets["i244_plantkg_main"]
    # 子 Agent 引用必须带 i244_ 前缀，否则 participants 里 KeyError
    assert main.sub_agents and all(sub.startswith("i244_") for sub in main.sub_agents)
    linked = {asset.name for asset, depth in participants(assets, "i244_plantkg_main") if depth == 1}
    assert linked == EXPERTS

    # knowledge 必须留成字典：list(dict) 会把索引名变成键名，AgentAsset 校验直接失败
    for name in EXPERTS:
        assert assets[name].knowledge is not None
        assert assets[name].knowledge.index_names

    tools = build_services(load_settings(profile="mock")).registry.tools  # 含各领域包登记的工具
    check_assets(assets, tools, "i244_plantkg_main")  # 不抛异常即通过启动自检

