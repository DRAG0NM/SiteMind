"""Synthetic, trusted-provider fixtures; not formal provenance evidence."""
from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from plantkg.core.contracts import Action, ErrorCode, Interval, PlantkgError, Ref
from plantkg.core.versions import atomic
from plantkg.decision.gate.execution_basis import ExecutionBasis, validate_basis
from plantkg.decision.planner.receipt import SolveReceipt, SolveReceiptStore
from plantkg.world.replay.clock import ReplayClock
from tests.gate.test_gate_action_integration import env  # noqa: F401
from tests.gate.test_policy import complete_materials
from tests.versions.conftest import PLANT, REVIEWER


def _pack_inputs(config, manifest_id):
    pv, storage = config["pv"], config["storage"]
    ops = config["operations"]["items"]
    return {"pv": {"config": {"entity_id": pv["entity_id"], "kwp": pv["capacity_kw"], "config_ref": pv["ref"]}},
            "storage": {"config": dict({k: v for k, v in storage.items() if k not in ("ref", "reserve_ratio")}, config_ref=storage["ref"]),
                        "parameters": {manifest_id: {"reserve_ratio": storage["reserve_ratio"]}}},
            "tariff": {}, "production": {"operations": [dict(operation_id=o["operation_id"], entity_id=o["entity_id"],
                       original_start=o["original_start"], rated_kw=1, shape={"fractions": o["profile_kw"]},
                       refs=[{"kind": "config", "id": "config://synthetic/operations"}]) for o in ops]}}


def _setup(env, automatic=False):
    service, actions, ctx, plan, _, writer, supplied = env
    if automatic:
        _, complete = complete_materials()
        forecasts = tuple(f.model_copy(update={"lower": (9.0,) * len(f.values), "upper": (11.0,) * len(f.values)})
                          for f in supplied[0].forecasts)
        supplied[0] = replace(supplied[0], forecasts=forecasts, calibrations=complete.calibrations)
    refs = frozenset(("config", f"config://synthetic/{name}") for name in ("pv", "storage", "export", "tariff"))
    supplied[0] = replace(supplied[0], verified_refs=refs)
    materials = supplied[0]
    config = {"pv": {"entity_id": f"entity://{PLANT}/pv/1", "capacity_kw": 500, "ref": "config://synthetic/pv"},
              "storage": {"entity_id": f"entity://{PLANT}/battery/B1", "capacity_kwh": 100, "charge_kw": 20, "discharge_kw": 20, "eta_c": .9, "eta_d": .9,
                          "soc_min": .1, "soc_max": .9, "reserve_ratio": 0, "ref": "config://synthetic/storage"},
              "export": {"limit_kw": 0, "ref": "config://synthetic/export"},
              "tariff": {"unit": "CNY/kWh", "ref": "config://synthetic/tariff",
                         "slots": [{"start": plan.valid_interval.start.isoformat(),
                                    "end": plan.valid_interval.end.isoformat(), "price_cny_per_kwh": 1}]},
              "operations": {"complete": True, "items": []}}
    receipt = SolveReceipt.create({"request": materials.request.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"), "rules_by_pack": {}, "pack_evidence": _pack_inputs(config, plan.version_manifest_id),
        "snapshot": {"run_id": plan.run_id, "snapshot_id": plan.snapshot_id,
                     "version_manifest_id": plan.version_manifest_id, "as_of": ctx.as_of.isoformat(),
                     "pack_states": {"storage": {"energy_kwh": 50}}},
        "forecasts": {"forecasts": [f.model_dump(mode="json") for f in materials.forecasts]},
        "contributions": {name: {} for name in materials.request.packs},
        "export_limit_kw": 0, "export_ref": "config://synthetic/export"})
    SolveReceiptStore(writer, service.runs, service.versions, verifier=lambda *_: True).save(ctx, receipt)
    service.execution_basis_provider = lambda _plan, _materials: ExecutionBasis(receipt, config, "a" * 64)
    clock = ReplayClock(writer, service.runs)
    clock.initialize(ctx)
    return service, actions, ctx, plan, writer, config


def _snapshot(service, ctx, plan, revision, writer):
    return atomic(writer, lambda con: service.require_execution_snapshot_in_transaction(
        con, plan.plan_id, revision, ctx.run_id, ctx.version_manifest_id, plan.valid_interval.start))


def test_auto_snapshot_is_bound_to_approval_and_rejects_tampering(env):
    service, _, ctx, plan, writer, _ = _setup(env, automatic=True)
    service.assess(plan.plan_id, 0, ctx)
    approved = service.executable(plan.plan_id)
    body, checksum = _snapshot(service, ctx, plan, approved.revision, writer)
    assert body["plan_revision"] == approved.revision and body["approval"]["mode"] == "auto"
    assert len(checksum) == 64
    writer.submit(lambda con: con.execute("UPDATE b15_execution_snapshots SET record='{}' WHERE plant_id=?", (PLANT,)))
    with pytest.raises(PlantkgError):
        _snapshot(service, ctx, plan, approved.revision, writer)


def test_manual_snapshot_is_frozen_only_after_confirm(env):
    service, actions, ctx, plan, writer, _ = _setup(env)
    action = service.propose(plan.plan_id, 0, "synthetic review", ctx)
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM b15_execution_snapshots").fetchone()[0] == 0
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "snapshot-test")
    approved = service.executable(plan.plan_id)
    body, _ = _snapshot(service, ctx, plan, approved.revision, writer)
    assert body["approval"]["action_id"] == action.action_id


def test_preview_inputs_only_returns_fixed_approved_plan_and_config(env):
    service, actions, ctx, plan, _, _ = _setup(env)
    action = service.propose(plan.plan_id, 0, "synthetic review", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "preview-inputs")
    service.executable(plan.plan_id)

    found = service.preview_inputs(ctx)

    assert len(found) == 1
    assert found[0]["plan"].plan_id == plan.plan_id
    assert found[0]["config"]["tariff"]["slots"][0]["price_cny_per_kwh"] == 1


def test_changed_basis_between_proposal_and_confirmation_is_rejected(env):
    service, actions, ctx, plan, _, config = _setup(env)
    action = service.propose(plan.plan_id, 0, "synthetic review", ctx)
    config["export"]["limit_kw"] = 5
    with pytest.raises(PlantkgError, match="执行依据"):
        actions.confirm(action.action_id, REVIEWER, "accept", 0, "snapshot-changed")
    assert actions.get(action.action_id).status == "proposed"


def test_missing_snapshot_from_legacy_approval_is_not_executable(env):
    service, actions, ctx, plan, _, writer, _ = env
    ReplayClock(writer, service.runs).initialize(ctx)
    action = service.propose(plan.plan_id, 0, "legacy", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "legacy-confirm")
    approved = service.executable(plan.plan_id)
    with pytest.raises(PlantkgError, match="缺少执行依据快照"):
        _snapshot(service, ctx, plan, approved.revision, writer)


def test_snapshot_failure_rolls_back_automatic_approval(env):
    service, _, ctx, plan, writer, _ = _setup(env, automatic=True)
    writer.submit(lambda con: con.execute("CREATE TRIGGER fail_snapshot BEFORE INSERT ON b15_execution_snapshots BEGIN SELECT RAISE(ABORT, 'injected'); END"))
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        service.assess(plan.plan_id, 0, ctx)
    assert service.repository.load(plan.plan_id).revision == 0
    assert service.repository.approval(plan.plan_id) is None
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM b15_execution_snapshots").fetchone()[0] == 0


def test_execution_rejects_corrupt_persisted_solve_receipt(env):
    service, _, ctx, plan, writer, _ = _setup(env, automatic=True)
    service.assess(plan.plan_id, 0, ctx)
    approved = service.executable(plan.plan_id)
    writer.submit(lambda con: con.execute("UPDATE b09_solve_receipts SET record='{}' WHERE plant_id=? AND plan_id=?",
                                          (PLANT, plan.plan_id)))
    with pytest.raises(PlantkgError, match="快照损坏"):
        _snapshot(service, ctx, plan, approved.revision, writer)


def test_operation_requires_verified_configuration_or_asset_provenance(env):
    service, _, ctx, plan, _, config = _setup(env, automatic=True)
    materials = env[-1][0]
    operation = {"operation_id": "op1", "entity_id": f"entity://{PLANT}/operation/1",
                 "profile_kw": [2.0], "original_start": plan.valid_interval.start.isoformat()}
    config["operations"]["items"] = [operation]
    original = service.execution_basis_provider(plan, materials).receipt.read()
    original["pack_evidence"] = _pack_inputs(config, plan.version_manifest_id)
    source = original["pack_evidence"]["production"]["operations"][0]
    source["refs"] = [{"kind": "rule", "id": "rule://synthetic/operation"}]
    basis = ExecutionBasis(SolveReceipt.create(original), config, "a" * 64)
    with pytest.raises(PlantkgError, match="执行依据"):
        validate_basis(plan, materials, basis)
    source["refs"] = [{"kind": "config", "id": "config://synthetic/operations"}]
    basis = ExecutionBasis(SolveReceipt.create(original), config, "a" * 64)
    with pytest.raises(PlantkgError, match="执行依据"):
        validate_basis(plan, materials, basis)
    verified = replace(materials, verified_refs=materials.verified_refs | {("config", "config://synthetic/operations")})
    assert validate_basis(plan, verified, basis)["config"]["operations"]["items"] == [operation]


def test_legacy_receipt_without_run_mode_still_confirms(env):
    """B46 给 Context 加了 run_mode 默认值；升级前保存的回执 request.context 没有它，仍要能确认与执行。"""
    service, _, ctx, plan, writer, config = _setup(env, automatic=True)
    materials = env[-1][0]
    legacy = service.execution_basis_provider(plan, materials).receipt.read()
    legacy["request"]["context"].pop("run_mode", None)  # 旧格式；live 上下文现在也不写出 run_mode
    basis = ExecutionBasis(SolveReceipt.create(legacy), config, "a" * 64)
    assert validate_basis(plan, materials, basis)
    # 模拟升级前落库的回执：库里存的就是不带 run_mode 的原文与摘要
    writer.submit(lambda con: con.execute("UPDATE b09_solve_receipts SET sha256=?, record=? WHERE plant_id=? AND plan_id=?",
                                          (basis.receipt.sha256, basis.receipt.canonical_json, PLANT, plan.plan_id)))
    service.execution_basis_provider = lambda _plan, _materials: basis
    service.assess(plan.plan_id, 0, ctx)
    approved = service.executable(plan.plan_id)
    assert _snapshot(service, ctx, plan, approved.revision, writer)
    legacy["request"]["snapshot_id"] = "other-snapshot"  # 规范化不放过真正的差异
    with pytest.raises(PlantkgError, match="执行依据"):
        validate_basis(plan, materials, ExecutionBasis(SolveReceipt.create(legacy), config, "a" * 64))


def test_export_action_is_refused_when_limit_is_zero():
    """本期不外送：门控在批准前就拒绝带外送动作的计划，不等到执行器再做实体核验。"""
    plan, materials = complete_materials()
    interval = Interval(start=plan.valid_interval.start, end=plan.valid_interval.start + timedelta(minutes=15))
    export = Action(action_id="export-0", object=f"entity://{PLANT}/grid/main", quantity="grid_export", unit="kW",
                    value=5.0, interval=interval, refs=(Ref(kind="config", id="config://synthetic/export"),))
    plan = plan.model_copy(update={"actions": (export,)})
    basis = ExecutionBasis(None, {"export": {"limit_kw": 0, "ref": "config://synthetic/export"}}, "a" * 64)
    with pytest.raises(PlantkgError) as refused:
        validate_basis(plan, materials, basis)
    assert refused.value.code == ErrorCode.DOMAIN_INVALID
