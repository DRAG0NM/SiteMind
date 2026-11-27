"""第四轮 #242：回测报告可信性、反馈稳健性、公开接口与 B43 复审遗留。"""
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from plantkg.core.contracts import (
    Candidate, CandidateKind, CandidateStatus, ComponentRef, ErrorCode, ExecutionRecord, ManifestComponent,
    PlantkgError, Rule,
)
from plantkg.core.ports import KnowledgePublisher, PendingActionStore
from plantkg.evolution.service import _report_outcome, report_artifact_hash, verify_report_binding
from plantkg.evolution.skills import history, miner
from plantkg.evolution.skills.generator import _skill_body as _generated_skill_body, has_pending_candidate
from plantkg.evolution.skills.validation import SkillValidationPolicy, _forbidden_hits
from plantkg.storage.migration import _declared_columns, _live_columns
from tests.evolution.test_backtest_default import _service, assembled  # noqa: F401  (assembled 是 fixture)
from tests.evolution.test_skill_evolution import (
    _body, _candidate_with_body, _environment, _pattern, _skill_body, _step,
)


# ---------------------------------------------------------------- A 回测报告可信性

def test_report_hash_binds_candidate_scenario_and_rejects_tampering(assembled, tmp_path):
    service, store, versions, _artifacts, _reviewer, spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-hash", {
        "note": "照明异常需核查总配电柜", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    report = service.backtest(candidate.candidate_id, [dict(spec, seed=11)])
    saved = store.candidate(candidate.candidate_id)
    assert report.policy_version == "b32-v4" and report.artifact_hash == report_artifact_hash(saved, report.binding, _report_outcome(report))
    scenario = report.binding["scenarios"][0]
    assert scenario["seed"] == 11 and len(scenario["sha256"]) == 64  # seed 真正传进试算，场景文件内容摘要入哈希
    assert {"cold_s", "hot_s"} <= set(report.binding["timing"]) and report.binding["machine"]["cpu_count"]
    coverage = report.metrics["coverage"]
    assert coverage["deterministic_fallbacks"] == (coverage["base_deterministic_fallbacks"]
                                                   + coverage["candidate_deterministic_fallbacks"])
    verify_report_binding(saved, report)
    forged_ref = ComponentRef(ref=saved.changes[ManifestComponent.rules].ref, content_hash="sha256:" + "1" * 64)
    forged = saved.model_copy(update={"changes": {ManifestComponent.rules: forged_ref}})
    with pytest.raises(PlantkgError) as mismatch:
        verify_report_binding(forged, report)
    assert mismatch.value.code == ErrorCode.PRECONDITION and "回测报告与候选内容不符" in mismatch.value.message
    with pytest.raises(PlantkgError):  # 篡改报告里的绑定信息
        verify_report_binding(saved, report.model_copy(update={"binding": {**report.binding, "base_deterministic_fallbacks": 99}}))
    for field, value in (("verdict", "fail"), ("metrics", {**report.metrics, "candidate": {"target": 99.0}}),
                         ("failures", ("伪造",)), ("constraint_violations", {"base": 0, "candidate": 9})):
        with pytest.raises(PlantkgError):  # 报告结论也进哈希：改 verdict/metrics/failures/违例都会被发现
            verify_report_binding(saved, report.model_copy(update={field: value}))
    verify_report_binding(saved, report.model_copy(update={"policy_version": "b32-v2", "binding": {}}))  # 历史报告按旧口径
    with pytest.raises(PlantkgError) as dev:  # 开发期 b32-v3（哈希不含结论）须重新回测
        verify_report_binding(saved, report.model_copy(update={"policy_version": "b32-v3"}))
    assert "b32-v3" in dev.value.message and "重新回测" in dev.value.message
    with pytest.raises(PlantkgError) as update:  # update_candidate 不允许改 changes
        store.update_candidate(forged)
    assert update.value.code == ErrorCode.STATE_CONFLICT
    versions.writer.submit(lambda con: con.execute(  # 绕过 update_candidate 直接改库：发布前重算哈希仍拒绝
        "UPDATE b32_candidates SET body=? WHERE plant_id=? AND candidate_id=?",
        (forged.model_dump_json(), versions.plant_id, saved.candidate_id)))
    with pytest.raises(PlantkgError) as publish:
        service.propose_publish(store.candidate(saved.candidate_id))
    assert "回测报告与候选内容不符" in publish.value.message


def test_tampered_report_in_database_blocks_publish(assembled, tmp_path):
    service, store, versions, _artifacts, _reviewer, spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-tamper", {
        "note": "照明异常需核查总配电柜", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    report = service.backtest(candidate.candidate_id, [spec])
    assert report.verdict == "pass"

    def write_report(body):
        versions.writer.submit(lambda con: con.execute(
            "UPDATE b32_reports SET body=? WHERE plant_id=? AND report_id=?",
            (body.model_dump_json(), versions.plant_id, report.report_id)))
    for field, value in (("metrics", {**report.metrics, "candidate": {**report.metrics["candidate"], "target": 99.0}}),
                         ("verdict", "fail")):
        write_report(report.model_copy(update={field: value}))
        with pytest.raises(PlantkgError) as error:
            service.propose_publish(store.candidate(candidate.candidate_id))
        assert error.value.code == ErrorCode.PRECONDITION, field
    write_report(report)
    service.propose_publish(store.candidate(candidate.candidate_id))  # 还原后可以发布


def test_publish_rechecks_scenario_file_and_versions(assembled, tmp_path):
    service, store, versions, _artifacts, _reviewer, spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-recheck", {
        "note": "照明异常需核查总配电柜", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    report = service.backtest(candidate.candidate_id, [spec])
    saved = store.candidate(candidate.candidate_id)

    def rebound(**changes):
        binding = {**report.binding, **changes}
        return report.model_copy(update={"binding": binding, "artifact_hash": report_artifact_hash(
            saved, binding, _report_outcome(report))})
    first = dict(report.binding["scenarios"][0])
    missing = rebound(scenarios=[{**first, "path": "configs/scenarios/no_such_scenario.yaml"}])  # 相对路径按仓库根解析
    with pytest.raises(PlantkgError) as gone:
        verify_report_binding(saved, missing)
    assert "不存在" in gone.value.message
    relative = rebound(scenarios=[{**first, "path": "configs/scenarios/fast/b32_demo.yaml", "sha256": "0" * 64}])
    with pytest.raises(PlantkgError) as changed:
        verify_report_binding(saved, relative)
    assert "已改动" in changed.value.message
    verify_report_binding(saved, report, assembled.registry)  # 版本标识与当前一致
    assembled.registry.register_hook("evolution.backtest_versions", lambda: {"profile": "other"})
    with pytest.raises(PlantkgError) as versions_changed:
        verify_report_binding(saved, report, assembled.registry)
    assert "版本" in versions_changed.value.message


def test_event_type_correction_without_target_events_is_inconclusive(assembled, tmp_path):
    """复现：event_type=door_alarm 不在 router 事件域/场景里，默认快场景下 target_events=0 不能 pass。"""
    service, _store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-door", {
        "note": "门禁报警先核对门磁", "event_type": "door_alarm",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    report = service.fast_backtest(candidate.candidate_id)
    assert report.metrics["coverage"]["target_events"] == 0
    assert report.verdict == "inconclusive" and report.failures == ("目标类事件未出现在回测窗口",)


def test_baseline_cache_is_not_reused_after_scenario_content_changes(assembled, tmp_path):
    """A.1 场景绑定：改场景文件后旧基准摘要不能复用，否则候选侧与基准侧跑的不是同一份场景。"""
    service, _store, versions, _artifacts, _reviewer, spec = _service(assembled, tmp_path)
    scenario = Path(spec["path"])
    first = service.record_correction("ticket-cache-1", {
        "note": "缓存键第一次", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    service.backtest(first.candidate_id, [spec], fast=True)  # 预热并写入基准摘要缓存
    same = service.record_correction("ticket-cache-2", {
        "note": "缓存键第二次", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    cached = service.backtest(same.candidate_id, [spec], fast=True)
    assert cached.metrics["coverage"]["baseline_reused"] == 1.0  # 场景未改：复用基准
    scenario.write_text(scenario.read_text(encoding="utf-8").replace("组合异常", "组合异常（已改）"), encoding="utf-8")
    changed = service.record_correction("ticket-cache-3", {
        "note": "缓存键第三次", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    report = service.backtest(changed.candidate_id, [spec], fast=True)
    assert report.metrics["coverage"]["baseline_reused"] == 0.0, "场景内容已改，不能复用旧基准"
    assert report.binding["scenarios"][0]["sha256"] == hashlib.sha256(scenario.read_bytes()).hexdigest()


def test_no_target_events_is_inconclusive_for_rule_candidates(assembled, tmp_path):
    service, _store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    assembled.registry.register_hook("evolution.backtest_evaluator", lambda *_: {
        "scenario_id": "zero", "executed": True, "ledger_verified": True, "target_events": 0,
        "targets_unmeasurable": True, "targets_event_like": True, "metrics": {"target": {"base": 0.0, "candidate": 0.0}}})
    candidate = service.record_correction("ticket-zero", {
        "note": "组合异常核查", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    report = service.backtest(candidate.candidate_id, [{"scenario_id": "zero", "seed": 1}])
    assert report.verdict == "inconclusive" and report.failures == ("目标类事件未出现在回测窗口",)


def test_trapped_one_sentence_correction_passes_and_publishes(assembled, tmp_path):
    """困人属安全类事件，不经规则命中路径：按处置结果（转工单）命中计数，一句话纠错 → 回测 pass → 发布。"""
    service, store, versions, _artifacts, reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-trapped", {
        "note": "困人后先广播安抚再转工单", "event_type": "elevator_trapped",
        "entity_id": f"entity://{versions.plant_id}/elevator/FE-2"})
    action = service.propose_publish(store.candidate(candidate.candidate_id))
    report = store.report(store.candidate(candidate.candidate_id).backtest_report_id)
    assert report.verdict == "pass" and report.metrics["candidate"]["target"] > report.metrics["base"]["target"]
    assembled.registry.resolve(PendingActionStore).confirm(action.action_id, reviewer, "确认发布", 0, "opt4-trapped")
    assert versions.knowledge_version(versions.active().version_manifest_id) >= 2


# ---------------------------------------------------------------- B 反馈稳健性

def _feedback_env(tmp_path):
    env = _environment(tmp_path)
    env.service.config = env.service.config | {"default_policy": {"autonomy_unseen_level": "L2"}}
    return env


def test_downgrade_needs_min_system_error_rejections(tmp_path):
    env = _feedback_env(tmp_path)
    category = "load_spike|air_compressor"
    reject = lambda ticket, **kw: env.service.record_feedback(ticket, {"category": category, "outcome": "rejected", **kw})
    assert reject("T-1")["candidate_id"] is None  # 单次驳回不降级
    assert reject("T-2", system_error=False)["candidate_id"] is None  # “不是系统判断有误”不计入
    assert reject("T-3")["candidate_id"] is not None  # 第二次系统判断有误才降级
    assert env.service.config["autonomy_downgrade_min"] == 2  # configs/defaults.yaml 的默认值生效


def test_downgrade_min_comes_from_config_and_resets_after_level_change(tmp_path):
    env = _feedback_env(tmp_path)
    env.service.config = env.service.config | {"autonomy_downgrade_min": 3}
    category = "load_spike|air_compressor"
    reject = lambda ticket: env.service.record_feedback(ticket, {"category": category, "outcome": "rejected"})
    assert reject("T-1")["candidate_id"] is None and reject("T-2")["candidate_id"] is None  # 临时设 3：两次不够
    assert reject("T-3")["candidate_id"] is not None
    # 该类别等级变更发布后，之前的驳回不再计数，避免连环降级
    env.store.event({"event": "published", "candidate_id": "c", "authorized_category": category,
                     "at": datetime.now(timezone.utc).isoformat()})
    env.store.clear_autonomy_pending(env.store.autonomy_pending_candidate(category) or "")
    env.store.put_autonomy_state(category, 0, None)
    assert reject("T-4")["candidate_id"] is None


def test_sampling_window_filters_source_before_limit_and_ignores_null_source(tmp_path):
    env = _feedback_env(tmp_path)
    env.service.config = env.service.config | {"feedback_window": 10}
    store, category = env.store, "pv_drop|pv"
    for index in range(10):  # 旧数据（source 为空）与纠错记录不能挤掉抽查样本，也不计入抽查错误率
        store.feedback(category, "rejected", ticket_id=f"old-{index}", source=None)
    store.feedback(category, "approved", ticket_id="s-1", source="sampling")
    for index in range(3):
        store.feedback(category, "rejected", ticket_id=f"c-{index}", source="correction")
    rows, fallback = env.service._sampling_rows(category)
    assert [row["ticket_id"] for row in rows] == ["s-1"] and fallback is None
    assert store.feedback_categories() == [category]
    store.feedback("probe", "approved", ticket_id="x", source=None)
    assert "probe" not in store.feedback_categories()


def test_composite_category_falls_back_to_event_type_samples(tmp_path):
    env = _feedback_env(tmp_path)
    for index in range(3):
        env.store.feedback("heat_wave", "approved", ticket_id=f"h-{index}", source="sampling")
    rows, fallback = env.service._sampling_rows("heat_wave|chiller")
    assert len(rows) == 3 and fallback == "heat_wave"
    assert env.service._sampling_rows("heat_wave")[1] is None


def test_declared_columns_compare_types_and_primary_key():
    import sqlite3
    ddl = "CREATE TABLE IF NOT EXISTS t(plant_id TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT, PRIMARY KEY(plant_id, seq));"
    con = sqlite3.connect(":memory:")
    con.execute(ddl.replace(" IF NOT EXISTS", ""))
    assert _declared_columns(ddl, "t") == _live_columns(con, "t")
    changed = ddl.replace("body TEXT", "body BLOB")
    assert _declared_columns(changed, "t") != _live_columns(con, "t")
    assert _declared_columns(ddl.replace("PRIMARY KEY(plant_id, seq)", "PRIMARY KEY(plant_id)"), "t") != _live_columns(con, "t")


# ---------------------------------------------------------------- C 公开接口与依赖

def test_mining_candidate_reports_consumer_mismatch_instead_of_publishing(assembled):
    publisher = assembled.registry.resolve(KnowledgePublisher)
    mining = Candidate(
        candidate_id="cand-b26-test", kind="knowledge", base_manifest_id="vm-b26-workorder-demo",
        changes={ManifestComponent.rules: ComponentRef(ref="rules@b26-workorder-abc", content_hash="sha256:" + "a" * 64)},
        status="proposed", trigger="历史工单挖掘", created_at=datetime.now(timezone.utc),
        validation_policy_version="b26-workorder-mining-v1")
    assert publisher.supports_candidate(mining) is False  # mining.publish 据此落资产等 B30 结构化
    with pytest.raises(PlantkgError) as mismatch:
        publisher.propose_publish(mining)
    assert mismatch.value.code == ErrorCode.PRECONDITION and "consumer_mismatch" in mismatch.value.message


def test_publish_fork_is_idempotent_by_candidate_and_manifest(assembled, tmp_path):
    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
    from plantkg.world.scenario.script import Scenario

    registry = assembled.registry
    _service_, _store, versions, artifacts, reviewer, _spec = _service(assembled, tmp_path)
    world = build_world(Scenario.model_validate({"scenario_id": "opt4-fork", "title": "分叉幂等", "date": "2026-07-01",
                                                 "seed": 7, "initial": {"soc": 0.5}}))
    runner = ScenarioRunner(world, ScenarioDB.from_writer(assembled.storage.writer, world.site.plant_id),
                            registry=default_registry(registry), mode="evaluation")
    parent = runner.start()
    registry.hooks_for("evolution.scenario_runner")[-1](parent.run_id, runner=runner)
    other = Rule(rule_id="opt4-fork-rule", kind="hvac_setpoint", subject_ids=(f"entity://{versions.plant_id}/zone/admin",),
                 condition={"action": "keep_setpoint"}, quantity="setpoint", unit="degC", value=23.0,
                 config_ref="config://opt4/fork", effective_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                 available_at=datetime(2025, 1, 1, tzinfo=timezone.utc), nature="synthetic",
                 review_status="approved", reviewer=reviewer.actor_id)
    manifest = versions.prepare({"rules": artifacts.prepare([other], reviewer).model_dump(mode="json")}, "opt4 fork target")
    if manifest.status == "prepared":
        manifest = versions.check_integrity(manifest.version_manifest_id)
    versions.activate(versions.active().version_manifest_id, manifest.version_manifest_id, reviewer, "opt4 fork target")
    fork = registry.hooks_for("evolution.fork_published")[-1]
    fork(parent.run_id, manifest.version_manifest_id, SimpleNamespace(candidate_id="cand-fork"))
    fork(parent.run_id, manifest.version_manifest_id, SimpleNamespace(candidate_id="cand-fork"))  # 重试
    children = [entry for entry in runner.trace.runs if entry.forked_from == parent.run_id]
    assert len(children) == 1  # 重试后仍只有一个子运行
    assert runner.run_info.version_manifest_id == manifest.version_manifest_id  # 子运行绑定新版本
    assert runner.db.runs.get_run(parent.run_id).status == "finished"  # 父运行已标 forked 结束


# ---------------------------------------------------------------- D 时间线

def test_timeline_events_carry_actor_and_component_diff(assembled, tmp_path):
    service, store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-diff", {
        "note": "照明异常需核查总配电柜", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    proposed = next(item for item in store.timeline() if item.get("event") == "candidate_proposed"
                    and item["candidate_id"] == candidate.candidate_id)
    assert proposed["actor"] == "reviewer"
    diff = {row["component"]: row for row in proposed["component_diff"]}
    assert diff["rules"]["to_ref"].startswith("b30-rules:") and diff["rules"]["from_ref"]
    assert diff["skills"]["from_ref"] is None


# ---------------------------------------------------------------- E B43 复审遗留

def test_maintenance_is_allowed_but_cut_supply_stays_forbidden(tmp_path):
    vocabulary = SimpleNamespace(hooks_for=lambda name: (lambda: {
        "aliases": {"repair": ("maintenance",), "rescue": ("maintenance",), "isolate_leak": ("stop",)},
        "cut_supply": ("power_off", "shed", "curtail", "stop", "maintenance")},) if name == "b43.action_vocabulary" else ())
    policy = SkillValidationPolicy(None, registry=vocabulary)
    forbidden, aliases, cut = policy._rules()
    assert _forbidden_hits({"steps": [_step("repair"), _step("rescue")]}, forbidden, aliases, cut) == ()
    assert _forbidden_hits({"steps": [_step("repair"), _step("isolate_leak")]}, forbidden, aliases, cut) == ("isolate_leak",)


def test_forbidden_commands_hook_returning_string_is_not_iterated_by_character(tmp_path):
    registry = SimpleNamespace(hooks_for=lambda name: ((lambda: "ventilate"),) if name == "b43.skill_forbidden_commands" else ())
    forbidden, _aliases, _cut = SkillValidationPolicy(None, registry=registry)._rules()
    assert "ventilate" in forbidden and "v" not in forbidden


def test_forged_evidence_cannot_be_validated_against_real_execution_records(tmp_path):
    env = _environment(tmp_path)
    real = [ExecutionRecord(ticket_id=f"T-{i}", event_type="compressor_pressure_drop", entity_type="air_compressor",
                            command="ventilate", action_index=1, outcome="approved") for i in range(3)]
    registry = SimpleNamespace(hooks_for=lambda name: ((lambda run_id=None: real),) if name == miner.EXECUTION_RECORDS_HOOK else ())
    policy = SkillValidationPolicy(env.artifacts, registry=registry)
    report = env.service.fast_backtest(_candidate_with_body(
        env, dict(_skill_body("genuine", [_step("ventilate")], tickets=("T-0", "T-1", "T-2")), nature="derived"),
        "b43-genuine").candidate_id)
    genuine = env.store.candidate("b43-genuine")
    assert policy.evaluate(genuine, report)["verdict"] == "validated"
    forged = _candidate_with_body(
        env, dict(_skill_body("forged", [_step("ventilate")], tickets=("T-0", "T-1", "T-FAKE")), nature="derived"), "b43-forged")
    result = policy.evaluate(forged, report)
    assert result["verdict"] == "rejected" and "T-FAKE" in result["reasons"][0]


def test_repeated_propose_publish_returns_the_pending_action(assembled, tmp_path):
    service, store, versions, _artifacts, _reviewer, _spec = _service(assembled, tmp_path)
    candidate = service.record_correction("ticket-twice", {
        "note": "照明异常需核查总配电柜", "event_type": "unknown_combo",
        "entity_id": f"entity://{versions.plant_id}/lighting/LT-P"})
    first = service.propose_publish(candidate)
    second = service.propose_publish(store.candidate(candidate.candidate_id))
    assert first.action_id == second.action_id


def test_skill_history_records_override_rollback_to_none_and_pending_candidate(tmp_path):
    env = _environment(tmp_path)
    v1 = _candidate_with_body(env, _skill_body("flow-a", [_step("ventilate")]), "b43-v1")
    action = env.service.propose_publish(v1)
    env.actions.confirm(action.action_id, env.reviewer, "发布 v1", 0, "opt4-v1")
    after_v1 = env.versions.active().version_manifest_id
    # 同一个 skills 槽被 B32 调查模板顶掉
    override = Candidate(
        candidate_id="b32-investigation", kind=CandidateKind.skill, base_manifest_id=after_v1,
        changes={ManifestComponent.skills: env.artifacts.prepare(
            {"format_version": 1, "kind": "investigation", "event_type": "x", "steps": ["查读数"]}, env.reviewer)},
        status=CandidateStatus.proposed, trigger="纠错：调查模板", created_at=datetime.now(timezone.utc),
        validation_policy_version="b32-v1")
    env.store.put_candidate(override)
    env.actions.confirm(env.service.propose_publish(override).action_id, env.reviewer, "发布调查模板", 0, "opt4-override")
    chain = history.skill_history(env.versions, env.store, "flow-a")
    assert [record.action for record in chain] == ["publish", "overridden"]
    assert history.skill_summaries(env.versions, env.store)[0].status == "overridden"
    # v1 发布后又挖出待发布的 v2：列表追加一条 candidate 条目
    env.store.event({"event": "skill_candidate_created", "candidate_id": "b43-v2", "skill_name": "flow-a", "version": 2,
                     "event_type": "compressor_pressure_drop", "source_tickets": ["T-9"],
                     "at": datetime.now(timezone.utc).isoformat()})
    v2 = _candidate_with_body(env, dict(_skill_body("flow-a", [_step("ventilate")]), version=2), "b43-v2")
    assert v2.status == "proposed"
    statuses = [item.status for item in history.skill_summaries(env.versions, env.store) if item.name == "flow-a"]
    assert "candidate" in statuses and len(statuses) == 2


def test_rollback_to_manifest_without_skill_records_rollback_to_none(tmp_path):
    env = _environment(tmp_path)
    v1 = _candidate_with_body(env, _skill_body("flow-r", [_step("ventilate")]), "b43-r1")
    env.actions.confirm(env.service.propose_publish(v1).action_id, env.reviewer, "发布", 0, "opt4-r1")
    rollback = env.service.propose_rollback(env.base.version_manifest_id, "回到基线")
    env.actions.confirm(rollback.action_id, env.reviewer, "回滚", 0, "opt4-r2")
    chain = history.skill_history(env.versions, env.store, "flow-r")
    assert [record.action for record in chain] == ["publish", "rollback_to_none"]
    last = chain[-1]
    assert (last.version, last.candidate_id, last.manifest_id, last.from_manifest_id, last.current) == (
        1, "b43-r1", env.base.version_manifest_id, chain[0].manifest_id, False)
    assert history.skill_summaries(env.versions, env.store)[0].status == "rolled_back"


def test_stale_pending_candidate_no_longer_blocks_mining(tmp_path):
    env = _environment(tmp_path)
    candidate = _candidate_with_body(env, _skill_body("flow-b", [_step("ventilate")]), "b43-stale")
    env.store.event({"event": "skill_candidate_created", "candidate_id": candidate.candidate_id, "skill_name": "flow-b",
                     "version": 1, "event_type": "compressor_pressure_drop", "at": datetime.now(timezone.utc).isoformat()})
    assert has_pending_candidate(env.store, "compressor_pressure_drop", candidate.base_manifest_id) is True
    assert has_pending_candidate(env.store, "compressor_pressure_drop", "vm-other-active") is False  # base 已过期
    assert env.store.candidate("b43-stale").status == "rejected"
    assert any(item["event"] == "skill_candidate_stale" for item in env.store.timeline())


def test_mining_prefers_b47_records_and_marks_synthetic_fallback(tmp_path):
    real = [ExecutionRecord(ticket_id=f"T-{i}", event_type="compressor_pressure_drop", entity_type="air_compressor",
                            command="ventilate", outcome="approved") for i in range(3)]
    with_hook = SimpleNamespace(hooks_for=lambda name: ((lambda run_id=None: real),))
    records, gap = miner.execution_records(with_hook)
    pattern = miner.mine_patterns(records)[0]
    assert gap is False and pattern.nature == "derived" and pattern.success_count == 3
    body = _generated_skill_body(pattern, "mined-skill", 1, {})
    assert body["coverage_gap"] is False and body["nature"] == "derived"  # skill 体也要能看出输入来源
    records, gap = miner.execution_records(SimpleNamespace(hooks_for=lambda name: ()))
    assert gap is True and all(item["nature"] == "synthetic" for item in records)
    fallback = replace(miner.mine_patterns(records)[0], coverage_gap=gap)  # 与挖掘入口一致：回退 fixtures 时标覆盖不足
    assert fallback.nature == "synthetic"
    fallback_body = _generated_skill_body(fallback, "demo-skill", 1, {})
    assert fallback_body["coverage_gap"] is True and fallback_body["nature"] == "synthetic"


def test_candidate_and_creation_event_share_one_transaction_and_timeline_index(tmp_path):
    env = _environment(tmp_path)
    candidate = Candidate(
        candidate_id="b43-tx", kind=CandidateKind.skill, base_manifest_id=env.versions.active().version_manifest_id,
        changes={ManifestComponent.skills: env.artifacts.prepare(_skill_body("flow-c", [_step("ventilate")]), env.reviewer)},
        status=CandidateStatus.proposed, trigger="测试", created_at=datetime.now(timezone.utc),
        validation_policy_version="b43-v1")
    env.store.put_candidate(candidate, {"event": "skill_candidate_created", "candidate_id": "b43-tx",
                                        "skill_name": "flow-c", "event_type": "e1", "at": "2026-07-01T00:00:00+00:00"})
    assert [item["candidate_id"] for item in env.store.timeline_events("skill_candidate_created", skill_name="flow-c")] == ["b43-tx"]
    rows = env.writer.submit(lambda con: con.execute(
        "EXPLAIN QUERY PLAN SELECT body FROM b32_timeline WHERE plant_id=? AND json_extract(body, '$.event')=? "
        "AND json_extract(body, '$.event_type')=?", ("factory", "skill_candidate_created", "e1")).fetchall())
    assert any("b32_timeline_event_lookup" in str(row) for row in rows)


def test_event_driven_reads_stored_correction_and_scenario_path_is_repo_relative(assembled, tmp_path):
    from plantkg.bootstrap import _repo_relative
    service, store, versions, _a, _r, _spec = _service(assembled, tmp_path)
    old = Candidate(candidate_id="b32-old", kind="knowledge", base_manifest_id=versions.active().version_manifest_id,
                    changes={ManifestComponent.rules: ComponentRef(ref="b30-rules:x", content_hash="sha256:" + "3" * 64)},
                    status="proposed", trigger="人工纠错：t-old；旧候选", created_at=datetime.now(timezone.utc),
                    validation_policy_version="b32-v1")
    assert service._event_driven(old) is False
    store.correction("t-old", {"event_type": "door_alarm"})  # 合并前的候选：时间线事件没有 event_type，纠错内容里有
    assert service._event_driven(old) is True
    from pathlib import Path
    import plantkg
    root = Path(plantkg.__file__).resolve().parents[1]
    assert _repo_relative(root / "configs" / "scenarios" / "fast" / "b32_demo.yaml") == "configs/scenarios/fast/b32_demo.yaml"
    assert _repo_relative(tmp_path / "x.yaml") == str(tmp_path / "x.yaml")


def test_ticket_signatures_merge_all_event_types_of_one_ticket():
    rows = [{"ticket_id": "T-1", "event_type": "a", "entity_type": "pump", "command": "start", "action_index": 1},
            {"ticket_id": "T-1", "event_type": "b", "entity_type": "valve", "command": "open", "action_index": 2}]
    assert miner.ticket_signatures(rows)["T-1"] == (("pump", "start"), ("valve", "open"))


def test_published_level_change_and_rollback_reset_downgrade_count(tmp_path):
    from plantkg.evolution.artifacts import policy_artifacts
    env = _feedback_env(tmp_path)
    policies = policy_artifacts(env.writer, "factory")
    if not env.versions.has_verifier(policies.prefix):
        env.versions.register_verifier(policies.prefix, policies.verify)
    category = "load_spike|air_compressor"
    reject = lambda ticket: env.service.record_feedback(ticket, {"category": category, "outcome": "rejected"})
    assert reject("T-1")["candidate_id"] is None
    down = reject("T-2")["candidate_id"]
    assert down
    action = env.service.propose_publish(env.store.candidate(down))  # 真实链路：提议发布 + 确认
    env.actions.confirm(action.action_id, env.reviewer, "确认降级", 0, "opt4-down")
    assert env.service.published_autonomy(category) == "L1"
    assert reject("T-3")["candidate_id"] is None  # 发布前的两次驳回不再计数
    rollback = env.service.propose_rollback(env.base.version_manifest_id, "回到基线")
    env.actions.confirm(rollback.action_id, env.reviewer, "回滚", 0, "opt4-rollback-level")
    assert env.service._last_level_change_at(category) is not None
    assert reject("T-4")["candidate_id"] is None  # 回滚改变了该类别等级：再次从 0 计数（只有 T-4 一次）
