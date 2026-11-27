"""Actual B09/B10 solve -> stored receipt -> B04/B15 approval -> B16.

Inputs and source verification are explicitly synthetic/test_only.
"""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from plantkg.adapters import HipeAdapter, PvgisAdapter
from plantkg.core.contracts import ErrorCode, ForecastBundle, Interval, PlantkgError, SourceBundle, SourceRecord, StateSnapshot
from plantkg.core.registry import Registry
from plantkg.core.versions import ManifestReferences
from plantkg.decision.gate.execution_basis import StoredExecutionBasisProvider
from plantkg.decision.gate.policy import CostEvidence, RuleRequirement
from plantkg.decision.planner import SharedPlanner
from plantkg.decision.planner.receipt import SolveReceiptStore
from plantkg.packs.pv import PvConfig, PvPack
from plantkg.storage.integration_artifacts import IntegrationArtifacts
from plantkg.storage.series import ParquetSeriesStore
from plantkg.world.record.executor import RecordExecutor
from plantkg.world.replay.providers import install
from plantkg.world.replay.series import online_record_hash
from tests.gate.test_gate_action_integration import env  # noqa: F401
from tests.gate.test_policy import complete_materials
from tests.adapters.test_adapters import hipe_csv, hipe_map, pv_file, pv_map
from tests.packs.pv.test_pv import FakeTariff, cap_rule
from tests.versions.conftest import PLANT, REVIEWER, component as make_component

__all__ = ["env"]


@pytest.mark.parametrize("mode", ["online_observed", "offline_scenario"])
@pytest.mark.parametrize("approval_mode", ["manual", "auto"])
def test_real_b09_b10_receipt_approval_and_executor_cap_comparison(request, tmp_path, mode, approval_mode):
    gate, actions, old_ctx, original, _, writer, supplied = request.getfixturevalue("env")
    refs = ManifestReferences()
    refs.bind(gate.versions)
    series = ParquetSeriesStore(tmp_path / "cap-series", writer, PLANT, refs.series)
    artifacts = IntegrationArtifacts(writer, series, PLANT, tmp_path / "cap-artifacts")
    refs.bind_source_maps(artifacts)
    start = original.valid_interval.start
    slot = Interval(start=start, end=start + timedelta(minutes=15))
    if mode == "online_observed":
        records = tuple(SourceRecord(source_id=name, source_version="test-v1", content_hash="sha256:" + char * 64,
            locator={"line_start": 1}, nature="measured", event_time=start, interval=slot, available_at=slot.end,
            source_tz="UTC", source_unit="kW", quantity=name, unit="kW", value=value)
            for name, value, char in (("load", 10, "d"), ("pv", 9, "e")))
        bundles = tuple(SourceBundle(bundle_id=r.source_id, source_id=r.source_id, source_version=r.source_version,
                                     mapping_version="test", records=(r,)) for r in records)
    else:
        source_start = datetime(2017, 10, 23, tzinfo=ZoneInfo("Europe/Berlin"))
        rows = [[(source_start + timedelta(seconds=5*i)).isoformat(), "main", 2] for i in range(181)]
        hipe = HipeAdapter().normalize(hipe_csv(tmp_path, rows), hipe_map(), {})
        pvgis = PvgisAdapter().normalize(pv_file(tmp_path), pv_map(), {})
        records = (next(r for r in hipe.records if r.nature == "configured"), pvgis.records[0])
        bundles = (hipe, pvgis)
        assert records[0].interval == records[1].interval == slot
        assert records[0].value == pytest.approx(50) and records[1].value == pytest.approx(24.105)
        assert records[1].quality.flag == "estimated" and records[1].available_at > slot.end
    entries = []
    for record, bundle in zip(records, bundles):
        ref = series.write_partition(bundle)
        entries.append(dict(quantity=record.quantity, ref=ref, source_id=record.source_id,
                            source_version=record.source_version, basis="synthetic/test_only"))
    component = artifacts.prepare_series_map(entries)
    gate.versions.register_verifier("series-map:v1:", artifacts.verify_map)
    manifest = gate.versions.prepare({"data_sources": component}, "synthetic")
    gate.versions.check_integrity(manifest.version_manifest_id)
    gate.versions.activate(old_ctx.version_manifest_id, manifest.version_manifest_id, REVIEWER, "test")
    clock, query = install(Registry(), writer, gate.runs, series)
    inputs = {"online_source_ids": [r.source_id for r in records],
              "online_record_hashes": [online_record_hash(r) for r in records] if mode == "online_observed" else [],
              "execution_input_mode": mode, "execution_quantities": {"load": records[0].quantity, "pv": records[1].quantity}}
    if mode == "offline_scenario":
        inputs |= {"execution_truth_source_ids": [r.source_id for r in records],
                   "execution_truth_record_hashes": [online_record_hash(r) for r in records],
                   "execution_truth_as_of": max(r.available_at for r in records).isoformat()}
    output, plan_actions = [], []
    for cap in (8.0, 10.0):
        run = gate.runs.create_run(old_ctx.model_copy(update={"version_manifest_id": manifest.version_manifest_id}),
                                   "replay_day", original.valid_interval, inputs=inputs)
        ctx = old_ctx.model_copy(update={"run_id": run.run_id, "version_manifest_id": manifest.version_manifest_id})
        if mode == "offline_scenario":
            ctx = ctx.model_copy(update={"channel": "offline", "agent_run_id": None})
        rule = cap_rule(cap, effective_from=start - timedelta(days=1), available_at=ctx.as_of)
        base = supplied[0]
        planning = base.request.model_copy(update={"context": ctx, "rule_ids": (rule.rule_id,), "packs": ("pv", "tariff")})
        forecasts = tuple(f.model_copy(update={"values": (5.0 if f.quantity == "pv_available" else 10.0,) * 96,
                          "params": f.params | {"run_id": run.run_id, "version_manifest_id": manifest.version_manifest_id}})
                          for f in base.forecasts)
        if approval_mode == "auto":
            forecasts = tuple(f.model_copy(update={"lower": tuple(v - 1 for v in f.values),
                                                   "upper": tuple(v + 1 for v in f.values)}) for f in forecasts)
        state = StateSnapshot(run_id=run.run_id, snapshot_id=planning.snapshot_id, event_seq=0,
                              as_of=ctx.as_of, version_manifest_id=manifest.version_manifest_id, pack_states={})
        pv = PvPack(PvConfig(rule.subject_ids[0], 500, rule.config_ref))
        planner = SharedPlanner({"pv": pv, "tariff": FakeTariff([1.0] * 96)}, lambda _: state,
                                lambda _: ForecastBundle(forecasts=forecasts), lambda _: (rule,),
                                clock=lambda: ctx.as_of, export_ref="config://synthetic/export")
        solved, receipt = planner.solve_with_receipt(planning)
        assert solved.solver_status == "optimal"
        plan = solved.plan
        plan_actions.append(plan.actions)
        cost = CostEvidence(solved.objective_value, "test://synthetic", "test://fixed")
        baseline = replace(base.baseline, plan=plan.model_copy(update={"plan_id": "baseline", "policy": "B0"}), cost=cost)
        verified = frozenset(("config", ref) for ref in (rule.config_ref, "config://synthetic/export", "config://synthetic/tariff"))
        supplied[0] = replace(base, request=planning, forecasts=forecasts, baseline=baseline, plan_cost=cost, rules=(rule,),
            rule_requirements=(RuleRequirement(rule.rule_id, plan.valid_interval),), verified_refs=verified,
            calibrations=complete_materials()[1].calibrations if approval_mode == "auto" else ())
        config = {"pv": {"entity_id": rule.subject_ids[0], "capacity_kw": 500, "ref": rule.config_ref}, "storage": None,
            "export": {"limit_kw": 0.0, "ref": "config://synthetic/export"},
            "tariff": {"unit": "CNY/kWh", "ref": "config://synthetic/tariff", "slots": [
                {"start": start.isoformat(), "end": plan.valid_interval.end.isoformat(), "price_cny_per_kwh": 1.0}]},
            "operations": {"complete": True, "items": []}}
        store = SolveReceiptStore(writer, gate.runs, gate.versions, verifier=lambda *_: True)
        store.save(ctx, receipt)
        gate.execution_basis_provider = StoredExecutionBasisProvider(store, lambda *_: (config, "f" * 64), lambda *_: True)
        gate.register(plan, ctx)
        if approval_mode == "manual":
            action = gate.propose(plan.plan_id, 0, "synthetic", ctx)
            actions.confirm(action.action_id, REVIEWER, "accept", 0, f"synthetic-confirm-{cap}")
        else:
            assert gate.assess(plan.plan_id, 0, ctx).outcome == "auto_confirmed"
        approved = gate.executable(plan.plan_id)
        if cap == 8.0:
            newer = gate.versions.prepare({"data_sources": component, "parameters": make_component("t:later")}, "synthetic replacement")
            gate.versions.check_integrity(newer.version_manifest_id)
            gate.versions.activate(manifest.version_manifest_id, newer.version_manifest_id, REVIEWER, "switch")
        executor = RecordExecutor(writer, clock, gate, query, gate.runs)
        if mode == "offline_scenario":
            online_ctx = ctx.model_copy(update={"channel": "gateway", "agent_run_id": "agent-test"})
            with pytest.raises(PlantkgError) as denied:
                executor.execute(approved, online_ctx, 0, f"forbidden-{cap}")
            assert denied.value.code == ErrorCode.FORBIDDEN
        result = executor.execute(approved, ctx, 0, f"synthetic-execute-{cap}")
        assert result.ledger.balanced and result.ledger.entries[0].pv_curtailed_kwh == 0
        assert result.forecast_error["base_load"] == pytest.approx(records[0].value - 10.0)
        assert result.forecast_error["pv_available"] == pytest.approx(records[1].value - 5.0)
        limits = [a.executed_value for a in result.executed if a.action_id.startswith("actual-pv-limit")]
        output.append(limits[0] if limits else 0.0)
        if mode == "offline_scenario":
            with pytest.raises(PlantkgError) as hidden:
                executor.read_execution(ctx.model_copy(update={"as_of": slot.end}), 1)
            assert hidden.value.code == ErrorCode.NOT_VISIBLE
            assert executor.read_execution_offline(ctx, 1) == result
            with pytest.raises(PlantkgError) as no_gateway:
                executor.read_execution_offline(online_ctx, 1)
            assert no_gateway.value.code == ErrorCode.FORBIDDEN
            assert query.query(ctx, records[1].quantity, slot).records == ()
            clock.advance(ctx, 1, records[1].available_at)
            visible = ctx.model_copy(update={"as_of": records[1].available_at})
            assert executor.read_execution(visible, 1) == result
            assert query.query(visible, records[1].quantity, slot).records == ()
    assert plan_actions[0] == plan_actions[1]
    assert output == pytest.approx([max(records[1].value - cap, 0) for cap in (8.0, 10.0)])
