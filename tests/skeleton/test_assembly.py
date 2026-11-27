"""PR#2 审查：健康状态如实、真实实现替换 Fake、prod 缺端口明确失败。"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from plantkg.api import _health
from plantkg.bootstrap import PACKS_DIR, build_services
from plantkg.core import contracts as c
from plantkg.core.config import discover_packs, load_settings
from plantkg.core.ports import AgentResultSource, RunSessionStore
from plantkg.core.registry import Registry, RegistryError
from plantkg.testing import fakes, install_fakes
from tests.skeleton.conftest import ROOT


def test_mock_health_is_honest(services):
    health = _health(services)
    deps = health.dependencies
    assert health.status == "degraded"  # 占位包与 Fake 都不算正常
    assert deps["nexent"] == "disabled" and deps["judge"] == "fake"
    assert all(deps[f"pack.{p}"] == "placeholder" for p in ("pv", "storage", "tariff", "production"))
    assert deps["pack.elevator"] == "ok"  # B20 的真实包，不再是占位
    assert deps["pack.hvac"] == "ok"  # B21 的暖通包是真实现
    assert deps["fake.RunSessionStore"] == "fake" and deps["fake.get_plan"] == "fake"
    assert deps["pack.global_graph"] == "ok"  # B25 全局图谱是真实实现
    assert all(k.startswith("pack.") for k, v in deps.items() if v == "ok")  # 只有真实实现的领域包可报 ok


def test_enabled_but_unchecked_dependency_is_not_ok(config_dir, edit_config, monkeypatch):
    edit_config("defaults.yaml", lambda d: d["nexent"].update(enabled=True))
    monkeypatch.setenv("NEXENT_API_KEY", "x" * 16)
    health = _health(build_services(load_settings(profile="dev", config_dir=config_dir)))
    assert health.dependencies["nexent"] == "unchecked" and health.status == "degraded"


@pytest.mark.parametrize("real_first", [True, False])
def test_job_submit_and_query_share_one_queue(real_first):
    """PR#2 复审：Fake 的 run_day、get_job 与 GET /jobs 必须和当前登记的 JobRunner 是同一个队列。"""
    from plantkg.core.ports import JobRunner

    registry = Registry()
    real_queue = fakes.InMemoryJobRunner()  # 代表 B04 的真实实现（非 fallback 登记）
    settings = load_settings(profile="dev")
    if real_first:
        registry.register(JobRunner, real_queue)
        install_fakes(registry, settings)
    else:
        install_fakes(registry, settings)
        registry.register(JobRunner, real_queue)
    context = c.Context(plant_id="demo-electronics", industry_pack="manufacturing", actor=c.Actor(actor_id="u"),
                        channel="mock_default", trace_id="tr-1", as_of="2026-06-30T10:00:00Z")
    submitted = registry.tools["run_day"].handler(context, plan_date="2026-07-01")
    assert real_queue.get(submitted.job_id).job_id == submitted.job_id  # 提交进了真实队列
    queried = registry.tools["get_job"].handler(context, job_id=submitted.job_id)
    assert queried.business_status == "done"
    rest_job = registry.rest[("GET", "/jobs/{job_id}")](context, {"job_id": submitted.job_id}, {})
    assert rest_job.job_id == submitted.job_id


def test_fake_job_runner_keeps_plant_isolation():
    """#69 二修/三修：Fake 保留厂区归属；只读请求不绑定厂区，跨厂区 submit/snapshot/health 一律拒绝。"""
    runner = fakes.InMemoryJobRunner()
    a = c.Context(plant_id="plant-a", industry_pack="manufacturing", actor=c.Actor(actor_id="u"),
                  channel="mock_default", trace_id="tr-1", as_of="2026-06-30T10:00:00Z")
    b = a.model_copy(update={"plant_id": "plant-b"})
    assert runner.snapshot(b).total == 0 and runner.health(b).queue_depth == 0  # 首次只读不占厂区
    job = runner.submit(c.JobKind.run_day, {"mock_result_ref": "plan://a@1"}, "same-key", a)
    assert runner.snapshot(a).jobs == (job,)
    for call in (lambda: runner.snapshot(b), lambda: runner.health(b),
                 lambda: runner.submit(c.JobKind.run_day, {"mock_result_ref": "plan://b@1"}, "same-key", b)):
        with pytest.raises(c.PlantkgError) as exc:
            call()
        assert exc.value.code == c.ErrorCode.CROSS_PLANT
    pinned = fakes.InMemoryJobRunner("plant-a")  # 构造时明确厂区，首次使用即校验
    with pytest.raises(c.PlantkgError) as exc:
        pinned.health(b)
    assert exc.value.code == c.ErrorCode.CROSS_PLANT
    assert pinned.health(a).closed is False


def test_fakes_bind_settings_plant_at_assembly():
    """#69 三修：Mock/dev 装配用 settings.plant_id 固定 Fake 厂区，其他厂区第一次只读就被拒。"""
    from plantkg.core.ports import JobRunner

    settings = load_settings(profile="dev")
    registry = Registry()
    install_fakes(registry, settings)
    runner = registry.resolve(JobRunner)
    other = c.Context(plant_id="other-plant", industry_pack="manufacturing", actor=c.Actor(actor_id="u"),
                      channel="mock_default", trace_id="tr-1", as_of="2026-06-30T10:00:00Z")
    for call in (lambda: runner.snapshot(other), lambda: runner.health(other)):
        with pytest.raises(c.PlantkgError) as exc:
            call()
        assert exc.value.code == c.ErrorCode.CROSS_PLANT


def test_active_pack_without_entrypoint_fails_fast(tmp_path):
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / "pack.yaml").write_text("name: half\nschema_version: 1\nstatus: active\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="entrypoint"):
        Registry().load([tmp_path / "half"], ["half"])


def test_contract_readme_version_matches_code():
    readme = (ROOT / "contracts" / "README.md").read_text(encoding="utf-8")
    assert f"契约版本 **{c.CONTRACT_VERSION}**" in readme


@pytest.mark.parametrize("real_first", [True, False])
def test_real_implementation_replaces_fake_in_any_order(real_first):
    registry = Registry()
    real_store = fakes.InMemoryRunSessionStore()

    def real_get_plan(context, plan_id, revision=None):
        return c.ToolResult(ok=True, business_status="done", data={"source": "real"})

    def register_real():
        registry.register(RunSessionStore, real_store)
        registry.register_tool("get_plan", real_get_plan)

    if real_first:
        register_real()
        install_fakes(registry, None)
    else:
        install_fakes(registry, None)
        register_real()
    assert registry.ports[RunSessionStore] is real_store
    assert registry.tools["get_plan"].handler is real_get_plan
    assert "get_plan" not in registry.fake_bindings() and "list_assets" in registry.fake_bindings()
    with pytest.raises(RegistryError, match="重复"):
        registry.register_tool("get_plan", real_get_plan)  # 两个真实实现仍然冲突


def test_pack_entrypoint_replaces_fake_end_to_end(tmp_path, monkeypatch):
    """用一个最小的真实领域包验证 dev 切换：包里的 get_plan 生效，其余槽位仍由 Fake 补齐。"""
    packs = tmp_path / "packs"
    for p in PACKS_DIR.iterdir():
        if p.is_dir() and (p / "pack.yaml").exists():
            (packs / p.name).mkdir(parents=True)
            (packs / p.name / "pack.yaml").write_text((p / "pack.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (packs / "demo").mkdir()
    (packs / "demo" / "pack.yaml").write_text(textwrap.dedent("""
        name: demo
        schema_version: 1
        entrypoint: demo_pack_entry:install
        provides: {tools: [get_plan]}
    """), encoding="utf-8")
    (tmp_path / "demo_pack_entry.py").write_text(textwrap.dedent("""
        from plantkg.core.contracts import ToolResult
        def install(registry, settings):
            registry.register_tool("get_plan", lambda context, plan_id, revision=None:
                ToolResult(ok=True, business_status="done", data={"source": "demo-pack", "plan_id": plan_id}))
    """), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    settings = load_settings(profile="dev", overrides={"packs": "[pv, storage, tariff, production, demo]"})
    services = build_services(settings, packs_dir=packs)
    binding = services.registry.tools["get_plan"]
    text = services.guard.call(binding.spec, binding.handler, "", {"plan_id": "p1"})
    assert '"source":"demo-pack"' in text
    assert _health(services).dependencies["pack.demo"] == "ok"
    assert "list_assets" in services.registry.fake_bindings()


def test_prod_fails_clearly_until_b04():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-m", "plantkg", "--profile", "prod", "--dry-run"], cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 2
    assert "RunSessionStore" in out.stderr and "B04" in out.stderr and "Traceback" not in out.stderr


EVAL_PACK = """
from plantkg.core.contracts import AgentResultKind
from plantkg.core.ports import AgentResultSource

class Source:
    def __init__(self, name):
        self.name = name
    def get(self, kind, binding, payload):
        return {"by": self.name}

def install(registry, settings):
    for kind in AgentResultKind:
        for mode in MODES:
            registry.register_mode(AgentResultSource, mode, Source(f"{mode}-{kind.value}"), key=kind.value)
"""


def _evaluation_services(tmp_path, monkeypatch, modes):
    """真实配置与装配：evaluation profile、启用存储，Agent 结果实现由一个领域包登记（模拟 B27/B28/B29 与 B33）。"""
    packs = tmp_path / "packs"
    for p in PACKS_DIR.iterdir():
        if p.is_dir() and (p / "pack.yaml").exists():
            (packs / p.name).mkdir(parents=True)
            (packs / p.name / "pack.yaml").write_text((p / "pack.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    enabled = "[pv, storage, tariff, production]"
    if modes:
        (packs / "agent_results").mkdir()
        module = "eval_pack_" + "_".join(modes)  # 每组模式一个模块名，避免导入缓存串用
        (packs / "agent_results" / "pack.yaml").write_text(
            f"name: agent_results\nschema_version: 1\nentrypoint: {module}:install\n", encoding="utf-8")
        (tmp_path / f"{module}.py").write_text(f"MODES = {modes!r}\n" + EVAL_PACK, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        enabled = "[pv, storage, tariff, production, agent_results]"
    runtime = tmp_path / "runtime"
    settings = load_settings(profile="evaluation", overrides={
        "packs": enabled, "storage.enabled": True, "storage.sqlite": str(runtime / "catalog.db"),
        "storage.graph": str(runtime / "graph"), "storage.series": str(runtime / "series")})
    return build_services(settings, packs_dir=packs)


@pytest.mark.parametrize("modes", [("deterministic",), ("deterministic", "evaluation", "trial")], ids=["deterministic", "cache"])
def test_evaluation_profile_starts_with_deterministic_or_cache(tmp_path, monkeypatch, modes):
    services = _evaluation_services(tmp_path, monkeypatch, modes)
    try:
        assert _health(services).profile == "evaluation" and services.registry.fake_bindings() == []
        source = services.registry.resolve_mode(AgentResultSource, "evaluation", "triage")
        assert source.name == ("evaluation-triage" if "evaluation" in modes else "deterministic-triage")
    finally:
        services.close()


def test_evaluation_profile_without_agent_results_fails_clearly(tmp_path, monkeypatch):
    with pytest.raises(RegistryError, match="evaluation 模式缺少实现"):
        _evaluation_services(tmp_path, monkeypatch, ())


def test_evaluation_profile_is_accepted_by_cli():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-m", "plantkg", "--profile", "evaluation", "--dry-run"], cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert "invalid choice" not in out.stderr and "Traceback" not in out.stderr
    assert out.returncode == 2 and "装配失败" in out.stderr  # 默认配置未启用存储、也没人登记 Agent 结果实现：自检明确失败


def test_packs_auto_discovers_by_order_without_shared_config(tmp_path):
    for name, order in (("first", 10), ("second", "20"), ("added", None)):  # 带引号的数字与整数混用
        (tmp_path / name).mkdir()
        manifest = {"name": name, "schema_version": 1} | ({"order": order} if order else {})
        (tmp_path / name / "pack.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    assert discover_packs(tmp_path) == ("first", "second", "added")
    assert load_settings().packs == discover_packs()
