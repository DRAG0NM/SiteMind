"""生成 contracts/examples 下的样例：valid/ 有效样本、invalid/ 故意无效的反例、mcp/ 与 rest/ 调用样例。

    python contracts/examples/make_examples.py

样例场景：虚拟华东电子厂 demo-electronics，厂区日 2026-07-01（Asia/Shanghai），as_of 为 D-1 18:00。
反例在有效样本上做单项改动，expect_error 是校验报错里应出现的片段。
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from plantkg.core import contracts as c  # noqa: E402

OUT = Path(__file__).resolve().parent


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


POLICY_HASH = sha("江苏省工商业分时电价政策（示例占位，原文待 B12 选定核实）")
POLICY_ID = f"asset://manufacturing/{POLICY_HASH[7:15]}/policy/tou-2026"
HIPE_HASH = sha("HIPE main_terminal 2017-10-23 week sample")
CTX = "3f9c2a7e5b1d4c8e9a0b6d2f7e1c4a5b"  # 样例用；真实 ctx 为 128 位随机数
AS_OF = "2026-06-30T10:00:00Z"  # 厂区 D-1 18:00
DAY_START = "2026-06-30T16:00:00Z"  # 厂区 2026-07-01 00:00
DAY_END = "2026-07-01T16:00:00Z"
PV = "entity://demo-electronics/pv_array/PV1"
BESS = "entity://demo-electronics/battery/BESS1"
GRID = "entity://demo-electronics/grid_connection/MAIN"
PUMP = "entity://demo-electronics/operation/JOB1"
ACTOR = {"actor_id": "u-reviewer-1", "roles": ["viewer", "reviewer"], "display_name": "复核员甲"}

V: dict[str, tuple[str, dict[str, Any]]] = {}


def valid(name: str, model: str, data: dict[str, Any]) -> dict[str, Any]:
    V[name] = (model, data)
    return data


context = valid("context_gateway", "Context", {
    "plant_id": "demo-electronics", "industry_pack": "manufacturing", "actor": ACTOR, "channel": "gateway",
    "agent_run_id": "ar-20260630-0001", "run_id": "run-day-20260701", "trace_id": "tr-0001", "as_of": AS_OF,
    "version_manifest_id": "vm-0003", "idempotency_key": "idem-0001",
})
valid("context_no_ctx", "Context", {
    "plant_id": "demo-electronics", "industry_pack": "manufacturing", "actor": None, "channel": "none",
    "trace_id": "tr-0002", "as_of": AS_OF,
})
valid("context_offline", "Context", {
    "plant_id": "demo-electronics", "industry_pack": "manufacturing", "actor": {"actor_id": "loop.py", "roles": ["operator"]},
    "channel": "offline", "run_id": "run-day-20260701", "trace_id": "tr-loop-1", "as_of": AS_OF, "version_manifest_id": "vm-0003",
})
valid("run_session", "RunSession", {
    "ctx": CTX, "actor": ACTOR, "plant_id": "demo-electronics", "industry_pack": "manufacturing", "as_of": AS_OF,
    "visible_document_paths": ["kb/policy/tou-2026.pdf"], "agent_run_id": "ar-20260630-0001", "trace_id": "tr-0001",
    "created_at": "2026-10-30T02:00:00Z", "expires_at": "2026-10-30T03:00:00Z",
})
origin = {"agent_run_id": "ar-20260630-0001", "run_id": "run-day-20260701", "trace_id": "tr-0001", "call_id": "call-0007", "tool": "propose_plan_confirmation"}
valid("pending_action_proposed", "PendingAction", {
    "action_id": "act-0001", "kind": "plan_confirmation", "plant_id": "demo-electronics",
    "payload": {"plan_id": "plan-20260701", "rationale": "门控转人工：光伏预测区间偏宽"}, "proposed_by": origin,
    "proposed_at": "2026-10-30T02:01:00Z", "expires_at": "2026-10-31T02:01:00Z", "status": "proposed", "revision": 0,
    "target_ref": "plan-20260701", "target_revision": 1, "idempotency_key": "idem-act-0001",
})
valid("pending_action_confirmed", "PendingAction", {
    **V["pending_action_proposed"][1], "status": "confirmed", "revision": 1, "decided_by": "u-reviewer-1",
    "decided_at": "2026-10-30T02:05:00Z", "reason": "核对动作依据后确认",
})
valid("action_decision_request", "ActionDecisionRequest", {"reason": "核对动作依据后确认", "expected_revision": 0, "idempotency_key": "idem-confirm-0001"})
valid("job_pending", "Job", {
    "job_id": "job-0001", "kind": "run_day", "idempotency_key": "idem-run-day-20260701", "status": "running",
    "submitted_at": "2026-10-30T02:00:10Z", "started_at": "2026-10-30T02:00:10Z", "budget": {"time_limit_s": 20},
    "progress": 0.4, "run_id": "run-day-20260701", "agent_run_id": "ar-20260630-0001",
})
valid("job_succeeded", "Job", {
    **V["job_pending"][1], "status": "succeeded", "progress": 1.0, "finished_at": "2026-10-30T02:00:11Z", "result_ref": "plan://plan-20260701@1",
})
valid("source_record_hipe", "SourceRecord", {
    "source_id": "hipe", "source_version": "hipe-2017-week-v1", "content_hash": HIPE_HASH,
    "locator": {"meter": "main_terminal", "time_range": {"start": "2017-10-23T06:00:00Z", "end": "2017-10-23T06:15:00Z"}},
    "nature": "measured", "event_time": "2026-07-01T00:00:00Z",
    "interval": {"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T00:15:00Z"}, "available_at": "2026-07-01T00:15:00Z",
    "source_tz": "Europe/Berlin", "source_unit": "W", "quantity": "active_power", "unit": "kW", "value": 412.5,
    "transform_steps": [
        {"op": "time_map_local_wall_clock", "params": {"source_start": "2017-10-23", "plant_start": "2026-07-01"}, "version": "tm-1"},
        {"op": "unit_w_to_kw", "params": {}, "version": "u-1"},
        {"op": "resample_15min_mean", "params": {}, "version": "rs-1"},
        {"op": "scale_load", "params": {"factor": 25, "nature": "configured"}, "version": "sc-1"},
    ],
    "quality": {"flag": "ok"},
})
valid("source_record_missing", "SourceRecord", {**V["source_record_hipe"][1], "value": None, "quality": {"flag": "missing", "note": "原始采样缺测"}})
valid("asset_version_policy", "AssetVersion", {
    "asset_version_id": POLICY_ID, "content_hash": POLICY_HASH, "industry": "manufacturing", "asset_type": "policy",
    "source_uri": "file://datasets/policy/tou-2026.pdf", "media_type": "application/pdf", "nature": "configured",
    "available_at": "2026-06-15T00:00:00Z", "ingested_at": "2026-10-30T01:00:00Z", "entity_ids": [GRID], "title": "分时电价政策（示例）",
})
valid("entity_battery", "Entity", {"entity_id": BESS, "entity_type": "battery", "label": "储能 1#", "aliases": ["BESS-1", "1号储能"], "plant_id": "demo-electronics", "ontology_class": "brick:Battery"})
valid("asset_fragment", "AssetFragment", {
    "fragment_id": "frag-0001", "asset_version_id": POLICY_ID, "locator": {"page": 2, "clause": "第三条"}, "kind": "text",
    "text": "高峰时段 8:00-11:00、17:00-22:00……", "available_at": "2026-06-15T00:00:00Z",
})
valid("ontology_candidate", "OntologyCandidate", {
    "candidate_id": "oc-0001", "layer": "concept", "subject": "point:AHU1_SAT", "options": ["Supply_Air_Temperature_Sensor", "Return_Air_Temperature_Sensor", "Mixed_Air_Temperature_Sensor"],
    "evidence": [{"perspective": "name", "excerpt": "AHU1_SAT"}], "recall_source": "vector",
})
valid("judge_result", "JudgeResult", {
    "candidate_id": "oc-0001", "choice": "Supply_Air_Temperature_Sensor", "score": 0.91, "prob": 0.88, "coverage": 1.0,
    "backend": "jev", "model": "decision-model-preview", "calibration_version": "cal-jev-1",
    "orders": [
        {"order": ["Supply_Air_Temperature_Sensor", "Return_Air_Temperature_Sensor", "Mixed_Air_Temperature_Sensor"], "choice": "Supply_Air_Temperature_Sensor", "distribution": {"Supply_Air_Temperature_Sensor": 0.9, "Return_Air_Temperature_Sensor": 0.06, "Mixed_Air_Temperature_Sensor": 0.04}},
        {"order": ["Mixed_Air_Temperature_Sensor", "Supply_Air_Temperature_Sensor", "Return_Air_Temperature_Sensor"], "choice": "Supply_Air_Temperature_Sensor", "distribution": {"Supply_Air_Temperature_Sensor": 0.92, "Return_Air_Temperature_Sensor": 0.05, "Mixed_Air_Temperature_Sensor": 0.03}},
        {"order": ["Return_Air_Temperature_Sensor", "Mixed_Air_Temperature_Sensor", "Supply_Air_Temperature_Sensor"], "choice": "Supply_Air_Temperature_Sensor", "distribution": {"Supply_Air_Temperature_Sensor": 0.91, "Return_Air_Temperature_Sensor": 0.05, "Mixed_Air_Temperature_Sensor": 0.04}},
    ],
    "order_agree": True, "cross_backend_agree": None,
    "top_k": [{"option": "Supply_Air_Temperature_Sensor", "prob": 0.91}, {"option": "Return_Air_Temperature_Sensor", "prob": 0.055}, {"option": "Mixed_Air_Temperature_Sensor", "prob": 0.035}],
    "candidate_recall_source": "vector", "vector_top": "Supply_Air_Temperature_Sensor", "gate_accept": True,
})
_tied = copy.deepcopy(V["judge_result"][1])
_tied["orders"][0].update(choice=None, distribution={"Supply_Air_Temperature_Sensor": 0.45, "Return_Air_Temperature_Sensor": 0.45, "Mixed_Air_Temperature_Sensor": 0.1})
_tied.update(order_agree=False, gate_accept=False)
valid("judge_result_tie_to_human", "JudgeResult", _tied)  # 一种顺序第一名平分：不自动接纳，转人工
valid("rule_tariff_policy", "Rule", {
    "rule_id": "rule-tou-peak-1", "kind": "tariff_period", "subject_ids": [GRID],
    "condition": {"local_time": ["08:00", "11:00"], "season": "summer"}, "quantity": "energy_price", "unit": "CNY/kWh", "value": 1.0812,
    "effective_from": "2026-06-01T00:00:00Z", "available_at": "2026-06-15T00:00:00Z",
    "source_asset_version": POLICY_ID, "locator": {"page": 2, "clause": "第三条"}, "nature": "configured",
    "review_status": "approved", "reviewer": "u-reviewer-1", "compiler_version": "tariff-compiler-1",
})
valid("rule_trusted_config", "Rule", {
    "rule_id": "rule-job1-window", "kind": "operation_window", "subject_ids": [PUMP],
    "condition": {"local_window": ["08:00", "18:00"]}, "quantity": "operation_start", "unit": "h", "bounds": {"lower": 8, "upper": 18},
    "effective_from": "2026-01-01T00:00:00Z", "available_at": "2026-01-01T00:00:00Z",
    "config_ref": "config://configs/plant.yaml#loads.job-1.shiftable", "nature": "configured", "review_status": "trusted_config",
})
valid("rule_candidate", "RuleCandidate", {
    "candidate_id": "rc-0001", "draft": {"kind": "tariff_period", "value": 1.0812, "unit": "CNY/kWh"},
    "alternatives": [{"kind": "tariff_period", "value": 1081.2, "unit": "CNY/kWh", "why": "单位误读 元/MWh"}],
    "extractor_version": "extract-1", "source_asset_version": POLICY_ID, "locator": {"page": 2, "clause": "第三条"}, "confidence": 0.82,
})
valid("pending_issue", "PendingIssue", {"issue_id": "pi-0001", "reason": "missing_price", "rule_id": None, "detail": "2026-07-01 12:00-12:15 无适用电价", "refs": [{"kind": "asset_version", "id": POLICY_ID}]})
valid("state_snapshot", "StateSnapshot", {
    "run_id": "run-day-20260701", "snapshot_id": "snap-0001", "event_seq": 0, "as_of": AS_OF, "version_manifest_id": "vm-0003",
    "pack_states": {"storage": {"energy_kwh": 500.0, "soc": 0.5}, "pv": {}, "tariff": {}, "production": {"pending_jobs": ["JOB1"]}},
})
valid("forecast_pv", "Forecast", {
    "forecast_id": "fc-pv-20260701", "quantity": "pv_available", "unit": "kW", "issued_at": AS_OF,
    "target": {"start": "2026-07-01T02:00:00Z", "end": "2026-07-01T03:00:00Z"}, "step_minutes": 15,
    "values": [310.2, 322.8, 330.1, 335.4], "lower": [280.0, 290.0, 300.0, 305.0], "upper": [340.0, 350.0, 360.0, 362.0],
    "model_version": "persistence-1", "params": {"pv_correction_k": 1.0},
    "input_slice": {"source_ids": ["pvgis"], "visible_until": AS_OF, "note": "持久性预测取 24h 前，不可见时段取 48h 前"}, "coverage": 1.0,
})
solver_budget = {"time_limit_s": 20, "mip_rel_gap": 0.001, "threads": 1, "random_seed": 0}
valid("planning_request", "PlanningRequest", {
    "context": context, "snapshot_id": "snap-0001", "forecast_ids": ["fc-pv-20260701", "fc-load-20260701"],
    "rule_ids": ["rule-tou-peak-1", "rule-job1-window"], "horizon": {"start": DAY_START, "end": DAY_END}, "step_minutes": 15,
    "solver_budget": solver_budget, "packs": ["pv", "storage", "tariff", "production"], "policy": "B2",
})
action_charge = {
    "action_id": "a-0001", "object": BESS, "quantity": "charge_power", "unit": "kW", "value": 200.0,
    "interval": {"start": "2026-07-01T03:00:00Z", "end": "2026-07-01T03:15:00Z"},
    "refs": [{"kind": "rule", "id": "rule-tou-peak-1"}, {"kind": "config", "id": "config://configs/plant.yaml#storage"}], "evidence_ids": ["ev-0011"],
}
action_job = {
    "action_id": "a-0002", "object": PUMP, "quantity": "operation_start", "unit": "count", "value": 1,
    "interval": {"start": "2026-07-01T04:00:00Z", "end": "2026-07-01T04:15:00Z"}, "refs": [{"kind": "rule", "id": "rule-job1-window"}],
}
valid("action", "Action", action_charge)
plan = valid("plan", "Plan", {
    "plan_id": "plan-20260701", "revision": 1, "run_id": "run-day-20260701", "status": "review_required", "solver_status": "optimal",
    "valid_interval": {"start": DAY_START, "end": DAY_END}, "version_manifest_id": "vm-0003", "snapshot_id": "snap-0001",
    "forecast_ids": ["fc-pv-20260701", "fc-load-20260701"], "policy": "B2", "actions": [action_charge, action_job],
    "objective_terms": {"tariff.energy_cost": 6235.4}, "created_at": "2026-10-30T02:00:11Z",
})
solver = {"name": "highs", "version": "1.15.1", "params": {"time_limit": 20, "mip_rel_gap": 0.001, "threads": 1, "random_seed": 0}}
valid("plan_result_optimal", "PlanResult", {
    "solver_status": "optimal", "plan": plan, "solver": solver, "mip_gap": 0.0004, "objective_value": 6235.4, "objective_bound": 6232.9,
    "elapsed_s": 0.42, "objective_terms": {"tariff.energy_cost": 6235.4},
})
valid("plan_result_infeasible", "PlanResult", {
    "solver_status": "infeasible", "plan": None, "solver": solver, "elapsed_s": 0.08,
    "conflict_hints": ["production.JOB1 窗口 [08:00,18:00) 放不下 12 小时工序"],
})
valid("contribution_receipt", "ContributionReceipt", {
    "pack": "storage", "variables": ["storage.pc", "storage.pd", "storage.u", "storage.E"], "constraints": ["storage.soc_recursion", "storage.mutex", "storage.end_energy"],
    "balance_terms": ["storage.charge", "storage.discharge"], "refs": [{"kind": "config", "id": "config://configs/plant.yaml#storage"}],
})
valid("constraint_contribution", "ConstraintContribution", {
    "rule_id": "rule-job1-window", "pack": "production", "constraint_names": ["production.JOB1.window"],
    "refs": [{"kind": "rule", "id": "rule-job1-window"}], "compiler_version": "operation-window-1",
})
valid("gate_decision", "GateDecision", {
    "plan_id": "plan-20260701", "revision": 1, "outcome": "review_required", "reasons": ["光伏预测区间宽度超过阈值"],
    "policy_version": "gate-fixed-1", "thresholds": {"pv_band_ratio": 0.2}, "assessed_at": "2026-10-30T02:00:12Z", "pending_action_id": "act-0001",
})
valid("approval_manual", "Approval", {"plan_id": "plan-20260701", "revision": 1, "mode": "manual", "actor": "u-reviewer-1", "approved_at": "2026-10-30T02:05:00Z", "action_id": "act-0001", "reason": "核对动作依据后确认"})
entry = {
    "interval": {"start": "2026-07-01T03:00:00Z", "end": "2026-07-01T03:15:00Z"},
    "pv_available_kwh": 82.0, "pv_used_kwh": 82.0, "pv_curtailed_kwh": 0.0, "grid_import_kwh": 71.0, "grid_export_kwh": 0.0,
    "charge_kwh": 50.0, "discharge_kwh": 0.0, "load_kwh": 103.0, "energy_start_kwh": 500.0, "energy_end_kwh": 547.5, "soc_end": 0.5475,
    "price_cny_per_kwh": 0.3195, "cost_cny": 22.68, "residual_kwh": 0.0,
}
valid("ledger_entry", "LedgerEntry", entry)
ledger = valid("ledger_execution", "Ledger", {
    "ledger_id": "led-exec-20260701", "kind": "execution", "run_id": "run-day-20260701", "plan_id": "plan-20260701", "plan_revision": 1,
    "version_manifest_id": "vm-0003", "entries": [entry], "total_cost_cny": 22.68, "peak_import_kw": 284.0,
    "tolerance_policy_version": "ledger-tol-1", "calculator_version": "ledger-calc-1", "source_refs": ["bundle-hipe-20260701", "bundle-pvgis-20260701"],
    "storage": {"capacity_kwh": 1000, "eta_c": 0.95, "eta_d": 0.95, "soc_min": 0.1, "soc_max": 0.9}, "balanced": True,
})
valid("execution_result", "ExecutionResult", {
    "run_id": "run-day-20260701", "plan_id": "plan-20260701", "plan_revision": 1, "event_seq": 12,
    "executed": [{"action_id": "a-0001", "planned_value": 200.0, "executed_value": 200.0, "unit": "kW", "interval": action_charge["interval"]}],
    "ledger": ledger, "soc_deviation": 0.0, "forecast_error": {"pv_available_mae_kw": 12.4}, "idempotency_key": "idem-exec-20260701-12",
})
valid("evaluation_report", "EvaluationReport", {
    "report_id": "eval-20260701", "eval_days": ["2026-07-01"], "independent_eval_day": True, "policies": ["B0", "B1", "B2"],
    "metrics": {"B0": {"cost_cny": 7120.0, "peak_import_kw": 980.0}, "B1": {"cost_cny": 6710.0, "peak_import_kw": 940.0}, "B2": {"cost_cny": 6235.4, "peak_import_kw": 905.0}},
    "comparable": True, "run_ids": ["run-b0", "run-b1", "run-day-20260701"],
})
manifest_components = {
    "ontology": {"ref": "ontology@v2", "content_hash": sha("ontology v2")},
    "rules": {"ref": "rules@v3", "content_hash": sha("rules v3")},
    "parameters": {"ref": "params@v1", "content_hash": sha("params v1 k=1.0")},
    "data_sources": {"ref": "sources@v1", "content_hash": sha("sources v1")},
}
valid("version_manifest_active", "VersionManifest", {
    "version_manifest_id": "vm-0003", "components": manifest_components, "status": "active", "previous_active_id": "vm-0002", "ever_active": True,
    "created_at": "2026-10-30T00:00:00Z", "activated_at": "2026-10-30T00:01:00Z", "activated_by": "u-reviewer-1", "reason": "发布电价规则 v3",
    "manifest_hash": sha("vm-0003"),
})
valid("candidate_validated", "Candidate", {
    "candidate_id": "cand-k-0001", "kind": "parameter", "base_manifest_id": "vm-0003",
    "changes": {"parameters": {"ref": "params@v2", "content_hash": sha("params v2 k=0.93")}}, "status": "validated",
    "trigger": "连续 3 天光伏实际低于预测", "created_at": "2026-10-30T03:00:00Z", "validation_policy_version": "param-policy-1",
    "backtest_report_id": "bt-0001",
})
valid("backtest_report", "BacktestReport", {
    "report_id": "bt-0001", "candidate_id": "cand-k-0001", "base_manifest_id": "vm-0003", "split_ids": ["split-validation-1"], "seed": 0,
    "metrics": {"base": {"cost_cny": 43210.0}, "candidate": {"cost_cny": 43050.0}}, "constraint_violations": {"base": 0, "candidate": 0},
    "thresholds": {"min_cost_saving_ratio": 0.0}, "policy_version": "param-policy-1", "verdict": "pass", "artifact_hash": sha("bt-0001"), "job_id": "job-0042",
})
valid("frozen_split", "FrozenSplit", {"split_id": "split-validation-1", "purpose": "validation", "days": ["2026-07-08", "2026-07-09"], "frozen_at": "2026-11-05T00:00:00Z", "content_hash": sha("split-validation-1")})
valid("evidence_event_tool", "EvidenceEvent", {
    "event_id": "ev-0010", "seq": 10, "occurred_at": "2026-10-30T02:00:10Z", "agent_run_id": "ar-20260630-0001", "run_id": "run-day-20260701",
    "trace_id": "tr-0001", "call_id": "call-0005", "event_type": "tool_called", "tool": "run_day", "path": "reasoning",
    "version_manifest_id": "vm-0003", "result_status": "pending", "params": {"plan_date": "2026-07-01", "policy": "B2"},
    "model_params": {"plan_date": "2026-07-01"},
})
valid("evidence_event_rule", "EvidenceEvent", {
    "event_id": "ev-0011", "seq": 11, "occurred_at": "2026-10-30T02:00:11Z", "agent_run_id": "ar-20260630-0001", "run_id": "run-day-20260701",
    "trace_id": "tr-0001", "action_id": "a-0001", "event_type": "rule_applied", "path": "reasoning", "rule_ids": ["rule-tou-peak-1"],
    "sources": [{"asset_version_id": POLICY_ID, "locator": {"page": 2, "clause": "第三条"}}], "version_manifest_id": "vm-0003",
    "available_at": "2026-06-15T00:00:00Z", "nature": "configured", "result_status": "ok",
})
valid("ui_event_tool_started", "UIEvent", {
    "type": "tool_started", "agent_run_id": "ar-20260630-0001", "trace_id": "tr-0001", "seq": 3, "ts": "2026-10-30T02:00:10Z",
    "call_id": "call-0005", "payload": {"tool": "plantkg_run_day", "model_params": {"plan_date": "2026-07-01"}}, "event_id": "ev-0010",
    "nexent_ref": {"type": "tool", "index": 4},
})
valid("ui_event_run_finished", "UIEvent", {
    "type": "run_finished", "agent_run_id": "ar-20260630-0001", "trace_id": "tr-0001", "seq": 41, "ts": "2026-10-30T02:00:40Z",
    "payload": {"terminal_reason": "completed_from_deltas"},
})
valid("ui_event_keepalive", "UIEvent", {"type": "keepalive", "agent_run_id": "ar-20260630-0001", "trace_id": "tr-0001", "seq": 20, "ts": "2026-10-30T02:00:25Z"})
valid("tool_result_done", "ToolResult", {
    "ok": True, "business_status": "done", "data": {"plan_id": "plan-20260701", "revision": 1, "actions": 2, "cost_cny": 6235.4},
    "evidence_ids": ["ev-0011"], "run_id": "run-day-20260701", "version_manifest_id": "vm-0003",
})
valid("tool_result_pending", "ToolResult", {"ok": True, "business_status": "pending", "job_id": "job-0001", "run_id": "run-day-20260701", "version_manifest_id": "vm-0003", "notice": "求解在后台继续；用同一 job_id 调 plantkg_get_job"})
valid("tool_result_degraded", "ToolResult", {"ok": True, "business_status": "degraded", "data": {"plans": []}, "notice": "本次运行没有网关会话，只能只读查看"})
valid("tool_result_failed", "ToolResult", {"ok": False, "business_status": "failed", "error": {"code": "PLANTKG_E_NO_CTX", "message": "写入需要经工作台网关发起的运行", "retryable": False}})
valid("tool_spec", "ToolSpec", c.TOOL_SPECS[0].model_dump(mode="json"))
valid("health", "HealthResponse", {"status": "ok", "profile": "mock", "service_version": "0.1.0", "contract_version": c.CONTRACT_VERSION, "dependencies": {"nexent": "disabled", "jev": "disabled"}})
valid("agent_run_request", "AgentRunRequest", {"agent_name": "plantkg_energy_assistant", "query": "明天的计划为什么在 11 点给电池充电？", "run_id": "run-day-20260701"})
valid("run_info", "RunInfo", {
    "run_id": "run-day-20260701", "plant_id": "demo-electronics", "kind": "replay_day", "version_manifest_id": "vm-0003",
    "clock": {"start": DAY_START, "end": DAY_END}, "as_of": AS_OF, "status": "running", "created_at": "2026-10-30T02:00:00Z",
})
valid("version_diff", "VersionDiff", {"from_manifest_id": "vm-0002", "to_manifest_id": "vm-0003", "changed": {"rules": ["rule-tou-peak-1"]}, "affected_plan_ids": ["plan-20260701"], "unknown": []})
valid("nexent_run_payload", "NexentRunPayload", {
    "agent_name": "plantkg_energy_assistant", "query": "明天的计划为什么在 11 点给电池充电？",
    "tool_params": {"agents": {"plantkg_energy_assistant": {"tools": {
        "plantkg_get_plan": {"ctx": CTX}, "plantkg_explain": {"ctx": CTX},
        "knowledge_base_search": {"document_paths": ["knowledge_base/tou-2026.md"]},
    }}}},
})
valid("agent_run_stopped", "AgentRunStopped", {"agent_run_id": "ar-20260630-0001", "status": "stopping"})
valid("metric_series", "MetricSeries", {"metric": "f1", "dataset": "plastering-sample", "split_id": "split-test-1", "points": [[0, 0.71], [20, 0.83], [50, 0.9]]})

# ---- v0.8（B46）：专家提案、试算、场景槽汇总、策略配置；“一天”整体样例见 contracts/examples/day.sample.json
T0805, T0806 = "2026-07-01T00:05:00Z", "2026-07-01T00:06:00Z"  # 厂区 08:05、08:06
EG = "entity://demo-electronics/elevator_group/EG-PROD"
valid("ledger_entry_breakdown", "LedgerEntry", {**entry, "load_breakdown": {"elevator": 8.0, "hvac": 30.0, "air_compressor": 20.0,
                                                                            "lighting": 5.0, "process": 30.0, "base": 10.0}})
valid("run_info_scenario_fork", "RunInfo", {
    **V["run_info"][1], "run_id": "run-scn-20260701-f1", "kind": "scenario", "forked_from": "run-scn-20260701",
    "forked_at": "2026-07-01T06:00:00Z", "energy_end_target_kwh": 500.0,
    "data_sources": [{"source": "D05", "dataset_version": HIPE_HASH}],
})
valid("evaluation_report_scenario", "EvaluationReport", {
    "report_id": "eval-scn-20260701", "eval_days": ["2026-07-01"], "independent_eval_day": True,
    "policies": ["baseline", "independent", "coordinated"],
    "metrics": {"baseline": {"cost_cny": 18620.0, "wait_min": 410.0}, "independent": {"cost_cny": 17940.0, "wait_min": 380.0},
                "coordinated": {"cost_cny": 17210.0, "wait_min": 300.0}},
    "comparable": True, "run_ids": ["run-scn-base", "run-scn-ind", "run-scn-20260701"],
})
proposal_draft = valid("expert_proposal_draft", "ExpertProposal", {
    "expert": "energy", "ticket_id": "ticket://tk-0001",
    "actions": [{"entity_id": BESS, "command": "discharge", "params": {"kw": 390},
                 "interval": {"start": T0806, "end": "2026-07-01T00:30:00Z"}, "readiness": "ready"},
                {"entity_id": EG, "command": "set_mode", "params": {"mode": "maintenance"},
                 "interval": {"start": T0806, "end": "2026-07-01T00:30:00Z"}, "readiness": "needs_diagnosis", "depends_on": ["diag:t1_root_cause"]}],
    "cost": {"cny": -38.2, "wait_min": 0, "comfort_min": 0}, "confidence": 0.82, "safety_tags": [],
})
valid("expert_proposal_saved", "ExpertProposal", {
    **proposal_draft, "proposal_id": "proposal://pp-0001", "diagnosis": "早高峰电梯与空压同时启动，T1 越限",
    "evidence_refs": ["rule://tou-2026#peak"],
    "binding": {"run_id": "run-scn-20260701", "knowledge_version": 1, "ticket_id": "ticket://tk-0001", "round": 1, "state_version": 5, "trigger_at": T0805},
    "timing": {"origin_event_at": T0805, "trigger_at": T0805, "effective_at": T0806, "requested_at": "2026-11-04T02:10:00Z",
               "returned_at": "2026-11-04T02:10:12.400Z", "latency_ms": 12400, "retries": 0},
})
valid("proposal_receipt", "ProposalReceipt", {"proposal_id": "proposal://pp-0001", "summary": "储能 08:06 起放电 390 kW，预计省 38.2 元"})
valid("expert_reply", "ExpertReply", {"status": "needs_clarification", "proposal_id": "proposal://pp-0001", "question": "T1 越限是否由空压机启动造成？"})
valid("sim_action", "SimAction", {"entity_id": EG, "command": "set_mode", "params": {"mode": "up_peak"}, "interval": {"start": T0806, "end": "2026-07-01T00:30:00Z"}})
valid("trial_result_infeasible", "TrialResult", {
    "feasible": False, "violations": [{"kind": "capacity", "entity_id": "entity://demo-electronics/transformer/T1", "at": T0806, "detail": "负载率 104%"}],
    "soft_cost": {"cny": 12.0, "wait_min": 35.0, "comfort_min": 0}, "minute_ledger": [],
})
valid("scenario_slot_summary", "ScenarioSlotSummary", {
    "interval": {"start": "2026-07-01T00:00:00Z", "end": "2026-07-01T00:15:00Z"}, "charge_kwh": 10.0, "discharge_kwh": 66.7,
    "net_storage_kwh": -56.7, "load_kwh": 225.0, "pv_available_kwh": 30.0, "pv_used_kwh": 30.0, "grid_import_kwh": 138.3,
    "grid_export_kwh": 0.0, "pv_curtailed_kwh": 0.0, "soc_end": 0.567, "cost_cny": 131.39, "peak_minute_kw": 860.0,
    "demand_15m_kw": 553.2, "minutes_balanced": True, "capacity_violations": [],
})
valid("policy_config", "PolicyConfig", {
    "policy_version": "policy-1", "arbitration_weights": {"cny": 1.0, "wait_min": 1.0, "comfort_min": 2.0},
    "priority_profiles": {"production_hours": ["safety", "production", "comfort", "cost"], "morning_peak": ["safety", "comfort", "production", "cost"],
                          "night": ["safety", "cost", "production", "comfort"]},
    "confidence_low": 0.3, "confidence_high": 0.7, "sampling_ratio": 0.2, "response_budget": {"layer12_min": 1, "layer3_min": 2},
})


# ---------------------------------------------------------------- 反例


valid("document_ingest_request", "DocumentIngestRequest", {
    "filename": "manual.txt", "text": "AC-2 空压机，主管压力5分钟内下降超过0.2 bar。",
    "expert": "equipment", "nature": "synthetic", "document_id": "manual-ac2", "idempotency_key": "manual-ac2-v1",
})
valid("document_ingest_receipt", "DocumentIngestReceipt", {
    "document_id": "manual-ac2", "asset_version_id": "asset://manufacturing/4f1bfb5c/process_doc/source",
    "content_hash": "sha256:4f1bfb5c85e1505070be6de368a1cf2f6a84691c8a1aa24966f6dee066153edd",
    "expert": "equipment", "nature": "synthetic", "available_at": "2026-11-15T06:10:53Z",
    "received_at": "2026-11-15T06:10:53Z", "source_name": "manual.txt", "status": "needs_review",
    "published": False, "note": "synthetic 契约样例，尚未发布",
})
valid("document_search_hit", "DocumentSearchHit", {
    "document_id": "manual-ac2", "asset_version_id": "asset://manufacturing/4f1bfb5c/process_doc/source",
    "locator": {"line_start": 1}, "text": "AC-2 空压机，主管压力5分钟内下降超过0.2 bar。",
    "score": 1.0, "expert": "equipment",
})


def drop(key: str) -> Callable[[dict], None]:
    return lambda d: d.pop(key)


# #245 人工干预（B47）：暂停自动执行、取消与撤销
valid("autonomy_pause_request", "AutonomyPauseRequest", {"reason": "巡检期间先只提议"})
valid("autonomy_resume_request", "AutonomyResumeRequest", {})
valid("autonomy_status", "AutonomyStatus", {
    "paused": True, "scope": "plant", "by": "u-reviewer-1", "since": "2026-07-01T00:20:00Z", "reason": "巡检期间先只提议",
    "backlog": 2})
valid("applied_action", "AppliedAction", {
    "action_id": "A-00003", "run_id": "run-day-20260701", "entity_id": BESS, "command": "discharge",
    "params": {"power_kw": 400.0}, "interval": {"start": "2026-07-01T00:06:00Z", "end": "2026-07-01T00:36:00Z"},
    "stage": "active", "protection": True, "ticket_id": "ticket://TK-0005"})
valid("action_cancel_request", "ActionCancelRequest", {"reason": "现场已经处理"})
valid("action_revoke_request", "ActionRevokeRequest", {
    "reason_kind": "system_wrong", "note": "放电时机判断有误", "confirm_protection": True})
valid("action_intervention", "ActionIntervention", {
    "action_id": "A-00003", "run_id": "run-day-20260701", "kind": "revoked", "operator": "u-reviewer-1",
    "at": "2026-07-01T00:31:00Z", "reason_kind": "system_wrong", "note": "放电时机判断有误", "protection": True,
    "effective_end": "2026-07-01T00:31:00Z", "original_end": "2026-07-01T00:36:00Z"})
valid("revoke_outcome", "RevokeOutcome", {"cost_cny": 251.36, "violation_minutes": 0, "t1_peak_kw": 1451.2})
valid("revoke_preview", "RevokePreview", {
    "action_id": "A-00003", "run_id": "run-day-20260701",
    "window": {"start": "2026-07-01T00:31:00Z", "end": "2026-07-01T00:36:00Z"},
    "revoke": {"cost_cny": 251.36, "violation_minutes": 0, "t1_peak_kw": 1451.2},
    "keep": {"cost_cny": 251.76, "violation_minutes": 0, "t1_peak_kw": 1453.2},
    "cost_delta_cny": -0.4, "violation_minutes_delta": 0, "t1_peak_delta_kw": -2.0, "protection": True,
    "note": "保护动作：撤销需要填写说明并二次确认"})


def put(path: str, value: Any) -> Callable[[dict], None]:
    def apply(d: dict) -> None:
        *parents, last = path.split(".")
        node = d
        for p in parents:
            node = node[int(p)] if p.isdigit() else node[p]
        node[last] = value
    return apply


INVALID: list[tuple[str, str, Callable[[dict], None], str]] = [
    ("source_record_no_unit", "source_record_hipe", drop("unit"), "unit"),
    ("source_record_bad_unit", "source_record_hipe", put("unit", "W"), "unit"),
    ("source_record_naive_time", "source_record_hipe", put("event_time", "2026-07-01T00:00:00"), "UTC"),
    ("source_record_local_time", "source_record_hipe", put("available_at", "2026-07-01T08:15:00+08:00"), "UTC"),
    ("source_record_early_available", "source_record_hipe", put("available_at", "2026-07-01T00:10:00Z"), "available_at"),
    ("source_record_null_as_ok", "source_record_hipe", put("value", None), "缺测"),
    ("source_record_no_nature", "source_record_hipe", drop("nature"), "nature"),
    ("source_record_short_hash", "source_record_hipe", put("content_hash", "sha256:abcd1234"), "content_hash"),
    ("rule_no_source", "rule_tariff_policy", lambda d: d.pop("source_asset_version"), "来源"),
    ("rule_no_unit", "rule_tariff_policy", drop("unit"), "unit"),
    ("rule_no_effective", "rule_tariff_policy", drop("effective_from"), "effective_from"),
    ("rule_no_available_at", "rule_tariff_policy", drop("available_at"), "available_at"),
    ("rule_approved_no_reviewer", "rule_tariff_policy", drop("reviewer"), "复核人"),
    ("rule_no_review_status", "rule_tariff_policy", drop("review_status"), "review_status"),
    ("rule_no_locator", "rule_tariff_policy", drop("locator"), "locator"),
    ("rule_no_subject", "rule_tariff_policy", put("subject_ids", []), "subject_ids"),
    ("rule_reversed_effect", "rule_tariff_policy", put("effective_to", "2026-05-01T00:00:00Z"), "effective_to"),
    ("context_gateway_no_actor", "context_gateway", put("actor", None), "actor"),
    ("context_no_ctx_with_actor", "context_no_ctx", put("actor", ACTOR), "actor"),
    ("context_offline_agent_run", "context_offline", put("agent_run_id", "ar-1"), "agent_run_id"),
    ("context_extra_field", "context_gateway", put("ctx", CTX), "Extra inputs"),
    ("run_session_short_ctx", "run_session", put("ctx", "1234"), "ctx"),
    ("pending_action_confirmed_no_reason", "pending_action_confirmed", drop("reason"), "原因"),
    ("pending_action_payload_ctx", "pending_action_proposed", put("payload.ctx", CTX), "注入参数"),
    ("job_succeeded_no_result", "job_succeeded", drop("result_ref"), "result_ref"),
    ("asset_hash_mismatch", "asset_version_policy", put("content_hash", sha("另一份内容")), "哈希"),
    ("judge_unknown_choice", "judge_result", put("choice", "其他"), "门控"),
    ("judge_prob_no_calibration", "judge_result", drop("calibration_version"), "calibration_version"),
    ("judge_gate_without_agree", "judge_result", put("order_agree", False), "顺序不一致"),
    ("forecast_issued_late", "forecast_pv", put("issued_at", "2026-07-01T02:30:00Z"), "发布"),
    ("forecast_future_input", "forecast_pv", put("input_slice.visible_until", "2026-06-30T12:00:00Z"), "泄漏"),
    ("forecast_wrong_length", "forecast_pv", put("values", [1.0, 2.0]), "values"),
    ("planning_request_no_manifest", "planning_request", put("context.version_manifest_id", None), "version_manifest_id"),
    ("action_no_refs", "action", put("refs", []), "refs"),
    ("action_bad_config_ref", "action", put("refs", [{"kind": "config", "id": "plant.yaml"}]), "config://"),
    ("plan_action_outside", "plan", put("actions.0.interval", {"start": "2026-07-01T16:00:00Z", "end": "2026-07-01T16:15:00Z"}), "有效区间"),
    ("plan_result_infeasible_with_plan", "plan_result_infeasible", put("plan", V["plan"][1]), "计划"),
    ("plan_result_infeasible_no_hint", "plan_result_infeasible", put("conflict_hints", []), "冲突线索"),
    ("gate_review_no_action", "gate_decision", put("pending_action_id", None), "待确认动作"),
    ("ledger_balanced_with_violation", "ledger_execution", put("violations", ["slot 12 残差 0.4 kWh"]), "balanced"),
    ("ledger_entry_negative", "ledger_entry", put("grid_import_kwh", -1.0), "grid_import_kwh"),
    ("execution_with_plan_ledger", "execution_result", put("ledger.kind", "plan"), "执行账本"),
    ("manifest_retired_never_active", "version_manifest_active", lambda d: d.update(status="retired", ever_active=False), "retired"),
    ("candidate_published_no_action", "candidate_validated", put("status", "published"), "待确认动作"),
    ("evidence_echo_ctx", "evidence_event_tool", put("params.ctx", CTX), "注入参数"),
    ("evidence_tool_no_call_id", "evidence_event_tool", put("call_id", None), "call_id"),
    ("ui_tool_no_call_id", "ui_event_tool_started", put("call_id", None), "call_id"),
    ("ui_finished_bad_reason", "ui_event_run_finished", put("payload.terminal_reason", "timeout"), "terminal_reason"),
    ("ui_payload_document_paths", "ui_event_tool_started", put("payload.document_paths", ["kb/a.pdf"]), "注入参数"),
    ("tool_result_echo_ctx", "tool_result_done", put("data.ctx", CTX), "注入参数"),
    ("tool_result_pending_no_job", "tool_result_pending", drop("job_id"), "job_id"),
    ("tool_result_ok_with_error", "tool_result_done", put("error", {"code": "PLANTKG_E_INTERNAL", "message": "x"}), "error"),
    ("tool_result_unknown_code", "tool_result_failed", put("error.code", "PLANTKG_E_WHATEVER"), "未登记"),
    ("interval_reversed", "action", put("interval", {"start": "2026-07-01T03:15:00Z", "end": "2026-07-01T03:00:00Z"}), "end"),
    # 对抗性审查（2026-10-30）补充的反例
    ("tool_result_nested_ctx", "tool_result_done", put("data.rows", [{"ctx": CTX}]), "注入参数"),
    ("evidence_nested_document_paths", "evidence_event_tool", put("params.filters", {"document_paths": ["kb/a.pdf"]}), "注入参数"),
    ("source_record_numeric_time", "source_record_hipe", put("event_time", 1782864000), "UTC"),
    ("source_record_lowercase_z", "source_record_hipe", put("event_time", "2026-07-01T00:00:00z"), "UTC"),
    ("source_record_nan", "source_record_hipe", put("value", float("nan")), "finite"),
    ("source_record_null_estimated", "source_record_hipe", lambda d: d.update(value=None, quality={"flag": "estimated"}), "缺测"),
    ("plan_result_status_mismatch", "plan_result_optimal", put("solver_status", "feasible"), "solver_status"),
    ("plan_result_no_bound", "plan_result_optimal", drop("objective_bound"), "下界"),
    ("plan_duplicate_action", "plan", put("actions.1.action_id", "a-0001"), "唯一"),
    ("execution_ledger_other_plan", "execution_result", put("ledger.plan_id", "plan-other"), "一致"),
    ("ledger_residual_over_tolerance", "ledger_execution", lambda d: d["entries"][0].update(load_kwh=108.0, residual_kwh=-5.0), "超出容差"),
    # 队友审查 PR#1（2026-10-30）补充
    ("ledger_entry_forged_residual", "ledger_entry", lambda d: d.update(pv_available_kwh=0.0, pv_used_kwh=0.0, grid_import_kwh=0.0, charge_kwh=0.0, load_kwh=100.0, energy_end_kwh=500.0, soc_end=0.5, residual_kwh=0.0), "重算"),
    ("ledger_soc_recursion", "ledger_execution", lambda d: d["entries"][0].update(energy_end_kwh=560.0, soc_end=0.56), "SOC 递推"),
    ("ledger_simultaneous_charge_discharge", "ledger_execution", lambda d: d["entries"][0].update(discharge_kwh=10.0, load_kwh=113.0), "同时充放"),
    ("ledger_no_storage_with_charge", "ledger_execution", put("storage", None), "无储能参数"),
    ("ledger_no_calculator", "ledger_execution", drop("calculator_version"), "calculator_version"),
    ("judge_two_orders", "judge_result", lambda d: d.update(orders=d["orders"][:2]), "不同选项顺序"),
    ("judge_repeated_order", "judge_result", lambda d: d.update(orders=[d["orders"][0]] * 3), "不同选项顺序"),
    ("judge_order_disagree", "judge_result", lambda d: d.update(order_agree=False, orders=d["orders"][:2] + [{**d["orders"][2], "choice": "Return_Air_Temperature_Sensor", "distribution": {"Supply_Air_Temperature_Sensor": 0.3, "Return_Air_Temperature_Sensor": 0.6, "Mixed_Air_Temperature_Sensor": 0.1}}]), "顺序不一致"),
    # 队友复审 PR#1（第二轮）：各顺序自身一致性
    ("judge_orders_claim_foreign_choice", "judge_result", lambda d: d.update(choice="A", vector_top="A", top_k=[], orders=[
        {"order": ["X"], "choice": "A", "distribution": {"X": 0.9}}, {"order": ["Y"], "choice": "A", "distribution": {"Y": 0.9}},
        {"order": ["Z"], "choice": "A", "distribution": {"Z": 0.9}}]), "唯一第一名"),
    ("judge_orders_different_sets", "judge_result", lambda d: d["orders"].__setitem__(2, {"order": ["Supply_Air_Temperature_Sensor", "Return_Air_Temperature_Sensor"], "choice": "Supply_Air_Temperature_Sensor", "distribution": {"Supply_Air_Temperature_Sensor": 0.9, "Return_Air_Temperature_Sensor": 0.1}}), "同一候选集"),
    ("judge_order_tie_claims_choice", "judge_result", put("orders.0.distribution", {"Supply_Air_Temperature_Sensor": 0.45, "Return_Air_Temperature_Sensor": 0.45, "Mixed_Air_Temperature_Sensor": 0.1}), "唯一第一名"),
    ("judge_tie_gate_accept", "judge_result", lambda d: d["orders"][0].update(choice=None, distribution={"Supply_Air_Temperature_Sensor": 0.45, "Return_Air_Temperature_Sensor": 0.45, "Mixed_Air_Temperature_Sensor": 0.1}), "平分或无法判定"),
    ("judge_missing_score", "judge_result", lambda d: d["orders"][0]["distribution"].pop("Mixed_Air_Temperature_Sensor"), "缺分数"),
    ("judge_choice_outside_set", "judge_result", lambda d: d.update(choice="Zone_Air_Temperature_Sensor", order_agree=False, gate_accept=False), "属于候选集"),
    ("judge_vector_mismatch", "judge_result", put("vector_top", "Return_Air_Temperature_Sensor"), "向量第一名"),
    ("judge_no_vector_top", "judge_result", drop("vector_top"), "向量第一名"),
    ("ledger_entry_pv_mismatch", "ledger_entry", put("pv_used_kwh", 90.0), "光伏"),
    ("forecast_window_leak", "forecast_pv", put("input_slice.window", {"start": "2026-06-30T00:00:00Z", "end": "2026-07-01T00:00:00Z"}), "泄漏"),
    ("pending_action_proposed_with_reason", "pending_action_proposed", put("reason", "提前写好"), "未决动作"),
    ("pending_action_decided_after_expiry", "pending_action_confirmed", put("decided_at", "2026-11-01T00:00:00Z"), "过期"),
    ("job_running_no_start", "job_pending", drop("started_at"), "started_at"),
    ("job_succeeded_with_error", "job_succeeded", put("error", {"code": "PLANTKG_E_INTERNAL", "message": "x"}), "error"),
    ("ontology_unknown_option", "ontology_candidate", put("options", ["Supply_Air_Temperature_Sensor", "Unknown"]), "Unknown"),
    ("judge_gate_no_orders", "judge_result", put("orders", []), "顺序"),
    ("manifest_empty_components", "version_manifest_active", put("components", {}), "components"),
    ("manifest_active_no_reason", "version_manifest_active", drop("reason"), "原因"),
    ("planning_request_no_ctx_context", "planning_request", lambda d: d["context"].update(channel="none", actor=None, agent_run_id=None), "只读"),
    ("context_gateway_no_agent_run", "context_gateway", put("agent_run_id", None), "agent_run_id"),
    ("entity_plant_mismatch", "entity_battery", put("plant_id", "other-plant"), "厂区"),
    ("tool_error_wrong_retryable", "tool_result_failed", put("error.retryable", True), "retryable"),
    # v0.8（B46）
    ("ledger_entry_breakdown_mismatch", "ledger_entry_breakdown", put("load_breakdown.base", 50.0), "负荷分项"),
    ("ledger_entry_breakdown_unknown", "ledger_entry_breakdown", put("load_breakdown.kitchen", 0.0), "load_breakdown"),
    ("run_info_forked_at_no_parent", "run_info_scenario_fork", put("forked_from", None), "forked_at"),
    ("proposal_needs_diagnosis_no_depends", "expert_proposal_draft", put("actions.1.depends_on", []), "depends_on"),
    ("proposal_binding_other_ticket", "expert_proposal_saved", put("binding.ticket_id", "ticket://tk-9999"), "ticket_id"),
    ("proposal_effective_before_trigger", "expert_proposal_saved", put("timing.effective_at", "2026-07-01T00:04:00Z"), "effective_at"),
    ("reply_clarification_no_question", "expert_reply", put("question", None), "question"),
    ("trial_feasible_with_violation", "trial_result_infeasible", put("feasible", True), "feasible"),
    ("slot_net_storage_mismatch", "scenario_slot_summary", put("net_storage_kwh", 0.0), "net_storage_kwh"),
    ("policy_priority_not_safety_first", "policy_config", put("priority_profiles.night", ["cost", "safety"]), "safety"),
]


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


MCP_CALLS = [
    ("run_day_pending", "run_day", {"plan_date": "2026-07-01", "policy": "B2", "ctx": CTX}, "tool_result_pending"),
    ("get_plan_done", "get_plan", {"plan_id": "plan-20260701", "ctx": CTX}, "tool_result_done"),
    ("get_plan_no_ctx", "get_plan", {"plan_id": "plan-20260701"}, "tool_result_degraded"),
    ("propose_plan_confirmation_no_ctx", "propose_plan_confirmation", {"plan_id": "plan-20260701", "expected_revision": 1, "rationale": "门控转人工", "ctx": ""}, "tool_result_failed"),
]

REST_CALLS = [
    ("health", "GET", "/health", "health"),
    ("get_plan", "GET", "/plans/{plan_id}", "plan"),
    ("get_job", "GET", "/jobs/{job_id}", "job_succeeded"),
    ("confirm_action", "POST", "/actions/{action_id}/confirm", "pending_action_confirmed"),
    ("get_run", "GET", "/runs/{run_id}", "run_info"),
]


def main() -> int:
    for sub in ("valid", "invalid", "mcp", "rest"):
        shutil.rmtree(OUT / sub, ignore_errors=True)
    for name, (model, data) in V.items():
        getattr(c, model).model_validate(data)  # 有效样本在写出前必须通过
        write(OUT / "valid" / f"{name}.json", {"model": model, "data": data})
    for name, base, mutate, expect in INVALID:
        model, data = V[base]
        bad = copy.deepcopy(data)
        mutate(bad)
        write(OUT / "invalid" / f"{name}.json", {"model": model, "base": base, "expect_error": expect, "data": bad})
    for name, tool, arguments, result in MCP_CALLS:
        result_model = c.ToolResult.model_validate(V[result][1])
        write(OUT / "mcp" / f"{name}.json", {"tool": c.TOOL_PREFIX + tool, "arguments": arguments, "result_text": result_model.to_agent_text()})
    for name, method, path, example in REST_CALLS:
        route = next(r for r in c.API_ROUTES if r.method == method and r.path == path)
        model, data = V[example]
        response = getattr(c, model).model_validate(data).model_dump(mode="json")  # 服务端按序列化口径返回全字段
        payload: dict[str, Any] = {"method": method, "path": c.API_PREFIX + path, "status": 200, "response": response}
        if route.request:
            payload["request"] = V["action_decision_request"][1]
        write(OUT / "rest" / f"{name}.json", payload)
    print(f"valid {len(V)}，invalid {len(INVALID)}，mcp {len(MCP_CALLS)}，rest {len(REST_CALLS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
