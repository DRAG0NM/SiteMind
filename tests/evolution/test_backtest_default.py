"""B32 默认回测装配：候选规则进值守链路、policy 候选生效、trial 结果源继承与正式库隔离。"""
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, Candidate, ComponentRef, Context, ErrorCode, ManifestComponent, PlantkgError, PolicyConfig, ResponseBudget, Role, Rule
from plantkg.core.ports import AgentResultSource, PendingActionStore, VersionStore
from plantkg.core.registry import Registry
from plantkg.evolution.artifacts import policy_artifacts
from plantkg.evolution.service import EvolutionService, evolution_config
from plantkg.evolution.store import EvolutionStore
from plantkg.knowledge.rules.publisher import RuleArtifacts

ROOT = Path(__file__).resolve().parents[2]

SCENARIO = """
scenario_id: b32-backtest
title: B32 回测剧本
date: "2026-07-01"
seed: 7
initial: {soc: 0.5}
knowledge: []
events:
  - {at: "10:30", id: ev-combo, type: unknown_combo, entity_id: LT-P, severity: 3, summary: 组合异常}
  - {at: "10:30", id: ev-elevator, type: elevator_congestion, entity_id: "elevator_group:admin_low", severity: 3, summary: 拥堵}
"""


@pytest.fixture
def assembled(tmp_path):
    settings = load_settings(profile="mock", overrides={
        "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    services = build_services(settings)
    try:
        yield services
    finally:
        services.close()


def _service(services, tmp_path):
    registry = services.registry
    versions = registry.resolve(VersionStore)
    reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
    versions.register_verifier("scenario-knowledge:", lambda _: "complete")
    artifacts = RuleArtifacts(versions.writer, versions.plant_id)
    if "b30-rules:" not in versions._verifiers:
        versions.register_verifier("b30-rules:", artifacts.verify)
    neutral = Rule(rule_id="b32-neutral", kind="hvac_setpoint",
                   subject_ids=(f"entity://{versions.plant_id}/zone/admin",), condition={"action": "keep_setpoint"},
                   quantity="setpoint", unit="degC", value=24.0, config_ref="config://b32/neutral",
                   effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                   available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nature="synthetic",
                   review_status="approved", reviewer="reviewer")
    rules_ref = artifacts.prepare([neutral], reviewer)
    base = versions.prepare({"rules": rules_ref.model_dump(mode="json")}, "b32 backtest base")
    if base.status == "prepared":
        base = versions.check_integrity(base.version_manifest_id)
    versions.activate(None, base.version_manifest_id, reviewer, "b32 backtest base")
    scenario = tmp_path / "b32_backtest.yaml"
    scenario.write_text(SCENARIO, encoding="utf-8")
    context = Context(plant_id=versions.plant_id, industry_pack="manufacturing", actor=reviewer, channel="offline",
                      trace_id="b32-default-backtest", as_of=datetime(2026, 6, 30, 10, tzinfo=timezone.utc))
    store = EvolutionStore(versions.writer, versions.plant_id)
    service = EvolutionService(store, versions, registry.resolve(PendingActionStore), artifacts, registry, context,
                               config=evolution_config(services.settings))
    return service, store, versions, artifacts, reviewer, {"scenario_id": "b32-backtest", "seed": 7,
                                                          "path": str(scenario)}


def test_default_backtest_runs_candidate_rule_through_rule_matcher(assembled, tmp_path):
    service, store, versions, _artifacts, _reviewer, spec = _service(assembled, tmp_path)

    class CountingLiveSource:
        def __init__(self):
            self.calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("回测不得调用 live 模型入口")

    live = CountingLiveSource()
    for kind in ("expert", "investigate", "arbitration"):
        if (AgentResultSource, "live", kind) not in assembled.registry.mode_ports:
            assembled.registry.register_mode(AgentResultSource, "live", live, key=kind)
    candidate = service.record_correction("ticket-combo", {
        "note": "照明与空压同时异常是总配电柜接触不良", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P",
        "autonomy_category": "unknown_combo|lighting", "autonomy_level": "L2",
        "condition": {"event_type": "unknown_combo", "actions": [
            {"entity_id": f"entity://{versions.plant_id}/lighting/LT-P", "command": "repair"}]}})
    report = service.backtest(candidate.candidate_id, [spec])
    assert report.verdict == "pass"
    assert report.metrics["candidate"]["target"] > report.metrics["base"]["target"]
    assert report.metrics[spec["scenario_id"]]["candidate_target"] > 0
    assert report.metrics["coverage"]["deterministic_fallbacks"] > 0
    assert store.candidate(candidate.candidate_id).status == "validated"
    assert live.calls == 0


def test_default_backtest_applies_policy_candidate(assembled, tmp_path):
    service, _store, versions, _artifacts, reviewer, spec = _service(assembled, tmp_path)
    policy_store = policy_artifacts(versions.writer, versions.plant_id)
    if "policy:" not in versions._verifiers:
        versions.register_verifier("policy:", policy_store.verify)
    policy = PolicyConfig(policy_version="b32-policy", arbitration_weights={"cny": 1.0, "wait_min": 0.5, "comfort_min": 0.8},
                          priority_profiles={"default": ("safety", "production", "comfort", "cost")},
                          response_budget=ResponseBudget(layer12_min=1, layer3_min=3),
                          autonomy_levels={"elevator_congestion": "L1"})
    ref = policy_store.prepare(policy.model_dump(mode="json"), reviewer)
    candidate = service.record_correction("ticket-policy", {"changes": {"policy": ref.model_dump(mode="json")}})
    report = service.backtest(candidate.candidate_id, [spec])
    assert report.verdict == "pass"
    assert report.metrics["candidate"]["target"] > report.metrics["base"]["target"]


def test_default_registry_inherits_trial_sources_but_not_live():
    from plantkg.world.scenario.runner import default_registry

    class RecordingSource:
        def get(self, *_args, **_kwargs):
            raise AssertionError("结果源只应被继承，不应在装配时调用")

    source = Registry()
    source.register_mode(AgentResultSource, "trial", RecordingSource(), key="expert")
    source.register_mode(AgentResultSource, "evaluation", RecordingSource(), key="investigate")
    source.register_mode(AgentResultSource, "live", RecordingSource(), key="arbitration")
    runtime = default_registry(source)
    assert (AgentResultSource, "trial", "expert") in runtime.mode_ports
    assert (AgentResultSource, "evaluation", "investigate") in runtime.mode_ports
    assert (AgentResultSource, "live", "arbitration") not in runtime.mode_ports


def test_default_scenarios_cover_demo_and_extreme_scripts(assembled):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    expected = {path.stem for path in (root / "configs" / "scenarios").glob("*.yaml")}
    scenarios = assembled.registry.hooks_for("evolution.default_scenarios")[-1](None, None)
    assert "factory_demo_day" in expected <= {item["scenario_id"] for item in scenarios}
    assert all(item["path"].endswith(".yaml") for item in scenarios)


def test_evolution_tools_work_through_tool_guard_and_backtest_job(assembled, tmp_path):
    from plantkg.core import contracts as c
    from plantkg.core.ports import RunSessionStore

    _svc, _store, versions, _artifacts, _reviewer, spec = _service(assembled, tmp_path)
    registry = assembled.registry
    registry.register_hook("evolution.default_scenarios", lambda _candidate, _ctx: (spec,))
    session = registry.resolve(RunSessionStore).register(
        Actor(actor_id="job-reviewer", roles=(Role.viewer, Role.operator, Role.reviewer)),
        versions.plant_id, datetime(2026, 6, 30, 10, tzinfo=timezone.utc), (), 3600, "ar-b32-job", "tr-b32-job")

    def call(tool, ctx, **params):
        binding = registry.tools[tool]
        return c.ToolResult.model_validate_json(assembled.guard.call(binding.spec, binding.handler, ctx, params))

    created = call("propose_candidates_evo", session.ctx, kind="knowledge", trigger={
        "event_ticket": "ticket-job", "note": "组合异常规则", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/air_compressor/AC-2"})
    assert created.ok and created.data["candidate_id"]
    unbound = registry.tools["propose_candidates_evo"]
    denied = assembled.guard.call(unbound.spec, unbound.handler, "", {"kind": "knowledge", "trigger": {}})
    assert denied.startswith("PLANTKG_E_NO_CTX:")
    rollback_tool = registry.tools["propose_rollback"]
    denied = assembled.guard.call(rollback_tool.spec, rollback_tool.handler, session.ctx,
                                  {"target_manifest_id": "missing", "rationale": "测试"})
    assert denied.startswith("PLANTKG_E_NOT_FOUND:")

    binding = registry.tools["backtest"]
    assert binding.spec.mode == "job"
    denied = assembled.guard.call(binding.spec, binding.handler, "", {"candidate_id": created.data["candidate_id"]})
    assert denied.startswith("PLANTKG_E_NO_CTX:")
    pending = call("backtest", session.ctx, candidate_id=created.data["candidate_id"])
    assert pending.ok and pending.business_status == "pending" and pending.job_id
    for _ in range(6):
        done = call("get_job", session.ctx, job_id=pending.job_id, wait=40)
        if done.business_status != "pending":
            break
    assert done.ok and done.data["result_ref"].startswith("b32-report:")


def test_published_rule_matcher_registered_by_b32(assembled, tmp_path):
    from plantkg.core.contracts import DayEvent
    from plantkg.core.ports import RuleMatcher

    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    registry = assembled.registry
    matcher = registry.ports.get(RuleMatcher)
    assert matcher is not None

    def event(event_id, event_type):
        return DayEvent(event_id=event_id, at=datetime(2026, 7, 1, 2, 30, tzinfo=timezone.utc), event_type=event_type,
                        source="script", summary="测试事件", nature="synthetic",
                        entity_id=f"entity://{versions.plant_id}/air_compressor/AC-2")

    assert matcher.covers(event("ev-combo", "unknown_combo")) is False
    assert matcher.covers(event("ev-spike", "load_spike")) is True  # B27 静态白名单兜底
    registry.register_hook("evolution.backtest_evaluator", lambda *_: {
        "scenario_id": "matcher-publish", "executed": True, "ledger_verified": True,
        "metrics": {"target": {"base": 0.0, "candidate": 1.0}}})
    candidate = service.record_correction("ticket-matcher", {
        "note": "组合异常规则", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/air_compressor/AC-2",
        "condition": {"event_type": "unknown_combo", "actions": [
            {"entity_id": f"entity://{versions.plant_id}/air_compressor/AC-2", "command": "repair"}]}})
    report = service.backtest(candidate.candidate_id, [{"scenario_id": "matcher-publish", "seed": 1}])
    assert report.verdict == "pass"
    action = service.propose_publish(store.candidate(candidate.candidate_id))
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "确认发布", 0, "b32-matcher-publish")
    assert matcher.covers(event("ev-combo", "unknown_combo")) is True


def test_fast_publish_meets_demo_budget(assembled, tmp_path):
    """P1-1：发布只跑候选一侧；墙钟计量只在串行跑（xdist 并发负载会误判）。"""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.skip("计时用例只在串行执行，xdist 下的耗时受并发争用影响")
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-fast", {
        "note": "照明与空压同时异常是总配电柜接触不良", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    started = time.perf_counter()
    action = service.propose_publish(store.candidate(candidate.candidate_id))
    elapsed = time.perf_counter() - started
    published = store.candidate(candidate.candidate_id)
    report = store.report(published.backtest_report_id)
    assert report.verdict == "pass"
    assert report.scope == "fast_window"
    assert report.metrics["coverage"]["baseline_reused"] == 1.0  # 发布只跑候选侧
    assert elapsed < 10, f"单次发布耗时 {elapsed:.1f}s，超过串行 10 秒预算"
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "确认发布", 0, "b32-fast-publish")
    assert versions.knowledge_version(versions.active().version_manifest_id) >= 2


def test_combo_backtest_does_not_measure_a_different_subject(assembled, tmp_path):
    service, _store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction('ticket-other-device', {
        'note': '复核其他设备', 'event_type': 'unknown_combo',
        'entity_id': f'entity://{versions.plant_id}/air_compressor/AC-2'})
    report = service.fast_backtest(candidate.candidate_id)
    assert report.verdict == 'inconclusive'
    assert report.metrics['coverage']['target_events'] == 0
    assert any('未验证组合规则' in failure for failure in report.failures)


def test_text_correction_is_measured_as_human_advice(assembled, tmp_path):
    service, _store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction('ticket-lighting-advice', {
        'note': '照明异常需核查总配电柜接触情况', 'event_type': 'unknown_combo',
        'entity_id': f'entity://{versions.plant_id}/lighting/LT-P'})
    report = service.fast_backtest(candidate.candidate_id)
    assert report.verdict == 'pass', report.failures
    assert report.metrics['candidate']['target'] > report.metrics['base']['target']
    assert report.metrics['candidate']['fast'] == report.metrics['base']['fast']


def test_combo_event_before_correction_is_not_measurable(assembled, tmp_path):
    service, _store, versions, _artifacts, _reviewer, spec = _service(assembled, tmp_path)
    service.context = service.context.model_copy(update={'as_of': datetime(2026, 7, 2, tzinfo=timezone.utc)})
    candidate = service.record_correction('ticket-after-event', {
        'note': '检查照明配电柜', 'event_type': 'unknown_combo',
        'entity_id': f'entity://{versions.plant_id}/lighting/LT-P'})
    report = service.backtest(candidate.candidate_id, [spec])
    assert report.verdict == 'inconclusive'
    assert report.metrics['coverage']['target_events'] == 0


def test_publish_rejects_candidate_changed_after_review_proposal(assembled, tmp_path):
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction('ticket-stale-approval', {
        'note': '照明异常需核查总配电柜', 'event_type': 'unknown_combo',
        'entity_id': f'entity://{versions.plant_id}/lighting/LT-P'})
    action = service.propose_publish(candidate)
    saved = store.candidate(candidate.candidate_id)
    base_rule = versions.get(candidate.base_manifest_id).components[ManifestComponent.rules]
    def force_candidate(candidate):  # update_candidate 不再允许改 changes：直接改库模拟持久化内容被篡改
        versions.writer.submit(lambda con: con.execute(
            "UPDATE b32_candidates SET body=? WHERE plant_id=? AND candidate_id=?",
            (candidate.model_dump_json(), versions.plant_id, candidate.candidate_id)))
    force_candidate(saved.model_copy(update={'changes': {ManifestComponent.rules: base_rule}}))
    before = versions.active().version_manifest_id
    handler = assembled.registry.action_handlers['publish']
    with pytest.raises(PlantkgError) as error:
        handler.apply(action.model_copy(update={'status': 'confirmed'}), reviewer)
    assert error.value.code == ErrorCode.STATE_CONFLICT
    legacy_payload = dict(action.payload)
    legacy_payload.pop('backtest_report')
    with pytest.raises(PlantkgError) as legacy_error:
        handler.apply(action.model_copy(update={'status': 'confirmed', 'payload': legacy_payload}), reviewer)
    assert legacy_error.value.code == ErrorCode.STATE_CONFLICT
    force_candidate(saved.model_copy(update={'status': 'rejected'}))
    with pytest.raises(PlantkgError) as rejected:
        handler.apply(action.model_copy(update={'status': 'confirmed'}), reviewer)
    assert rejected.value.code == ErrorCode.STATE_CONFLICT
    assert versions.active().version_manifest_id == before


def test_feedback_accumulates_autonomy_level_candidates(assembled, tmp_path):
    """P1-2 ⑬：连续无误到阈值升一级；驳回降一级；原因不是系统判断有误时只记录。"""
    service, store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    # 钩子路径：B47 等调用方经 evolution.record_correction 传 feedback，应拿到 ToolResult
    hook = assembled.registry.hooks_for("evolution.record_correction")[-1]
    wired = hook(Context(plant_id=versions.plant_id, industry_pack="manufacturing", channel="offline",
                         actor=_reviewer, trace_id="b32-feedback-hook", as_of=datetime.now(timezone.utc)),
                 "ticket-hook", {"feedback": {"event_type": "load_spike", "outcome": "approved"}})
    assert wired.ok and wired.data["event_type"] == "load_spike"
    result = None
    for _ in range(3):
        result = service.record_feedback("ticket-ok", {"event_type": "elevator_congestion", "outcome": "approved"})
    assert result and result["candidate_id"]
    candidate = store.candidate(result["candidate_id"])
    policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        candidate.changes[ManifestComponent.policy]))
    assert policy.autonomy_levels["elevator_congestion"] == "L3"
    action = service.propose_publish(store.candidate(candidate.candidate_id))
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, _reviewer, "确认发布", 0, "b32-autonomy-up")
    assert service.published_autonomy("elevator_congestion") == "L3"

    neutral = service.record_feedback("ticket-other", {
        "event_type": "elevator_congestion", "outcome": "corrected", "system_error": False})
    assert neutral["candidate_id"] is None  # #193：原因不是系统判断有误，不计入降级

    first_bad = service.record_feedback("ticket-bad-1", {
        "event_type": "elevator_congestion", "outcome": "rejected", "system_error": True})
    assert first_bad["candidate_id"] is None  # 单次驳回不降级：窗口内至少 autonomy_downgrade_min 次系统判断有误
    down = service.record_feedback("ticket-bad", {
        "event_type": "elevator_congestion", "outcome": "rejected", "system_error": True})
    assert down["candidate_id"]
    downgraded = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        store.candidate(down["candidate_id"]).changes[ManifestComponent.policy]))
    assert downgraded.autonomy_levels["elevator_congestion"] == "L2"


def test_sensor_case_candidate_reaches_sensor_layer(assembled, tmp_path):
    """P1-2 ⑭：传感器 vs 设备案例生成故障特征矩阵候选，发布后进入观测层配置。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-sensor", {
        "note": "压力表卡滞", "sensor_case": {"relation_id": "air_current_pressure", "judgement": "sensor",
                                             "label": "人工确认压力表卡滞", "sensors": ["AIR-HDR.P"]}})
    action = service.propose_publish(store.candidate(candidate.candidate_id))
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "确认发布", 0, "b32-sensor-case")
    cases = service.published_sensor_cases()
    assert cases and cases[0]["relation_id"] == "air_current_pressure"

    # P2-3：第二个案例累积发布，不覆盖第一份，也不占基准 parameters 槽
    second = service.record_correction("ticket-sensor-2", {
        "note": "温度传感器漂移", "sensor_case": {"relation_id": "zone_temp_drift", "judgement": "sensor",
                                                  "label": "人工确认温度传感器漂移", "sensors": ["ZONE-ADMIN.T"]}})
    action2 = service.propose_publish(store.candidate(second.candidate_id))
    assembled.registry.resolve(PendingActionStore).confirm(action2.action_id, reviewer, "确认发布", 0,
                                                           "b32-sensor-case-2")
    cases = service.published_sensor_cases()
    assert {item["relation_id"] for item in cases} == {"air_current_pressure", "zone_temp_drift"}
    active = versions.active()
    assert active.components.get("sensor_cases") is not None
    assert active.components.get("parameters") == versions.get(second.base_manifest_id).components.get("parameters")

    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.runner import default_registry
    from plantkg.world.scenario.script import load_scenario
    from plantkg.world.scenario.sims import SIM_HOOK
    from plantkg.world.sensors import SensorLayer

    world = build_world(load_scenario(ROOT / "configs" / "scenarios" / "fast" / "b32_demo.yaml"))
    runtime = default_registry(assembled.registry)
    layers = []
    for factory in runtime.hooks_for(SIM_HOOK):
        sim = factory(world.index)
        if isinstance(sim, SensorLayer):
            layers.append(sim)
    assert layers
    assert any(item.id.startswith("case-") for item in layers[-1].config.fault_matrix["air_current_pressure"])


def test_evolution_publish_early_return_still_checks_consumer_mismatch(assembled, tmp_path):
    """合入 main 后：#143/#144 的消费端校验不能被 evolution.propose_publish 早返回绕过。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    registry = assembled.registry
    registry.register_hook("evolution.backtest_evaluator", lambda *_: {
        "scenario_id": "b32-mismatch", "executed": True, "ledger_verified": True,
        "metrics": {"target": {"base": 0.0, "candidate": 1.0}}})
    single = Rule(rule_id="b32-single-elevator", kind="elevator_congestion_rule",
                  subject_ids=(f"entity://{versions.plant_id}/passenger_elevator/PE-1",),
                  condition={"time_range": {"start": "08:00", "end": "18:00"}, "zones": ["admin"]},
                  bounds={"upper": 120.0}, quantity="wait_min", unit="s", value=None,
                  config_ref="config://corrections/b32-single-elevator",
                  effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                  available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nature="synthetic",
                  review_status="approved", reviewer=reviewer.actor_id)
    candidate = service.record_correction("ticket-single-elevator", {
        "note": "单梯拥堵规则", "rule": single.model_dump(mode="json")})
    report = service.backtest(candidate.candidate_id, [{"scenario_id": "b32-mismatch", "seed": 1}])
    assert report.verdict == "pass"
    context = Context(plant_id=versions.plant_id, industry_pack="manufacturing", actor=reviewer,
                      channel="offline", trace_id="b32-mismatch-publish",
                      as_of=datetime(2026, 6, 30, 10, tzinfo=timezone.utc))
    with pytest.raises(PlantkgError) as error:
        registry.tools["propose_publish"].handler(context, candidate_id=candidate.candidate_id,
                                                  rationale="发布单梯规则")
    assert error.value.code == ErrorCode.PRECONDITION and "consumer_mismatch" in str(error.value)
    assert store.candidate(candidate.candidate_id).publish_action_id is None


def test_publish_validates_only_changed_rules(assembled, tmp_path):
    """P2-9：历史规则集里的坏条目（单梯电梯规则）不连带阻断本次新增规则发布。"""
    registry = assembled.registry
    versions = registry.resolve(VersionStore)
    reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
    versions.register_verifier("scenario-knowledge:", lambda _: "complete")
    artifacts = RuleArtifacts(versions.writer, versions.plant_id)
    if "b30-rules:" not in versions._verifiers:
        versions.register_verifier("b30-rules:", artifacts.verify)
    bad = Rule(rule_id="legacy-bad-elevator", kind="elevator_congestion_rule",
               subject_ids=(f"entity://{versions.plant_id}/passenger_elevator/PE-1",),
               condition={"time_range": {"start": "08:00", "end": "18:00"}, "zones": ["admin"]},
               bounds={"upper": 120.0}, quantity="wait_min", unit="s", value=None,
               config_ref="config://legacy/bad", effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
               available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nature="synthetic",
               review_status="approved", reviewer="reviewer")
    neutral = Rule(rule_id="legacy-neutral", kind="hvac_setpoint",
                   subject_ids=(f"entity://{versions.plant_id}/zone/admin",), condition={"action": "keep_setpoint"},
                   quantity="setpoint", unit="degC", value=24.0, config_ref="config://legacy/neutral",
                   effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                   available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nature="synthetic",
                   review_status="approved", reviewer="reviewer")
    base = versions.prepare({"rules": artifacts.prepare([bad, neutral], reviewer).model_dump(mode="json")},
                            "historical bad rule base")
    if base.status == "prepared":
        base = versions.check_integrity(base.version_manifest_id)
    versions.activate(None, base.version_manifest_id, reviewer, "historical bad rule base")
    context = Context(plant_id=versions.plant_id, industry_pack="manufacturing", actor=reviewer, channel="offline",
                      trace_id="b32-changed-only", as_of=datetime(2026, 6, 30, 10, tzinfo=timezone.utc))
    store = EvolutionStore(versions.writer, versions.plant_id)
    service = EvolutionService(store, versions, registry.resolve(PendingActionStore), artifacts, registry, context,
                               evaluator=lambda *_: {"scenario_id": "changed-only", "executed": True,
                                                     "ledger_verified": True,
                                                     "metrics": {"target": {"base": 0.0, "candidate": 1.0}}})
    good = Rule(rule_id="new-good-combo", kind="unknown_combo",
                subject_ids=(f"entity://{versions.plant_id}/air_compressor/AC-2",),
                condition={"event_type": "unknown_combo", "action": "按人工纠错后的处置方式处理"},
                quantity="event_handling", unit="count", value=1.0, config_ref="config://corrections/new-good",
                effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nature="synthetic",
                review_status="approved", reviewer="reviewer")
    candidate = service.record_correction("ticket-changed-only", {"rule": good.model_dump(mode="json")})
    report = service.backtest(candidate.candidate_id, [{"scenario_id": "changed-only", "seed": 1}])
    assert report.verdict == "pass"
    action = service.propose_publish(store.candidate(candidate.candidate_id))
    assert action.status == "proposed"


def test_sampling_error_window_tightens_policy(assembled, tmp_path):
    """P2-2：滑窗抽查错误率高于上限且样本够时生成收紧候选（降一级）；候选被拒后释放占位。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    category = "elevator_congestion|elevator_group"
    for index in range(10):
        store.feedback(category, "rejected", system_error=True, ticket_id=f"seed-{index}", note="抽查判错",
                       source="sampling")
    result = service.record_feedback("ticket-tighten", {"category": category, "outcome": "approved",
                                                        "source": "sampling"})
    assert result["candidate_id"], "错误率 91% 高于上限 20% 且样本够，应生成收紧候选"
    candidate = store.candidate(result["candidate_id"])
    policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        candidate.changes[ManifestComponent.policy]))
    assert policy.autonomy_levels[category] == "L1"  # 默认 L2 降一级
    assert store.autonomy_state(category)[1] == "L1"
    assert store.autonomy_pending_candidate(category) == result["candidate_id"]

    service.evaluator = lambda *_: {"baseline_score": 1.0, "candidate_score": 0.0,
                                    "executed": True, "ledger_verified": True}
    report = service.backtest(result["candidate_id"], [{"scenario_id": "tighten-fail", "seed": 1}])
    assert report.verdict == "fail"
    assert store.autonomy_state(category)[1] is None
    assert store.autonomy_pending_candidate(category) is None


def test_b47_category_policy_candidate_requires_measured_level(assembled, tmp_path):
    """P1：B47 类别级候选走真实发布路径（fast_backtest → propose_publish）必须测到等级生效。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    category = "load_spike|air_compressor"
    result = None
    for index in range(3):
        result = service.record_feedback(f"ticket-category-{index}",
                                         {"category": category, "outcome": "approved"})
    candidate_id = result["candidate_id"]
    assert candidate_id, "连续 3 次批准应生成升一级候选"
    report = service.fast_backtest(candidate_id)
    assert report.verdict == "pass", report.failures
    assert report.metrics["coverage"]["target_events"] >= 1, "类别目标事件应被计入"
    assert report.metrics["candidate"]["target"] > report.metrics["base"]["target"], "等级必须真的在执行决策里生效"
    assert report.metrics["coverage"]["baseline_reused"] == 1.0, "候选生成时应已预热基准"
    action = service.propose_publish(store.candidate(candidate_id))
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "确认发布", 0, "p1-category-publish")
    active = versions.active()
    policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        active.components[ManifestComponent.policy]))
    assert policy.autonomy_levels[category] == "L3"


def test_target_metric_rejects_rule_action_outside_event_domain():
    """P2-1：动作与事件域不符的规则覆盖不计入目标指标（拆开 rule_coverage 与目标证据）。"""
    from plantkg.bootstrap import _summary_metrics

    summary = {"capacity_violation_minutes": 0.0, "cost_cny": 0.0,
               "events": [
                   {"event_id": "bad", "event_type": "load_spike", "category": "load_spike|transformer",
                    "hit": True, "rule_action": "isolate_leak", "resolved": False, "channel": "fast", "level": None},
                   {"event_id": "good", "event_type": "load_spike", "category": "load_spike|transformer",
                    "hit": True, "rule_action": "discharge", "resolved": False, "channel": "fast", "level": None}]}
    metrics = _summary_metrics(summary, {"load_spike"}, {})
    assert metrics["target"] == 1.0


def test_policy_target_with_zero_events_cannot_pass(assembled, tmp_path):
    """P2-2：policy 候选不允许走 target_events==0 的非退化放行。"""
    service, store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    ref = ComponentRef(ref="policy:" + "0" * 64, content_hash="sha256:" + "0" * 64)
    candidate = Candidate(candidate_id="b32-zero-events", kind="parameter",
                          base_manifest_id=versions.active().version_manifest_id,
                          changes={ManifestComponent.policy: ref}, status="proposed", trigger="测试",
                          created_at=datetime(2026, 6, 30, tzinfo=timezone.utc), validation_policy_version="b32-v1")
    report = service._backtest_report(candidate, [{"scenario_id": "zero"}], [{
        "scenario_id": "zero", "executed": True, "ledger_verified": True, "target_events": 0,
        "policy_targets": True, "targets_unmeasurable": True,
        "metrics": {"target": {"base": 0.0, "candidate": 0.0}}}])
    assert report.verdict == "fail"
    assert any("policy" in failure for failure in report.failures)


def test_sampling_error_low_relaxes_thresholds_and_publishes(assembled, tmp_path):
    """P2-1/P2-2/P2-6：初始清单上放宽候选能过真回测并发布；只按抽查结果、至少两个类别满足、不改抽查比例。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    category = "load_spike|air_compressor"
    other = "elevator_congestion|elevator_group"
    for cat in (category, other):
        for index in range(10):
            store.feedback(cat, "approved", system_error=True, ticket_id=f"seed-{cat}-{index}", source="sampling")
    result = service.record_feedback("ticket-relax", {"category": category, "outcome": "approved", "source": "sampling"})
    candidate_id = result["candidate_id"]
    assert candidate_id, "错误率 0 低于下限且两个类别满足，应生成放宽候选"
    candidate = store.candidate(candidate_id)
    policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        candidate.changes[ManifestComponent.policy]))
    assert policy.confidence_low < 0.3
    assert policy.sampling_ratio == 0.2, "sampling_ratio 暂无消费端，候选不应改"
    assert "抽查比例" not in candidate.trigger

    started = time.perf_counter()
    report = service.fast_backtest(candidate_id)
    elapsed = time.perf_counter() - started
    assert report.verdict == "pass", report.failures
    assert report.metrics["coverage"]["baseline_reused"] == 1.0
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        assert elapsed < 10, f"放宽候选发布回测耗时 {elapsed:.1f}s，超过 10 秒"
    action = service.propose_publish(store.candidate(candidate_id))
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "确认发布", 0, "p2-relax-publish")
    active = versions.active()
    published = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        active.components[ManifestComponent.policy]))
    assert published.confidence_low < 0.3


def test_random_alarm_target_is_not_counted_as_observable(assembled, tmp_path):
    """P2-3：随机告警（source=random）不进目标统计，也不该把只出现在随机告警的目标类判成可观测。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    scenario_file = tmp_path / "random_only.yaml"
    scenario_file.write_text(
        "scenario_id: random-only\ntitle: 随机告警目标\ndate: \"2026-07-01\"\nseed: 7\n"
        "initial: {soc: 0.5}\n"
        "bursts:\n  - {at: \"00:05\", count: 2, minutes: 10, noise_ratio: 0.0, "
        "alarms: [{type: load_spike, entity: AC-2}]}\n", encoding="utf-8")
    rule = Rule(rule_id="random-target-rule", kind="load_spike",
                subject_ids=(f"entity://{versions.plant_id}/air_compressor/AC-2",),
                condition={"event_type": "load_spike", "action": "按人工纠错后的处置方式处理"},
                quantity="event_handling", unit="count", value=1.0, config_ref="config://tests/random-only",
                effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nature="synthetic",
                review_status="approved", reviewer=reviewer.actor_id)
    candidate = service.record_correction("ticket-random-target", {"rule": rule.model_dump(mode="json")})
    report = service.backtest(candidate.candidate_id, [{"scenario_id": "random-only", "seed": 7,
                                                        "path": str(scenario_file), "fast": True}])
    assert report.metrics["coverage"]["target_events"] == 0.0
    # 规则候选没有目标事件不算通过（#242 A.3）：随机告警不计目标，目标类事件未出现在回测窗口
    assert report.verdict == "inconclusive" and report.failures == ("目标类事件未出现在回测窗口",)


def test_afternoon_correction_publish_anchors_fast_scenario(assembled, tmp_path):
    """P1-B1：16:00 本地做一句话纠错，快场景要锚定到规则生效之后，发布不能必败。"""
    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
    from plantkg.world.scenario.script import load_scenario

    registry = assembled.registry
    versions = registry.resolve(VersionStore)
    reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    runner = ScenarioRunner(world, ScenarioDB.from_writer(assembled.storage.writer, world.site.plant_id),
                            registry=default_registry(registry), mode="evaluation", group="coordinated")
    runner.start()
    context = Context(plant_id=versions.plant_id, industry_pack="manufacturing", actor=reviewer, channel="offline",
                      trace_id="p1b1-afternoon", as_of=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
                      version_manifest_id=runner.run_info.version_manifest_id, run_id=runner.run_info.run_id)
    created = registry.tools["propose_candidates_evo"].handler(context, kind="knowledge", trigger={
        "event_ticket": "ticket://TK-0013", "note": "照明与空压同时异常是总配电柜接触不良",
        "event_type": "unknown_combo", "entity_id": f"entity://{versions.plant_id}/lighting/LT-P",
        "autonomy_category": "unknown_combo|lighting", "autonomy_level": "L2",
        "condition": {"event_type": "unknown_combo", "actions": [
            {"entity_id": f"entity://{versions.plant_id}/lighting/LT-P", "command": "repair"}]}})
    assert created.ok, created.to_agent_text()
    cid = created.data["candidate_id"]
    publish = registry.tools["propose_publish"].handler(context, candidate_id=cid, rationale="16:00 发布")
    assert publish.ok, publish.to_agent_text()
    store = EvolutionStore(versions.writer, versions.plant_id)
    candidate = store.candidate(cid)
    report = store.report(candidate.backtest_report_id)
    assert report.verdict == "pass", report.failures
    assert report.metrics["coverage"]["target_events"] >= 1
    assert report.metrics["candidate"]["target"] > report.metrics["base"]["target"]


def test_scene_start_sensor_case_confirm_publish_200(tmp_path):
    """P1-B2：场景刚起步、没上传资料时，只有 sensor_cases 的候选也能确认发布（200）。"""
    from starlette.testclient import TestClient
    from plantkg.api import create_app
    from plantkg.core.config import load_settings
    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
    from plantkg.world.scenario.script import load_scenario

    services = build_services(load_settings(profile="mock", overrides={
        "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series"),
        "actions.reviewers": "[u-reviewer]"}))
    try:
        registry = services.registry
        versions = registry.resolve(VersionStore)
        reviewer = Actor(actor_id="u-reviewer", roles=(Role.reviewer,))
        scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
        world = build_world(scenario)
        runner = ScenarioRunner(world, ScenarioDB.from_writer(services.storage.writer, world.site.plant_id),
                                registry=default_registry(registry), mode="evaluation", group="coordinated")
        runner.start()  # 只发起始清单，不跑知识步
        assert "scenario-knowledge:" in versions._verifiers and "scenario-policy:" in versions._verifiers
        context = Context(plant_id=versions.plant_id, industry_pack="manufacturing", actor=reviewer, channel="offline",
                          trace_id="p1b2-sensor-boot", as_of=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
                          version_manifest_id=runner.run_info.version_manifest_id, run_id=runner.run_info.run_id)
        hook = registry.hooks_for("evolution.record_correction")[-1]
        created = hook(context, "ticket-sensor-boot", {"sensor_case": {
            "relation_id": "air_current_pressure", "judgement": "sensor",
            "label": "人工确认压力表卡滞", "sensors": ["AIR-HDR.P"]}})
        cid = created.data["candidate_id"]
        publish = registry.tools["propose_publish"].handler(context, candidate_id=cid, rationale="起步案例发布")
        assert publish.ok, publish.to_agent_text()
        actions = registry.resolve(PendingActionStore)
        action = actions.get(publish.data["action_id"])
        headers = {"x-plantkg-operator-token": services.version_run.operator.token, "x-plantkg-actor": "u-reviewer"}
        client = TestClient(create_app(services))
        try:
            response = client.post(f"/api/v1/actions/{action.action_id}/confirm", headers=headers,
                                   json={"reason": "确认案例", "expected_revision": action.revision,
                                         "idempotency_key": "sensor-boot-confirm"})
        finally:
            client.close()
        assert response.status_code == 200, response.text
        assert versions.active().components.get(ManifestComponent.sensor_cases) is not None
    finally:
        services.close()


def test_scenario_runner_hook_registers_and_resolves_by_run_id(assembled):
    """C.2（#242）：场景运行器经 evolution.scenario_runner 钩子按 run_id 登记/查询，不写注册表私有属性。"""
    from types import SimpleNamespace

    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
    from plantkg.world.scenario.script import load_scenario

    registry = assembled.registry
    versions = registry.resolve(VersionStore)
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    runner = ScenarioRunner(world, ScenarioDB.from_writer(assembled.storage.writer, world.site.plant_id),
                            registry=default_registry(registry), mode="evaluation", group="coordinated")
    runner.start()
    hook = registry.hooks_for("evolution.scenario_runner")[-1]
    parent_id = runner.run_info.run_id
    assert hook(parent_id) is runner
    assert not hasattr(registry, "_b32_scenario_runners"), "运行器索引应放在钩子里，不写注册表私有属性"

    components = dict(versions.active().components)
    reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
    policy_ref = policy_artifacts(versions.writer, versions.plant_id).prepare(
        PolicyConfig(policy_version="c2-fork", arbitration_weights={"cny": 1.0},
                     priority_profiles={"default": ("safety",)},
                     response_budget=ResponseBudget(layer12_min=1, layer3_min=3)).model_dump(mode="json"),
        reviewer)
    components[ManifestComponent.policy] = policy_ref
    manifest = versions.prepare(components, "C.2 发布分叉测试")
    if manifest.status == "prepared":
        manifest = versions.check_integrity(manifest.version_manifest_id)
    versions.activate(versions.active().version_manifest_id, manifest.version_manifest_id, reviewer, "C.2 发布分叉测试")
    registry.hooks_for("evolution.fork_published")[-1](parent_id, manifest.version_manifest_id,
                                                       SimpleNamespace(candidate_id="c2-fork"))
    child_id = runner.run_info.run_id
    assert child_id != parent_id
    assert hook(child_id) is runner and hook(parent_id) is runner, "分叉后的子运行也应按 run_id 查到"


def test_duplicate_correction_returns_existing_candidate(assembled, tmp_path):
    """P2-②：同一句纠错重复提交返回同一个候选。"""
    service, store, _versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    correction = {"note": "同一句纠错", "event_type": "unknown_combo",
                  "entity_id": f"entity://{_versions.plant_id}/air_compressor/AC-2"}
    first = service.record_correction("ticket-dup", correction)
    second = service.record_correction("ticket-dup", correction)
    assert first.candidate_id == second.candidate_id


def test_duplicate_propose_publish_writes_one_timeline_event(assembled, tmp_path):
    """P2-①：重复 propose_publish 只在时间线写一条 publish_proposed。"""
    service, store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-dup-publish", {
        "note": "照明异常需核查总配电柜", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    service.fast_backtest(candidate.candidate_id)
    service.propose_publish(store.candidate(candidate.candidate_id))
    service.propose_publish(store.candidate(candidate.candidate_id))
    events = [row for row in store.timeline()
              if row.get("event") == "publish_proposed" and row.get("candidate_id") == candidate.candidate_id]
    assert len(events) == 1


def test_publish_after_published_reports_state_conflict(assembled, tmp_path):
    """P2-⑤：已发布候选再提议发布报 STATE_CONFLICT“已发布”。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-published", {
        "note": "照明异常需核查总配电柜", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    action = service.propose_publish(store.candidate(candidate.candidate_id))
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "确认发布", 0, "publish-twice")
    with pytest.raises(PlantkgError) as error:
        service.propose_publish(store.candidate(candidate.candidate_id))
    assert error.value.code == ErrorCode.STATE_CONFLICT and "已发布" in error.value.message


def test_tighten_candidate_requires_minimum_samples(assembled, tmp_path):
    """P2-③：收紧候选也要最小样本数，1 条抽查错误不触发。"""
    service, store, _versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    category = "load_spike|air_compressor"
    store.feedback(category, "rejected", system_error=True, ticket_id="one-reject", source="sampling")
    result = service.record_feedback("ticket-tighten-one", {"category": category, "outcome": "approved",
                                                            "source": "sampling"})
    assert result["candidate_id"] is None, "只有 1 条抽查错误，不应触发收紧候选"
    for index in range(10):
        store.feedback(category, "rejected", system_error=True, ticket_id=f"reject-{index}", source="sampling")
    result = service.record_feedback("ticket-tighten-many", {"category": category, "outcome": "approved",
                                                             "source": "sampling"})
    assert result["candidate_id"], "样本够且错误率高于上限，应生成收紧候选"


def _downgrade_setup(assembled):
    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
    from plantkg.world.scenario.script import load_scenario

    registry = assembled.registry
    versions = registry.resolve(VersionStore)
    reviewer = Actor(actor_id="reviewer", roles=(Role.reviewer,))
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    runner = ScenarioRunner(world, ScenarioDB.from_writer(assembled.storage.writer, world.site.plant_id),
                            registry=default_registry(registry), mode="evaluation", group="coordinated")
    runner.start()
    context = Context(plant_id=versions.plant_id, industry_pack="manufacturing", actor=reviewer, channel="offline",
                      trace_id="downgrade-e2e", as_of=runner.now,
                      version_manifest_id=runner.run_info.version_manifest_id, run_id=runner.run_info.run_id)
    store = EvolutionStore(versions.writer, versions.plant_id)
    hook = registry.hooks_for("evolution.record_correction")[-1]
    actions = registry.resolve(PendingActionStore)
    return registry, versions, store, hook, context, actions, reviewer


def test_b47_downgrade_policy_candidate_publishes_and_takes_effect(assembled):
    """P1-①：4 次驳回 + 1 次通过生成的 L2→L1 降级候选能过回测并确认发布，类别等级变 L1。"""
    registry, versions, store, hook, context, actions, reviewer = _downgrade_setup(assembled)
    category = "load_spike|air_compressor"
    created = [hook(context, f"ticket://dg-{index}", {"feedback": {
        "category": category, "event_type": "load_spike", "outcome": "rejected", "source": "review"}}).data.get("candidate_id")
        for index in range(4)]
    hook(context, "ticket://dg-ok", {"feedback": {
        "category": category, "event_type": "load_spike", "outcome": "approved", "source": "review"}})
    candidate_id = next((cid for cid in created if cid), None)
    assert candidate_id, "驳回应生成降级候选"
    publish = registry.tools["propose_publish"].handler(context, candidate_id=candidate_id, rationale="发布降级")
    assert publish.ok, publish.to_agent_text()
    report = store.report(store.candidate(candidate_id).backtest_report_id)
    assert report.verdict == "pass", report.failures
    assert report.metrics["candidate"]["target"] > report.metrics["base"]["target"]
    action = actions.get(publish.data["action_id"])
    actions.confirm(action.action_id, reviewer, "确认降级", action.revision, "dg-confirm-1")
    active_policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        versions.active().components[ManifestComponent.policy]))
    assert active_policy.autonomy_levels[category] == "L1"
    assert registry.hooks_for("evolution.published_autonomy")[-1](category) == "L1"
    published = [row for row in store.timeline() if row.get("event") == "published"][-1]
    assert published.get("autonomy_levels", {}).get(category) == "L1"


def test_b47_downgrade_after_published_upgrade_takes_effect(assembled):
    """P1-②：已发布 L3 后再走降级候选，确认发布后以已发布等级为准（变 L2）。"""
    registry, versions, store, hook, context, actions, reviewer = _downgrade_setup(assembled)
    category = "load_spike|air_compressor"
    result = None
    for index in range(3):
        result = hook(context, f"ticket://up-{index}", {"feedback": {
            "category": category, "event_type": "load_spike", "outcome": "approved", "source": "review"}})
    upgrade_id = result.data["candidate_id"]
    assert upgrade_id
    publish = registry.tools["propose_publish"].handler(context, candidate_id=upgrade_id, rationale="发布升级")
    assert publish.ok, publish.to_agent_text()
    action = actions.get(publish.data["action_id"])
    actions.confirm(action.action_id, reviewer, "确认升级", action.revision, "up-confirm-1")
    assert registry.hooks_for("evolution.published_autonomy")[-1](category) == "L3"

    hook(context, "ticket://down-0", {"feedback": {  # 第一次驳回只累计，第二次才降级
        "category": category, "event_type": "load_spike", "outcome": "rejected", "source": "review"}})
    downgrade = hook(context, "ticket://down-1", {"feedback": {
        "category": category, "event_type": "load_spike", "outcome": "rejected", "source": "review"}})
    downgrade_id = downgrade.data["candidate_id"]
    assert downgrade_id, "驳回应生成降级候选"
    context = context.model_copy(update={"version_manifest_id": versions.active().version_manifest_id,
                                         "run_id": None})
    publish = registry.tools["propose_publish"].handler(context, candidate_id=downgrade_id, rationale="发布降级")
    assert publish.ok, publish.to_agent_text()
    report = store.report(store.candidate(downgrade_id).backtest_report_id)
    assert report.verdict == "pass", report.failures
    action = actions.get(publish.data["action_id"])
    actions.confirm(action.action_id, reviewer, "确认降级", action.revision, "down-confirm-1")
    assert registry.hooks_for("evolution.published_autonomy")[-1](category) == "L2"
    active_policy = PolicyConfig.model_validate(policy_artifacts(versions.writer, versions.plant_id).load(
        versions.active().components[ManifestComponent.policy]))
    assert active_policy.autonomy_levels[category] == "L2"
    published = [row for row in store.timeline() if row.get("event") == "published"][-1]
    assert published.get("autonomy_levels", {}).get(category) == "L2"
