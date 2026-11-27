"""覆盖核验：contract-check.md 的落点真实存在；04 契约表字段与端口签名逐项落地。

只核验“设计文档点名的接口/字段在契约里存在且命名一致”；字段语义是否满足各任务卡的业务要求，
由各分支开工时按卡核对（见 contract-check.md「核验级别」）。
"""

import inspect
import re

import pytest

from plantkg.core import contracts as c
from plantkg.core import ports
from tests.contracts.conftest import ROOT

DOC = (ROOT / "docs" / "architecture" / "contract-check.md").read_text(encoding="utf-8")
BRANCHES = [f"B{n:02d}" for n in range(1, 51)]  # v0.8 的 50 个执行分支，编号见 docs/development/branch-plan.json
INTERNAL = {"replace_shifted_load"}  # 卡片点名但标为模块内部、不进契约的函数


def table_rows():
    return {m.group(1): line for line in DOC.splitlines() if (m := re.match(r"^\| (B\d\d)", line))}


def test_every_branch_has_its_own_row():
    rows = table_rows()
    assert sorted(rows) == BRANCHES


def _resolves(token: str) -> bool:
    routes = {re.sub(r"\{[^}]+\}", "{}", r.path) for r in c.API_ROUTES}
    if token.startswith("/"):
        path = re.sub(r"\{[^}]+\}", "{}", token.split("?")[0])
        return any(r.startswith(path[:-1]) for r in routes) if path.endswith("*") else path in routes
    if token.endswith((".md", ".json")):
        return any(ROOT.rglob(token.split("/")[-1]))
    base = re.split(r"[.(\[ =/]", token)[0]
    fields = {f for m in c.record_models().values() for f in m.model_fields}
    return hasattr(c, base) or hasattr(ports, base) or base in {t.name for t in c.TOOL_SPECS} or base in fields or base in INTERNAL


@pytest.mark.parametrize("branch", BRANCHES)
def test_landing_points_exist(branch):
    landing = table_rows()[branch].split("|")[3]
    missing = [t for t in re.findall(r"`([^`]+)`", landing) if not _resolves(t.strip())]
    assert not missing, f"{branch} 登记的落点在契约中不存在：{missing}"


# 04「关键记录与数据契约」逐字段：04 中的名字 → 契约中的字段（点号表示嵌套）
FIELDS_04 = {
    "Context": ["plant_id", "industry_pack", "actor", "actor.roles", "agent_run_id", "run_id", "trace_id", "as_of", "version_manifest_id", "idempotency_key"],
    "RunSession": ["ctx", "actor", "actor.roles", "plant_id", "industry_pack", "as_of", "visible_document_paths", "agent_run_id", "trace_id", "expires_at", "origin"],
    "PendingAction": ["action_id", "kind", "payload", "proposed_by.run_id", "proposed_by.trace_id", "proposed_by.call_id", "status", "decided_by", "decided_at", "reason", "revision", "idempotency_key"],
    "ActionDecisionRequest": ["expected_revision", "idempotency_key", "reason", "edits"],
    "Job": ["job_id", "kind", "idempotency_key", "status", "result_ref", "started_at", "finished_at", "budget"],
    "SourceRecord": ["source_id", "source_version", "content_hash", "locator", "nature", "event_time", "available_at", "source_tz", "source_unit", "quantity", "value", "transform_steps", "quality"],
    "AssetVersion": ["asset_version_id", "source_uri", "parent_version_id", "content_hash"],
    "Entity": ["entity_id"],
    "OntologyCandidate": ["layer", "subject", "options", "evidence"],
    "JudgeResult": ["choice", "score", "prob", "coverage", "backend", "model", "calibration_version", "orders", "order_agree", "cross_backend_agree", "top_k", "candidate_recall_source"],
    "Rule": ["rule_id", "kind", "subject_ids", "condition", "quantity", "unit", "bounds", "effective_from", "effective_to", "available_at", "source_asset_version", "locator", "review_status", "reviewer", "compiler_version"],
    "StateSnapshot": ["run_id", "snapshot_id", "event_seq", "as_of", "pack_states", "version_manifest_id"],
    "Forecast": ["issued_at", "target.start", "target.end", "values", "lower", "upper", "model_version", "input_slice", "coverage"],
    "PlanningRequest": ["context", "snapshot_id", "forecast_ids", "rule_ids", "horizon", "step_minutes", "solver_budget", "packs"],
    "Plan": ["plan_id", "revision", "status", "solver_status", "valid_interval", "version_manifest_id", "snapshot_id", "forecast_ids", "actions"],
    "Action": ["action_id", "object", "quantity", "unit", "value", "interval", "refs", "evidence_ids"],
    "GateDecision": ["plan_id", "revision", "outcome", "reasons", "thresholds", "policy_version"],
    "Approval": ["actor", "approved_at", "action_id"],
    "ExecutionResult": ["executed", "rejected", "event_seq", "ledger", "forecast_error"],
    "Ledger": ["entries", "source_refs", "total_cost_cny", "peak_import_kw"],
    "LedgerEntry": ["residual_kwh", "soc_end"],
    "VersionManifest": ["components", "manifest_hash", "status", "previous_active_id"],
    "Candidate": ["candidate_id", "kind", "base_manifest_id", "changes", "status", "validation_policy_version", "publish_action_id"],
    "BacktestReport": ["split_ids", "metrics", "constraint_violations", "failures", "thresholds", "policy_version"],
    "EvidenceEvent": ["event_id", "seq", "agent_run_id", "run_id", "trace_id", "call_id", "action_id", "event_type", "path", "rule_ids", "sources", "version_manifest_id", "available_at", "nature", "result_status", "params"],
    "ToolResult": ["schema_version", "ok", "business_status", "job_id", "data", "error.code", "error.message", "error.retryable", "evidence_ids", "run_id", "version_manifest_id"],
    "UIEvent": ["type", "agent_run_id", "trace_id", "call_id", "seq", "nexent_ref"],
    "PlanResult": ["solver_status", "solver", "mip_gap", "objective_value", "objective_bound", "elapsed_s", "objective_terms"],
    "SolverBudget": ["time_limit_s", "mip_rel_gap", "threads", "random_seed"],
}
# 04 写法与契约不同之处（契约决定，04 未定死的序列化细节）
FIELD_ALIASES_04 = {
    ("PendingAction", "expected_revision"): "revision（预期修订放在 ActionDecisionRequest.expected_revision）",
    ("Ledger", "来源"): "source_refs",
}
VERSION_COMPONENTS_04 = {"ontology", "rules", "parameters", "data_sources", "compilers", "models", "skills", "policy",
                          "sensor_cases"}  # policy：v0.8 03 §3.4～§3.5（B46）；sensor_cases：B32 D59 案例累积列表
UI_EVENT_TYPES_04 = {"run_started", "tool_started", "tool_finished", "evidence_added", "job_progress", "review_required",
                     "answer_delta", "plan_ready", "run_finished", "run_failed", "run_cancelled", "keepalive"}


def _field_exists(model, path: str) -> bool:
    head, _, rest = path.partition(".")
    field = model.model_fields.get(head)
    if field is None:
        return False
    if not rest:
        return True
    for arg in [field.annotation, *getattr(field.annotation, "__args__", ())]:
        if isinstance(arg, type) and issubclass(arg, c.Record) and _field_exists(arg, rest):
            return True
    return False


@pytest.mark.parametrize("record", sorted(FIELDS_04))
def test_04_record_fields(record):
    model = getattr(c, record)
    missing = [f for f in FIELDS_04[record] if not _field_exists(model, f)]
    assert not missing, f"{record} 缺 04 契约表字段：{missing}"


def test_04_enumerations():
    assert {m.value for m in c.ManifestComponent} == VERSION_COMPONENTS_04
    assert {t.value for t in c.UIEventType} == UI_EVENT_TYPES_04
    assert {k.value for k in c.PendingActionKind} == {"review_decision", "plan_confirmation", "publish", "rollback",
                                                      "work_order", "correction", "sampling", "human_decision",
                                                      "review", "question"}  # 后四种：v0.8（B46）；review：L1 先做后审（#169）；question：B35 回答卡片
    assert {s.value for s in c.JobStatus} == {"pending", "running", "succeeded", "failed", "cancelled"}
    assert {s.value for s in c.SolverStatus} == {"optimal", "feasible", "timeout", "infeasible", "error"}
    assert {n.value for n in c.DataNature} == {"measured", "derived", "configured", "synthetic", "simulated"}


# 04「端口签名」：端口.方法 → 04 中的参数名（按顺序）；别名为契约改名处
SIGNATURES_04 = {
    "Adapter.normalize": ["source_ref", "mapping", "visibility"],
    "StateProvider.at": ["context", "instant"],
    "StateProvider.advance": ["context", "expected_seq", "next_instant"],
    "Forecaster.predict": ["context", "horizon", "visible_history"],
    "RuleKind.compile": ["rule", "context"],
    "DomainPack.contribute": ["model_builder", "state", "rules", "forecasts"],
    "Planner.solve": ["request"],
    "Gate.assess": ["plan", "forecast_quality", "rule_status", "baseline"],
    "Executor.execute": ["confirmed_plan", "context", "expected_seq", "idempotency_key"],
    "Evaluator.compare": ["executions", "comparable_baselines"],
    "Judge.decide": ["state", "question", "options"],
    "CandidateGenerator.propose": ["trigger", "base_manifest", "context"],
    "Backtester.run": ["candidate", "frozen_split", "context"],
    "VersionStore.activate": ["expected_active", "candidate"],
    "EvidenceSink.append": ["event"],
    "EvidenceSink.list": ["run_id", "cursor", "visibility"],
    "EvidenceSink.subscribe": ["trace_id"],
    "Repository.append_once": ["key", "record"],
    "CtxResolver.resolve": ["ctx"],
    "RunSessionStore.register": ["actor", "plant_id", "as_of", "visible_docs", "ttl_s"],
    "RunSessionStore.expire": ["ctx"],
    "JobRunner.submit": ["kind", "payload", "idempotency_key"],
    "JobRunner.get": ["job_id", "wait_seconds"],
    "PendingActionStore.propose": ["kind", "payload", "context"],
    "PendingActionStore.confirm": ["action_id", "actor", "reason", "expected_revision"],
    "PendingActionStore.reject": ["action_id", "actor", "reason", "expected_revision"],
    "Gateway.run": ["actor", "query", "agent_name"],
    "Gateway.stop": ["agent_run_id"],
    "ModelBuilder.add_var": ["name", "domain", "bounds", "unit"],
    "ModelBuilder.add_constraint": ["name", "expr", "refs"],
    "ModelBuilder.register_balance": ["side", "expr", "label"],
    "ModelBuilder.register_cost": ["expr", "label"],
}
SIGNATURE_ALIASES_04 = {"plant→plant_id", "ttl→ttl_s", "user→actor（Gateway.run，actor 由网关认证）"}


@pytest.mark.parametrize("name", sorted(SIGNATURES_04))
def test_04_port_signatures(name):
    port, method = name.split(".")
    params = [p for p in inspect.signature(getattr(getattr(ports, port), method)).parameters if p != "self"]
    expected = SIGNATURES_04[name]
    assert params[: len(expected)] == expected, f"{name} 参数 {params} 与 04 {expected} 不一致"
