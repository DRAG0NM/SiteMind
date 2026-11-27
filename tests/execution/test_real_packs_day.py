"""96-slot B09/B10/B11/B13/B15/B16 integration using synthetic sources."""
from dataclasses import replace
from datetime import timedelta

import pytest

from plantkg.core.contracts import ForecastBundle, Interval, SourceBundle, SourceRecord, StateSnapshot
from plantkg.core.registry import Registry
from plantkg.core.versions import ManifestReferences
from plantkg.decision.gate.execution_basis import StoredExecutionBasisProvider
from plantkg.decision.gate.policy import CostEvidence, RuleRequirement
from plantkg.decision.planner import SharedPlanner
from plantkg.decision.planner.receipt import SolveReceiptStore
from plantkg.packs.production import ProductionPack
from plantkg.packs.pv import PvConfig, PvPack
from plantkg.packs.storage import StoragePack
from plantkg.storage.integration_artifacts import IntegrationArtifacts
from plantkg.storage.series import ParquetSeriesStore
from plantkg.world.record.executor import RecordExecutor
from plantkg.world.replay.providers import install
from plantkg.world.replay.series import online_record_hash
from tests.gate.test_gate_action_integration import env  # noqa: F401
from tests.packs.production.test_production import SPEC, window_rule
from tests.packs.pv.test_pv import FakeTariff, cap_rule
from tests.packs.storage.test_storage import CONFIG as STORAGE
from tests.planner.test_planner import prices
from tests.versions.conftest import PLANT, REVIEWER

__all__ = ["env"]


def test_real_domain_packs_solve_approve_and_execute_full_day(request, tmp_path):
    gate, actions, old_ctx, original, _, writer, supplied = request.getfixturevalue("env")
    refs = ManifestReferences()
    refs.bind(gate.versions)
    series = ParquetSeriesStore(tmp_path / "packs-series", writer, PLANT, refs.series)
    artifacts = IntegrationArtifacts(writer, series, PLANT, tmp_path / "packs-artifacts")
    refs.bind_source_maps(artifacts)
    start = original.valid_interval.start
    original_index = int((SPEC.original_start - start) / timedelta(minutes=15))
    load = [200.0] * 96
    for k, value in enumerate(SPEC.profile_kw):
        load[original_index + k] += value
    records = tuple(SourceRecord(source_id=name, source_version="test-v1", content_hash="sha256:" + char * 64,
        locator={"line_start": i + 1}, nature="measured", event_time=start + timedelta(minutes=15*i),
        interval=Interval(start=start + timedelta(minutes=15*i), end=start + timedelta(minutes=15*(i+1))),
        available_at=start + timedelta(minutes=15*(i+1)), source_tz="UTC", source_unit="kW", quantity=name, unit="kW",
        value=load[i] if name == "load" else 9.0 if i == 0 else 0.0)
        for name, char in (("load", "a"), ("pv", "b")) for i in range(96))
    entries = []
    for record in (records[0], records[96]):
        bundle = SourceBundle(bundle_id=record.source_id, source_id=record.source_id, source_version=record.source_version,
                              mapping_version="test", records=tuple(r for r in records if r.source_id == record.source_id))
        ref = series.write_partition(bundle)
        entries.append(dict(quantity=record.quantity, ref=ref, source_id=record.source_id,
                            source_version=record.source_version, basis="synthetic/test_only"))
    component = artifacts.prepare_series_map(entries)
    gate.versions.register_verifier("series-map:v1:", artifacts.verify_map)
    manifest = gate.versions.prepare({"data_sources": component}, "synthetic")
    gate.versions.check_integrity(manifest.version_manifest_id)
    gate.versions.activate(old_ctx.version_manifest_id, manifest.version_manifest_id, REVIEWER, "test")
    clock, query = install(Registry(), writer, gate.runs, series)
    run = gate.runs.create_run(old_ctx.model_copy(update={"version_manifest_id": manifest.version_manifest_id}),
        "replay_day", original.valid_interval, inputs={"online_source_ids": ["load", "pv"],
        "online_record_hashes": [online_record_hash(r) for r in records], "execution_input_mode": "online_observed",
        "execution_quantities": {"load": "load", "pv": "pv"}})
    ctx = old_ctx.model_copy(update={"run_id": run.run_id, "version_manifest_id": manifest.version_manifest_id})
    cap = cap_rule(8.0, effective_from=start - timedelta(days=1), available_at=ctx.as_of)
    window = window_rule(effective_from=start - timedelta(days=1), available_at=ctx.as_of)
    base = supplied[0]
    planning = base.request.model_copy(update={"context": ctx, "rule_ids": (cap.rule_id, window.rule_id),
                                               "packs": ("pv", "storage", "tariff", "production")})
    forecasts = tuple(f.model_copy(update={"values": (5.0 if f.quantity == "pv_available" else 200.0,) * 96,
                      "params": f.params | {"run_id": run.run_id, "version_manifest_id": manifest.version_manifest_id}})
                      for f in base.forecasts)
    state = StateSnapshot(run_id=run.run_id, snapshot_id=planning.snapshot_id, event_seq=0, as_of=ctx.as_of,
                          version_manifest_id=manifest.version_manifest_id, pack_states={"storage": {"energy_kwh": 500}})
    tariff_prices = prices()
    packs = {"pv": PvPack(PvConfig(cap.subject_ids[0], 500, cap.config_ref)),
             "storage": StoragePack(STORAGE, {manifest.version_manifest_id: {"reserve_ratio": 0}}, "synthetic/parameters"),
             "tariff": FakeTariff(tariff_prices), "production": ProductionPack([SPEC])}
    planner = SharedPlanner(packs, lambda _: state, lambda _: ForecastBundle(forecasts=forecasts), lambda _: (cap, window),
                            clock=lambda: ctx.as_of, export_ref="config://synthetic/export")
    solved, receipt = planner.solve_with_receipt(planning)
    assert solved.solver_status == "optimal", solved.validation_errors
    plan = solved.plan
    assert any(a.quantity == "charge_power" for a in plan.actions)
    assert any(a.quantity == "discharge_power" for a in plan.actions)
    starts = [a.interval.start for a in plan.actions if a.quantity == "operation_start"]
    assert len(starts) == 1 and starts[0] != SPEC.original_start
    verified = {(ref.kind, ref.id) for action in plan.actions for ref in action.refs if ref.kind in ("config", "asset_version")}
    verified |= {(ref.kind, ref.id) for ref in SPEC.refs if ref.kind in ("config", "asset_version")}
    verified |= {("config", value) for value in (cap.config_ref, window.config_ref, STORAGE.config_ref,
                 "config://synthetic/export", "config://synthetic/tariff")}
    cost = CostEvidence(solved.objective_value, "test://synthetic", "test://same")
    baseline = replace(base.baseline, plan=plan.model_copy(update={"plan_id": "baseline", "policy": "B0"}), cost=cost)
    supplied[0] = replace(base, request=planning, forecasts=forecasts, baseline=baseline, plan_cost=cost,
        rules=(cap, window), rule_requirements=(RuleRequirement(cap.rule_id, plan.valid_interval),
                                                RuleRequirement(window.rule_id, plan.valid_interval)),
        verified_refs=frozenset(verified))
    config = {"pv": {"entity_id": cap.subject_ids[0], "capacity_kw": 500, "ref": cap.config_ref},
        "storage": {"entity_id": STORAGE.entity_id, "capacity_kwh": STORAGE.capacity_kwh, "charge_kw": STORAGE.charge_kw,
                    "discharge_kw": STORAGE.discharge_kw, "eta_c": STORAGE.eta_c, "eta_d": STORAGE.eta_d,
                    "soc_min": STORAGE.soc_min, "soc_max": STORAGE.soc_max, "reserve_ratio": 0, "ref": STORAGE.config_ref},
        "export": {"limit_kw": 0, "ref": "config://synthetic/export"},
        "tariff": {"unit": "CNY/kWh", "ref": "config://synthetic/tariff", "slots": [
            {"start": (start + timedelta(minutes=15*i)).isoformat(), "end": (start + timedelta(minutes=15*(i+1))).isoformat(),
             "price_cny_per_kwh": tariff_prices[i]} for i in range(96)]},
        "operations": {"complete": True, "items": [{"operation_id": SPEC.operation_id, "entity_id": SPEC.entity_id,
                       "profile_kw": list(SPEC.profile_kw), "original_start": SPEC.original_start.isoformat()}]}}
    store = SolveReceiptStore(writer, gate.runs, gate.versions, verifier=lambda *_: True)
    store.save(ctx, receipt)
    gate.execution_basis_provider = StoredExecutionBasisProvider(store, lambda *_: (config, "f" * 64), lambda *_: True)
    gate.register(plan, ctx)
    action = gate.propose(plan.plan_id, 0, "synthetic", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "synthetic-real-packs")
    approved = gate.executable(plan.plan_id)
    executor = RecordExecutor(writer, clock, gate, query, gate.runs)
    results = [executor.execute(approved, ctx, i, f"real-packs-{i}") for i in range(96)]
    ledger = [item.ledger.entries[0] for item in results]
    assert all(item.ledger.balanced for item in results)
    assert sum(item.load_kwh for item in ledger) == pytest.approx(sum(load) * .25)
    assert sum(item.grid_import_kwh for item in ledger) == pytest.approx(sum(
        e.load_kwh + e.charge_kwh + e.grid_export_kwh - e.discharge_kwh - e.pv_used_kwh for e in ledger))
    assert sum(item.cost_cny for item in ledger) == pytest.approx(sum(e.grid_import_kwh * p for e, p in zip(ledger, tariff_prices)))
    assert results[-1].event_seq == 96 and ledger[-1].energy_end_kwh == pytest.approx(500, abs=.1)
