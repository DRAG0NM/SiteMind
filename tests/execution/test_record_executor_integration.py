"""Real storage/gate/clock/ledger with explicitly synthetic test-only inputs."""
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import subprocess
import sys

import pytest

from plantkg.core.contracts import Action, ErrorCode, Interval, PlantkgError, Ref, SourceBundle, SourceRecord
from plantkg.core.registry import Registry
from plantkg.core.versions import ManifestReferences, digest
from plantkg.decision.gate.execution_basis import StoredExecutionBasisProvider
from plantkg.decision.gate.policy import RuleRequirement
from plantkg.decision.planner.receipt import SolveReceipt, SolveReceiptStore
from plantkg.storage.integration_artifacts import IntegrationArtifacts
from plantkg.storage.series import ParquetSeriesStore
from plantkg.world.record.executor import RecordExecutor
from plantkg.world.replay.providers import install
from plantkg.world.replay.series import online_record_hash
from tests.gate.test_gate_action_integration import env  # noqa: F401
from tests.gate.test_execution_snapshots import _pack_inputs
from tests.packs.pv.test_pv import cap_rule
from tests.versions.conftest import PLANT, REVIEWER

__all__ = ["env"]


@pytest.mark.parametrize("cap, expected_limit", [(8.0, 1.0), (10.0, 0.0)])
@pytest.mark.parametrize("restart", [False, True])
def test_executor_commits_approved_pv_cap_and_balanced_ledger(tmp_path, request, cap, expected_limit, restart):
    env = request.getfixturevalue("env")
    gate, actions, old_ctx, draft, _, writer, supplied = env
    refs = ManifestReferences()
    refs.bind(gate.versions)
    series = ParquetSeriesStore(tmp_path / "record-series", writer, PLANT, refs.series)
    artifacts = IntegrationArtifacts(writer, series, PLANT, tmp_path / "record-artifacts")
    refs.bind_source_maps(artifacts)
    start = draft.valid_interval.start
    slot = Interval(start=start, end=start + timedelta(minutes=15))
    records = tuple(SourceRecord(source_id=name, source_version="synthetic-v1", content_hash="sha256:" + char * 64,
        locator={"line_start": i + 1}, nature="measured", event_time=start + timedelta(minutes=15*i),
        interval=Interval(start=start + timedelta(minutes=15*i), end=start + timedelta(minutes=15*(i+1))),
        available_at=start + timedelta(minutes=15*(i+1)), source_tz="UTC", source_unit="kW", quantity=name,
        unit="kW", value=(1 if name == "load" and i in (1, 3) else 10 if name == "load" else 9 if i == 0 else 20 if i == 3 else 0))
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
    gate.versions.activate(old_ctx.version_manifest_id, manifest.version_manifest_id, REVIEWER, "synthetic")
    clock, query = install(Registry(), writer, gate.runs, series)
    inputs = {"online_source_ids": [r.source_id for r in records],
              "online_record_hashes": [online_record_hash(r) for r in records],
              "execution_input_mode": "online_observed", "execution_quantities": {"load": "load", "pv": "pv"}}
    run = gate.runs.create_run(old_ctx.model_copy(update={"version_manifest_id": manifest.version_manifest_id}),
                               "replay_day", draft.valid_interval, inputs=inputs)
    ctx = old_ctx.model_copy(update={"run_id": run.run_id, "version_manifest_id": manifest.version_manifest_id})
    plan = draft.model_copy(update={"plan_id": "synthetic-executor", "run_id": run.run_id,
                                    "version_manifest_id": manifest.version_manifest_id}, deep=True)
    battery_id = f"entity://{PLANT}/battery/B1"
    operation_id = f"entity://{PLANT}/operation/Op1"
    def action(name, entity, quantity, value, index, unit="kW"):
        interval = Interval(start=start + timedelta(minutes=15*index), end=start + timedelta(minutes=15*(index+1)))
        return Action(action_id=name, object=entity, quantity=quantity, unit=unit, value=value, interval=interval,
                      refs=(Ref(kind="config", id="config://synthetic/operations" if quantity.startswith("operation")
                                else "config://synthetic/storage"),))
    plan = plan.model_copy(update={"actions": (action("discharge", battery_id, "discharge_power", 2, 1),
        action("charge", battery_id, "charge_power", 1, 2),
        action("discharge-day", battery_id, "discharge_power", 2, 3),
        action("op-start", operation_id, "operation_start", 1, 6, "count"),
        action("op-power-0", operation_id, "operation_power", 2, 6),
        action("op-power-1", operation_id, "operation_power", 4, 7))})
    rule = cap_rule(cap, effective_from=start - timedelta(days=1), available_at=ctx.as_of)
    materials = supplied[0]
    planning_request = materials.request.model_copy(update={"context": ctx, "rule_ids": (rule.rule_id,)})
    forecasts = tuple(f.model_copy(update={"params": f.params | {"run_id": run.run_id,
                       "version_manifest_id": manifest.version_manifest_id}}) for f in materials.forecasts)
    baseline = replace(materials.baseline, plan=plan.model_copy(update={"plan_id": "baseline", "policy": "B0"}))
    source_refs = frozenset(("config", ref) for ref in (
        rule.config_ref, "config://synthetic/storage", "config://synthetic/export", "config://synthetic/tariff",
        "config://synthetic/operations"))
    supplied[0] = replace(materials, request=planning_request, forecasts=forecasts, baseline=baseline, rules=(rule,),
        rule_requirements=(RuleRequirement(rule.rule_id, plan.valid_interval),), verified_refs=source_refs)
    config = {"pv": {"entity_id": rule.subject_ids[0], "capacity_kw": 500, "ref": rule.config_ref},
        "storage": {"entity_id": battery_id, "capacity_kwh": 100, "charge_kw": 20, "discharge_kw": 20, "eta_c": .9, "eta_d": .9,
                    "soc_min": .1, "soc_max": .9, "reserve_ratio": 0, "ref": "config://synthetic/storage"},
        "export": {"limit_kw": 0, "ref": "config://synthetic/export"},
        "tariff": {"unit": "CNY/kWh", "ref": "config://synthetic/tariff",
                   "slots": [{"start": start.isoformat(), "end": plan.valid_interval.end.isoformat(), "price_cny_per_kwh": 1}]},
        "operations": {"complete": True, "items": [{"operation_id": "op1", "entity_id": operation_id,
                       "profile_kw": [2, 4], "original_start": (start + timedelta(minutes=60)).isoformat()}]}}
    receipt = SolveReceipt.create({"request": planning_request.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
        "rules_by_pack": {"pv": [rule.model_dump(mode="json")]},
        "pack_evidence": _pack_inputs(config, plan.version_manifest_id),
        "contributions": {name: {} for name in planning_request.packs},
        "snapshot": {"run_id": plan.run_id, "snapshot_id": plan.snapshot_id, "version_manifest_id": plan.version_manifest_id,
                     "as_of": ctx.as_of.isoformat(), "pack_states": {"storage": {"energy_kwh": 50}}},
        "forecasts": {"forecasts": [f.model_dump(mode="json") for f in forecasts]},
        "export_limit_kw": 0, "export_ref": "config://synthetic/export"})
    receipt_store = SolveReceiptStore(writer, gate.runs, gate.versions, verifier=lambda _ctx, _receipt: True)
    receipt_store.save(ctx, receipt)
    gate.execution_basis_provider = StoredExecutionBasisProvider(receipt_store,
        lambda _ctx, _plan, _receipt: (config, "c" * 64), lambda *_args: True)
    gate.register(plan, ctx)
    action = gate.propose(plan.plan_id, 0, "synthetic", ctx)
    actions.confirm(action.action_id, REVIEWER, "accept", 0, "synthetic-executor-confirm")
    approved = gate.executable(plan.plan_id)
    executor = RecordExecutor(writer, clock, gate, query, gate.runs)
    with pytest.raises(PlantkgError):
        executor.execute(approved.model_copy(update={"revision": approved.revision + 1}), ctx, 0, "wrong-revision")
    assert clock.current(ctx)[1] == 0
    for bad_plan, bad_ctx in ((approved.model_copy(update={"actions": ()}), ctx),
                              (plan, ctx),
                              (approved, ctx.model_copy(update={"plant_id": "other"})),
                              (approved, ctx.model_copy(update={"version_manifest_id": "other"})),
                              (approved, ctx.model_copy(update={"run_id": "other"}))):
        with pytest.raises(PlantkgError):
            executor.execute(bad_plan, bad_ctx, 0, "rejected-request")
    assert clock.current(ctx)[1] == 0
    for table, when in (("b16_executions", "BEFORE"), ("b16_executions", "AFTER"), ("replay_clock_history", "AFTER")):
        writer.submit(lambda con, table=table, when=when: con.execute(
            f"CREATE TRIGGER fail_test {when} INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END"))
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            executor.execute(approved, ctx, 0, "synthetic-executor-key")
        writer.submit(lambda con: con.execute("DROP TRIGGER fail_test"))
        assert clock.current(ctx)[1] == 0
        with writer.read_only() as con:
            assert con.execute("SELECT count(*) FROM b16_executions WHERE run_id=?", (run.run_id,)).fetchone()[0] == 0
    writer.submit(lambda con: con.execute("CREATE TABLE test_parent (id INTEGER PRIMARY KEY)"))
    writer.submit(lambda con: con.execute("CREATE TABLE test_child (parent INTEGER, FOREIGN KEY(parent) REFERENCES test_parent(id) DEFERRABLE INITIALLY DEFERRED)"))
    writer.submit(lambda con: con.execute("CREATE TRIGGER fail_commit AFTER INSERT ON b16_executions BEGIN INSERT INTO test_child VALUES (123); END"))
    with pytest.raises(sqlite3.IntegrityError):
        executor.execute(approved, ctx, 0, "synthetic-executor-key")
    writer.submit(lambda con: con.execute("DROP TRIGGER fail_commit"))
    assert clock.current(ctx)[1] == 0
    with writer.read_only() as con:
        assert con.execute("SELECT count(*) FROM b16_executions WHERE run_id=?", (run.run_id,)).fetchone()[0] == 0
    def compete(key):
        try:
            return executor.execute(approved, ctx, 0, key)
        except PlantkgError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        competed = list(pool.map(compete, ("synthetic-executor-key", "synthetic-executor-other")))
    winners = [item for item in competed if not isinstance(item, ErrorCode)]
    assert len(winners) == 1 and competed.count(ErrorCode.STATE_CONFLICT) == 1
    result = winners[0]
    winner_key = result.idempotency_key
    with ThreadPoolExecutor(max_workers=2) as pool:
        retries = list(pool.map(lambda _: executor.execute(approved, ctx, 0, winner_key), range(2)))
    assert retries == [result, result]
    assert result.ledger.balanced and result.ledger.entries[0].pv_available_kwh == (9 - expected_limit) / 4
    limits = [a.executed_value for a in result.executed if a.action_id.startswith("actual-pv-limit")]
    assert limits == ([expected_limit] if expected_limit else [])
    assert executor.execute(approved, ctx, 0, winner_key) == result
    # B46：升级前落库的请求摘要按不含 Context.run_mode 的旧格式计算，live 重试仍取回原结果；其他运行模式视为不同请求
    legacy_context = {k: v for k, v in ctx.model_dump(mode="json").items() if k != "run_mode"}
    legacy_digest = digest({"context": legacy_context, "expected_seq": 0, "plan": approved.model_dump(mode="json")})
    writer.submit(lambda con: con.execute("UPDATE b16_executions SET request_digest=? WHERE run_id=? AND idempotency_key=?",
                                          (legacy_digest, run.run_id, winner_key)))
    assert executor.execute(approved, ctx, 0, winner_key) == result
    with pytest.raises(PlantkgError) as other_mode:
        executor.execute(approved, ctx.model_copy(update={"run_mode": "trial"}), 0, winner_key)
    assert other_mode.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    with pytest.raises(PlantkgError) as changed:
        executor.execute(approved, ctx, 1, winner_key)
    assert changed.value.code == ErrorCode.IDEMPOTENCY_MISMATCH
    with pytest.raises(PlantkgError) as same_slot:
        executor.execute(approved, ctx, 0, "different-key")
    assert same_slot.value.code == ErrorCode.STATE_CONFLICT
    later = ctx.model_copy(update={"as_of": slot.end})
    assert executor.read_execution(later, 1) == result
    assert clock.at(later, slot.end).pack_states["storage"] == {"energy_kwh": 50, "capacity_kwh": 100}
    assert clock.at(later, start).pack_states == {}
    results = [result]
    for index in range(1, 96):
        if restart and index == 40:
            from plantkg.core.actions import SqlitePendingActionStore
            from plantkg.core.runs import SqliteRunStore
            from plantkg.core.versions import SqliteVersionStore
            from plantkg.decision.gate.service import GateService
            from plantkg.storage.migration import MigrationRunner
            from plantkg.storage.sqlite import SQLiteWriter
            path = writer.path
            writer.close()
            payload = tmp_path / "restart-payload.json"
            payload.write_text(json.dumps({"plan": approved.model_dump(mode="json"),
                "context": ctx.model_dump(mode="json"), "key": winner_key}), encoding="utf-8")
            subprocess.run([sys.executable, "-m", "tests.execution.restart_worker", str(path), str(tmp_path),
                            str(payload)], check=True, timeout=60)
            writer = SQLiteWriter(path)
            request.addfinalizer(writer.close)
            assert MigrationRunner(writer).apply().applied == ()
            versions = SqliteVersionStore(writer, PLANT)
            frozen_refs = ManifestReferences()
            frozen_refs.bind(versions)
            series = ParquetSeriesStore(tmp_path / "record-series", writer, PLANT, frozen_refs.series)
            art = IntegrationArtifacts(writer, series, PLANT, tmp_path / "record-artifacts")
            frozen_refs.bind_source_maps(art)
            runs = SqliteRunStore(writer, PLANT, versions)
            clock, query = install(Registry(), writer, runs, series)
            pending = SqlitePendingActionStore(writer, PLANT, {})
            gate = GateService(writer, PLANT, pending, runs, versions, run_clock=clock.current_for_run)
            executor = RecordExecutor(writer, clock, gate, query, runs)
            assert executor.execute(approved, ctx, 0, winner_key) == result
        results.append(executor.execute(approved, ctx, index, f"synthetic-executor-{index}"))
    night = results[1]
    assert night.ledger.entries[0].discharge_kwh == .25
    assert night.ledger.entries[0].pv_available_kwh == night.ledger.entries[0].pv_curtailed_kwh == 0
    assert next(a for a in night.executed if a.action_id == "discharge").executed_value == 1
    assert night.soc_deviation > 0
    assert results[2].ledger.entries[0].energy_start_kwh == night.ledger.entries[0].energy_end_kwh
    day = results[3]
    assert day.ledger.entries[0].pv_curtailed_kwh == cap / 4
    assert day.ledger.entries[0].discharge_kwh == .25
    assert next(a for a in day.executed if a.action_id == "discharge-day").executed_value == 1
    assert [results[i].ledger.entries[0].load_kwh * 4 for i in (4, 5, 6, 7)] == [8, 6, 12, 14]
    assert sum(results[i].ledger.entries[0].load_kwh for i in (4, 5, 6, 7)) == 10
    assert all(r.ledger.balanced and r.event_seq == i + 1 for i, r in enumerate(results))
    assert clock.current(ctx)[1] == 96
    # Query the actual committed ledgers, including after the independent
    # process restart exercised by the restart=True cases.
    from plantkg.world.record.providers import install as install_ledger
    registry = Registry()
    install_ledger(registry, writer, clock, gate.runs)
    rest = registry.rest[("GET", "/runs/{run_id}/ledger")]
    read_ctx = ctx.model_copy(update={"as_of": start + timedelta(days=1)})
    page = rest(read_ctx, {"run_id": run.run_id}, {"kind": "execution", "page_size": "17"})
    fetched = list(page.items)
    first_cursor = page.next_cursor
    while page.next_cursor:
        page = rest(read_ctx, {"run_id": run.run_id},
                    {"kind": "execution", "page_size": "17", "cursor": page.next_cursor})
        fetched.extend(page.items)
    assert fetched == [item.ledger for item in results]
    early_ctx = ctx.model_copy(update={"as_of": slot.end})
    early = rest(early_ctx, {"run_id": run.run_id}, {"kind": "execution"})
    assert list(early.items) == [results[0].ledger]
    with pytest.raises(PlantkgError) as mismatched_cursor:
        rest(early_ctx, {"run_id": run.run_id},
             {"kind": "execution", "cursor": first_cursor})
    assert mismatched_cursor.value.code == ErrorCode.INVALID_REQUEST
    entries = [r.ledger.entries[0] for r in results]
    assert sum(e.load_kwh for e in entries) == pytest.approx(235.5)
    assert sum(e.grid_import_kwh for e in entries) == pytest.approx(233.25 if cap == 8 else 233.0)
    assert sum(e.cost_cny for e in entries) == pytest.approx(233.25 if cap == 8 else 233.0)
    assert max(e.grid_import_kwh * 4 for e in entries) == 14
    assert entries[-1].energy_end_kwh == pytest.approx(50 - .5 / .9 + .225)
    assert executor.execute(approved, ctx, 0, winner_key) == result
    historical = clock.at(ctx.model_copy(update={"as_of": slot.end}), slot.end)
    assert historical.event_seq == 1 and historical.pack_states["storage"]["energy_kwh"] == 50
    op_time = start + timedelta(minutes=15*7)
    op_snapshot = clock.at(ctx.model_copy(update={"as_of": op_time}), op_time)
    assert op_snapshot.pack_states["production"]["operations"]["op1"]["status"] == "running"
