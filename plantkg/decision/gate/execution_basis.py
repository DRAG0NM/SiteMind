"""Validation of server-supplied execution basis before it may be frozen.

The basis provider is a trusted internal service: it must independently verify
the receipt and configuration provenance against the fixed run and manifest.
The checks here bind that provider's result to the actual materials and plan;
they cannot turn a caller-provided digest into evidence of trusted provenance.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
import re

from plantkg.core.contracts import ErrorCode, PlanningRequest, PlantkgError
from plantkg.core.versions import canonical, digest
from .policy import evaluate_policy


@dataclass(frozen=True)
class ExecutionBasis:
    receipt: object
    config: dict
    source_digest: str


class StoredExecutionBasisProvider:
    """Resolve a persisted receipt and independently verified configuration.

    Both callbacks are supplied by trusted server-side assembly. No default
    callbacks are installed; callers' metadata is not evidence of provenance.
    """
    def __init__(self, receipt_store, config_resolver, source_verifier):
        self.receipt_store = receipt_store
        self.config_resolver = config_resolver
        self.source_verifier = source_verifier

    def __call__(self, plan, materials):
        if materials.request is None:
            raise PlantkgError(ErrorCode.PRECONDITION, "缺少固定规划请求")
        context = materials.request.context
        receipt = self.receipt_store.load(context, plan.plan_id)
        config, source_digest = self.config_resolver(context, plan, receipt)
        if self.source_verifier(context, plan, receipt, config, source_digest) is not True:
            raise PlantkgError(ErrorCode.PRECONDITION, "执行配置来源未核验")
        return ExecutionBasis(receipt, config, source_digest)


def validate_basis(plan, materials, basis):
    if not isinstance(basis, ExecutionBasis):
        raise PlantkgError(ErrorCode.PRECONDITION, "缺少可信执行依据")
    export = basis.config.get("export") if isinstance(basis.config, dict) else None
    if any(action.quantity == "grid_export" for action in plan.actions):
        limit = export.get("limit_kw") if isinstance(export, dict) else None
        if not (isinstance(limit, (int, float)) and not isinstance(limit, bool) and math.isfinite(limit) and limit > 0):
            # 本期不外送：没有正的外送上限时，门控不得批准带外送动作的计划
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "本期不外送：外送动作缺少正的外送上限，拒绝批准执行")
    result = evaluate_policy(plan, materials)
    if result.outcome == "rejected" or result.confirmation_blockers or not materials.dependencies_verified:
        raise PlantkgError(ErrorCode.PRECONDITION, "执行依据的规则和来源尚未通过核验")
    try:
        receipt = basis.receipt.read()
        if not re.fullmatch(r"[0-9a-f]{64}", basis.source_digest):
            raise ValueError("source digest")
        if set(basis.config) != {"pv", "storage", "export", "tariff", "operations"}:
            raise ValueError("incomplete configuration")
        pv, storage, export, tariff, operations = (basis.config[k] for k in ("pv", "storage", "export", "tariff", "operations"))
        def number(value, *, positive=False):
            return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                    and (value > 0 if positive else value >= 0))
        if (not isinstance(export, dict) or not number(export.get("limit_kw")) or not export.get("ref")
                or not isinstance(tariff, dict) or tariff.get("unit") != "CNY/kWh"
                or not tariff.get("ref") or not isinstance(tariff.get("slots"), list) or not tariff["slots"]
                or not isinstance(operations, dict) or operations.get("complete") is not True
                or not isinstance(operations.get("items"), list)):
            raise ValueError("incomplete execution configuration")
        if "pv" in materials.request.packs and (not isinstance(pv, dict) or not pv.get("entity_id")
                or not number(pv.get("capacity_kw"), positive=True) or not pv.get("ref")):
            raise ValueError("incomplete PV configuration")
        plant_prefix = f"entity://{materials.request.context.plant_id}/"
        if isinstance(pv, dict) and not pv["entity_id"].startswith(plant_prefix):
            raise ValueError("PV plant mismatch")
        if "storage" in materials.request.packs and (not isinstance(storage, dict) or not storage.get("ref") or not storage.get("entity_id")
                or not all(number(storage.get(k), positive=True) for k in ("capacity_kwh", "charge_kw", "discharge_kw", "eta_c", "eta_d"))
                or not all(number(storage.get(k)) for k in ("soc_min", "soc_max", "reserve_ratio"))
                or not (storage["eta_c"] <= 1 and storage["eta_d"] <= 1 and storage["soc_min"] < storage["soc_max"] <= 1)):
            raise ValueError("incomplete storage configuration")
        if isinstance(storage, dict) and not storage["entity_id"].startswith(plant_prefix):
            raise ValueError("storage plant mismatch")
        if (receipt.get("export_limit_kw"), receipt.get("export_ref")) != (export["limit_kw"], export["ref"]):
            raise ValueError("export differs from solve receipt")
        pack_inputs = receipt["pack_evidence"]
        if set(pack_inputs) != set(materials.request.packs):
            raise ValueError("missing pack inputs")
        if "pv" in materials.request.packs:
            source_pv = pack_inputs["pv"]["config"]
            if (source_pv["entity_id"], source_pv["kwp"], source_pv["config_ref"]) != (pv["entity_id"], pv["capacity_kw"], pv["ref"]):
                raise ValueError("PV config differs from solve")
        if "storage" in materials.request.packs:
            source_storage = pack_inputs["storage"]["config"]
            if any(source_storage[k] != storage[k] for k in ("entity_id", "capacity_kwh", "charge_kw", "discharge_kw", "eta_c", "eta_d", "soc_min", "soc_max")) or source_storage["config_ref"] != storage["ref"]:
                raise ValueError("storage config differs from solve")
            if (storage["reserve_ratio"] > .5 or
                    pack_inputs["storage"]["parameters"][plan.version_manifest_id]["reserve_ratio"] != storage["reserve_ratio"]):
                raise ValueError("storage reserve differs from solve")
        if "production" in materials.request.packs:
            source_ops = pack_inputs["production"]["operations"]
            configured_ops = operations["items"]
            if len(source_ops) != len(configured_ops):
                raise ValueError("operation set differs from solve")
            for source, configured in zip(source_ops, configured_ops):
                if not source["entity_id"].startswith(plant_prefix):
                    raise ValueError("operation plant mismatch")
                profile = [source["rated_kw"] * value for value in source["shape"]["fractions"]]
                if (source["operation_id"], source["entity_id"], source["original_start"], profile) != (
                        configured["operation_id"], configured["entity_id"], configured["original_start"], configured["profile_kw"]):
                    raise ValueError("operation profile differs from solve")
                provenance_refs = [ref for ref in source["refs"] if ref["kind"] in ("config", "asset_version")]
                if (not provenance_refs or any((ref["kind"], ref["id"]) not in materials.verified_refs
                                               for ref in provenance_refs)):
                    raise ValueError("operation sources not verified")
        if ("tariff" in pack_inputs and "config_ref" in pack_inputs["tariff"]
                and pack_inputs["tariff"]["config_ref"] != tariff["ref"]):
            raise ValueError("tariff source differs from solve")
        refs = [export["ref"], tariff["ref"]] + ([pv["ref"]] if isinstance(pv, dict) else []) + ([storage["ref"]] if isinstance(storage, dict) else [])
        if any(("config", ref) not in materials.verified_refs for ref in refs):
            raise ValueError("configuration sources not verified")
        tariff_slots = []
        for slot in tariff["slots"]:
            start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
            if (start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0) or start >= end
                    or not number(slot["price_cny_per_kwh"])):
                raise ValueError("invalid tariff slot")
            tariff_slots.append((start, end))
        tariff_slots.sort()
        if (tariff_slots[0][0] > plan.valid_interval.start or tariff_slots[-1][1] < plan.valid_interval.end
                or any(left[1] != right[0] for left, right in zip(tariff_slots, tariff_slots[1:]))):
            raise ValueError("tariff coverage gap or overlap")
        if "tariff" in pack_inputs and "prices" in pack_inputs["tariff"]:
            prices = pack_inputs["tariff"]["prices"]
            step = timedelta(minutes=materials.request.step_minutes)
            count = (plan.valid_interval.end - plan.valid_interval.start) / step
            if count != int(count) or len(prices) != int(count):
                raise ValueError("tariff solve coverage mismatch")
            for i, price in enumerate(prices):
                instant = plan.valid_interval.start + i * step
                matching = [s for s in tariff["slots"] if datetime.fromisoformat(s["start"]) <= instant
                            and datetime.fromisoformat(s["end"]) >= instant + step]
                if len(matching) != 1 or matching[0]["price_cny_per_kwh"] != price:
                    raise ValueError("tariff differs from solve")
        # 回执完整性由 SolveReceipt 摘要保证；语义比较前按当前契约规范化，升级前保存的回执（缺新增默认字段）仍可确认
        if PlanningRequest.model_validate(receipt["request"]).model_dump(mode="json") != materials.request.model_dump(mode="json"):
            raise ValueError("request mismatch")
        fixed_snapshot = receipt["snapshot"]
        if (fixed_snapshot["run_id"], fixed_snapshot["snapshot_id"], fixed_snapshot["version_manifest_id"]) != (
                plan.run_id, plan.snapshot_id, plan.version_manifest_id):
            raise ValueError("planning snapshot mismatch")
        if datetime.fromisoformat(fixed_snapshot["as_of"]) > materials.request.context.as_of:
            raise ValueError("planning snapshot is not visible")
        expected_forecasts = sorted(canonical(f.model_dump(mode="json")) for f in materials.forecasts)
        captured_forecasts = sorted(canonical(f) for f in receipt["forecasts"]["forecasts"])
        if expected_forecasts != captured_forecasts:
            raise ValueError("forecast mismatch")
        planned = plan.model_dump(mode="json")
        planned["status"], planned["revision"] = "draft", receipt["plan"]["revision"]
        if planned != receipt["plan"]:
            raise ValueError("plan mismatch")
        expected_rules = sorted(canonical(rule.model_dump(mode="json")) for rule in materials.rules)
        captured_rules = sorted(canonical(rule) for rules in receipt["rules_by_pack"].values() for rule in rules)
        if expected_rules != captured_rules:
            raise ValueError("rule mismatch")
        if set(receipt["contributions"]) != set(materials.request.packs):
            raise ValueError("contribution mismatch")
        body = {"schema_version": 1, "plant_id": materials.request.context.plant_id,
                "run_id": plan.run_id, "manifest_id": plan.version_manifest_id, "plan_id": plan.plan_id,
                "request": receipt["request"], "draft_plan": receipt["plan"],
                "planning_snapshot": receipt.get("snapshot"), "forecasts": receipt.get("forecasts"),
                "rules": [json.loads(value) for value in expected_rules],
                "contributions": receipt["contributions"], "config": basis.config,
                "verified_refs": sorted([list(ref) for ref in materials.verified_refs]),
                "receipt_sha256": basis.receipt.sha256, "source_digest": basis.source_digest}
        return json.loads(canonical(body))
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise PlantkgError(ErrorCode.PRECONDITION, "执行依据与规划材料不一致") from exc


def snapshot_record(basis_body, plan, approval):
    body = dict(basis_body, plan_revision=plan.revision,
                approval=approval.model_dump(mode="json"), approval_sha256=digest(approval.model_dump(mode="json")))
    return canonical(body), digest(body)
