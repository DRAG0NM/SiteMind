"""96-slot real B05/B03/B04/B07/B14/B09 -> B15 chain.

Tariff, source data, cost provenance, and dependency verification are constructed
for this test. Forecast generation/loading, run, solver, SQLite gate, HTTP action
confirmation and executable-plan checks use the real implementations.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from plantkg.adapters import HipeAdapter, PvgisAdapter
from plantkg.adapters.baseload import selected_device_baseload
from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.actions import ACTOR_HEADER, TOKEN_HEADER
from plantkg.core.config import load_settings
from plantkg.core.contracts import Interval, PlantkgError
from plantkg.core.ports import StateProvider
from plantkg.decision.forecasting import ForecastService
from plantkg.decision.gate.b14 import load_b14_forecasts
from plantkg.decision.gate.policy import CostEvidence, PolicyMaterials, RuleRequirement
from plantkg.decision.planner import SharedPlanner
from plantkg.packs.pv import PvConfig, PvPack
from plantkg.testing.fakes import fixture
from plantkg.world.replay.input_assembly import prepare_online_inputs
from tests.adapters.test_adapters import hipe_csv, hipe_map, pv_file, pv_map
from plantkg.packs.tariff import acceptance_blockers, load_tariff_config, load_tariff_rules
from tests.versions.conftest import REVIEWER, context


def test_96_slot_real_planner_gate_and_http_confirmation(tmp_path):
    cfg = load_settings(profile="dev", overrides={"storage.enabled": True,
        "storage.sqlite": str(tmp_path / "catalog.db"), "storage.graph": str(tmp_path / "graph"),
        "storage.series": str(tmp_path / "series"), "actions.reviewers": ["u-reviewer"], "actions.ttl_s": 900})
    services = build_services(cfg)
    app_owns_shutdown = False
    try:
        start = datetime(2017, 10, 23, tzinfo=ZoneInfo("Europe/Berlin"))
        def meter_source(name, value):
            rows = [[(start + timedelta(minutes=i)).isoformat(), name, value] for i in range(3 * 24 * 60 + 1)]
            return HipeAdapter().normalize(hipe_csv(tmp_path, rows), hipe_map(plant_start="2026-06-30", days=3), {})
        main, machine = meter_source("main", 2), meter_source("machine", .4)
        path = pv_file(tmp_path)
        response = json.loads(path.read_text(encoding="utf-8"))
        pv_start = datetime(2017, 6, 29, 16, 30, tzinfo=timezone.utc)
        response["outputs"]["hourly"] = [{"time": (pv_start + timedelta(hours=i)).strftime("%Y%m%d:%H%M"),
                                           "P": 48.21} for i in range(72)]
        path.write_text(json.dumps(response), encoding="utf-8")
        pv = PvgisAdapter().normalize(path, pv_map(source_start="2017-06-30", plant_start="2026-06-30", days=3), {})
        cutoff = datetime(2026, 7, 1, 18, tzinfo=timezone.utc)
        horizon = Interval(start=datetime(2026, 7, 2, tzinfo=timezone.utc),
                           end=datetime(2026, 7, 3, tzinfo=timezone.utc))
        owner, art = services.version_run, services.version_run.integration_artifacts
        approved = lambda b: [r for r in b.records if r.available_at <= cutoff and r.interval.end <= cutoff]
        source, inputs, _ = prepare_online_inputs(services.storage.series, art, main, [machine], ["machine"], pv,
            selected_device_baseload, approved_baseline=approved, approved_pv=approved, as_of=cutoff,
            basis="constructed full-day fixture")
        params = art.prepare_parameters(pv_capacity_kw=500, model_version="persistence-1",
                                        parameter_version="v1", configuration_ref="constructed-cfg")
        manifest = owner.versions.prepare({"data_sources": source, "parameters": params}, "constructed day")
        owner.versions.check_integrity(manifest.version_manifest_id)
        owner.versions.activate(None, manifest.version_manifest_id, REVIEWER, "constructed day")
        ctx = context(plant_id=cfg.plant_id, as_of=cutoff, version_manifest_id=manifest.version_manifest_id)
        run = owner.runs.create_run(ctx, "replay_day", Interval(start=cutoff, end=horizon.end), inputs=inputs)
        ctx = ctx.model_copy(update={"run_id": run.run_id})
        forecast_service = ForecastService.from_services(services)
        key, forecast = forecast_service.predict_and_save(ctx, horizon)
        assert all(f.coverage == 1 and len(f.values) == 96 for f in forecast.forecasts)
        assert forecast.uncertainty_summary["interval_calibrated"] == 0.0
        snap = services.registry.resolve(StateProvider).at(ctx, cutoff)
        pv_config = "config://test#pv"
        pack = PvPack(PvConfig(entity_id=f"entity://{cfg.plant_id}/pv/PV1", kwp=500, config_ref=pv_config))
        tariff_dir = Path(__file__).resolve().parents[2] / "fixtures" / "packs" / "tariff"
        tariff, demo_base = load_tariff_config(tariff_dir / "tariff.yaml", "fixtures/packs/tariff/tariff.yaml")
        tariff_rules = load_tariff_rules(tariff_dir / "demo_tou.yaml", "fixtures/packs/tariff/demo_tou.yaml")
        rules = tuple(rule for rule in tariff_rules if rule.effective_from <= horizon.start and
                      (rule.effective_to is None or rule.effective_to >= horizon.end))
        assert len(rules) == 1 and demo_base and acceptance_blockers(rules, demo_base)
        req = fixture("planning_request").model_copy(update={"context": ctx, "snapshot_id": snap.snapshot_id,
            "forecast_ids": tuple(f.forecast_id for f in forecast.forecasts), "rule_ids": tuple(r.rule_id for r in rules),
            "horizon": horizon, "packs": ("pv", "tariff")})
        # Actual B12 loader, compiler and pack using the repository's demo tariff.
        planner = SharedPlanner({"pv": pack, "tariff": tariff}, lambda _: snap,
                                forecast_service.for_planner(ctx, key), lambda _: rules)
        result = planner.solve(req)
        assert result.solver_status == "optimal", result.validation_errors
        plan = result.plan
        assert result.objective_terms["tariff.energy_cost"] > 0

        # Deliberately test-only: the test supplies the original request, the
        # fixture config assertion and explicit electricity cost; B14 forecasts
        # are reloaded through the actual service at every evaluation.
        def constructed_provider(current, metadata):
            assert metadata["forecast_key"] == key
            return PolicyMaterials(request=req, forecasts=load_b14_forecasts(forecast_service, ctx, current, key),
                rules=rules, dependencies_verified=True, rule_conflicts=(),
                verified_refs=frozenset({("config", pv_config), ("config", tariff.config_ref),
                                         *(('config', r.config_ref) for r in rules)}),
                rule_requirements=tuple(RuleRequirement(r.rule_id, horizon) for r in rules),
                plan_cost=CostEvidence(result.objective_terms["tariff.energy_cost"],
                                       "test://constructed-tariff-electricity", "test://constructed-inputs"))
        gate = services.gate
        gate.register(plan, ctx, {"forecast_key": key, "planning_request": req.model_dump(mode="json")})
        decision = gate.assess(plan.plan_id, 0, ctx)
        assert decision.outcome == "review_required" and decision.revision == 1
        assert any("校准" in reason for reason in decision.reasons)
        assert any("依赖" in reason for reason in decision.reasons)
        assert any("CNY" in reason for reason in decision.reasons)
        assert gate.repository.approval(plan.plan_id) is None
        action = owner.actions.get(decision.pending_action_id)
        assert action.target_ref == f"{plan.plan_id}@1" and action.revision == 0
        assert action.proposed_at == cutoff and action.expires_at == cutoff + timedelta(minutes=15)
        # A second run has its own persisted cursor and action TTL.
        other_run = owner.runs.create_run(ctx, "replay_day", Interval(start=cutoff, end=horizon.end), inputs=inputs)
        other_ctx = ctx.model_copy(update={"run_id": other_run.run_id, "idempotency_key": "other-run-clock"})
        other_action = owner.actions.propose("plan_confirmation", {"target_ref": "other@1", "target_revision": 1}, other_ctx)
        replay_clock = services.registry.resolve(StateProvider)
        replay_clock.advance(other_ctx, 0, cutoff + timedelta(minutes=15))
        assert owner.actions.get(other_action.action_id).status == "expired"
        assert owner.actions.get(action.action_id).status == "proposed"
        with pytest.raises(PlantkgError):
            gate.executable(plan.plan_id)
        with TestClient(create_app(services)) as client:
            app_owns_shutdown = True
            headers = {TOKEN_HEADER: owner.operator.token, ACTOR_HEADER: "u-reviewer"}
            body = {"reason": "accept uncalibrated interval and missing baseline", "expected_revision": 0,
                    "idempotency_key": "constructed-96-slot-confirm"}
            refused = client.post(f"/api/v1/actions/{action.action_id}/confirm", json=body, headers=headers)
            assert refused.status_code == 422
            assert gate.repository.approval(plan.plan_id) is None
            # This provider is only for the constructed test; the default provider
            # correctly refuses approval because provenance is not available.
            gate.materials_provider = constructed_provider
            reply = client.post(f"/api/v1/actions/{action.action_id}/confirm", json=body, headers=headers)
            assert reply.status_code == 200, reply.text
            assert reply.json()["status"] == "confirmed"
            executable = gate.executable(plan.plan_id)
            assert executable.revision == 2 and executable.status == "confirmed"
            approval = gate.repository.approval(plan.plan_id, 2)
            assert approval.mode == "manual" and approval.actor == "u-reviewer" and approval.action_id == action.action_id
            updated = art.prepare_parameters(pv_capacity_kw=500, model_version="persistence-1",
                                             parameter_version="v2", configuration_ref="constructed-cfg-v2")
            new_manifest = owner.versions.prepare({"data_sources": source, "parameters": updated}, "next run")
            owner.versions.check_integrity(new_manifest.version_manifest_id)
            owner.versions.activate(manifest.version_manifest_id, new_manifest.version_manifest_id, REVIEWER, "next run")
            assert gate.executable(plan.plan_id, 2).version_manifest_id == manifest.version_manifest_id
        restarted = build_services(cfg)
        try:
            assert restarted.gate.executable(plan.plan_id, 2).status == "confirmed"
            assert restarted.version_run.actions.get(action.action_id).status == "confirmed"
            restarted.registry.resolve(StateProvider).advance(ctx, 0, horizon.end)
            with pytest.raises(PlantkgError, match="过期"):
                restarted.gate.executable(plan.plan_id, 2)
        finally:
            restarted.close()
    finally:
        if not app_owns_shutdown:
            services.close()
