"""Acceptance smoke: scenario evidence and the reproducible comparison."""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from plantkg.core.contracts import Context, EvidenceEvent, EvidenceRef, ExpertProposal, Interval, Locator, ProposalCost, ProposedAction
from plantkg.core.evidence import SqliteEvidenceSink, rest_providers
from plantkg.knowledge.evidence.demo import run_demo
from plantkg.knowledge.evidence.evaluation import evaluate, _answer, _corpus
from plantkg.knowledge.evidence import hooks
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.runner import ScenarioRunner
from plantkg.world.scenario.script import load_scenario


def test_b31_scenario_and_comparison(tmp_path, capsys):
    run_demo(build_world, open_db, ScenarioRunner, load_scenario)
    text = capsys.readouterr().out
    assert '"graph_query"' in text
    assert '"action_decided"' in text
    # #258 统一窗口后第一幕选中 P-0002+P-0003，多出的 hvac set_mode@admin 图谱里没有支撑，如实记 missing_evidence（原来 0 条）
    assert '"missing_count": 1' in text
    assert '"conflict": true' in text
    assert '"future_document_visible": false' in text
    assert '"future_events_visible": false' in text
    assert '"repeat_added_events": 0' in text
    evaluate(tmp_path)
    result = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert result["mode"] == "offline-synthetic"
    assert len(result["questions"]) == 24
    assert all(len(q["required_documents"]) == 2 for q in result["questions"])
    assert len(result["results"]) == 48
    assert (tmp_path / "results.csv").exists()


def test_missing_evidence_does_not_change_dispatch(tmp_path, monkeypatch):
    writer = SQLiteWriter(tmp_path / "evidence.db")
    MigrationRunner(writer).apply()
    at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    runner = SimpleNamespace(now=at, db=SimpleNamespace(writer=writer, plant_id="plant"),
        run_info=SimpleNamespace(run_id="run", version_manifest_id="vm"),
        world=SimpleNamespace(scenario=SimpleNamespace(scenario_id="test", nature="synthetic")))
    monkeypatch.setattr(hooks, "_graph_support", lambda *_: None)
    monkeypatch.setattr(hooks, "_document_support", lambda *_: [])
    monkeypatch.setattr(hooks, "_conflicts", lambda *_: [])
    def proposal(command):
        action = ProposedAction(entity_id="entity://plant/device/x", command=command,
                                interval=Interval(start=at, end=at + timedelta(minutes=5)))
        return ExpertProposal(proposal_id="p", expert="energy", ticket_id="t", actions=(action,),
                              cost=ProposalCost(cny=0), confidence=0.9)
    try:
        ordinary = SimpleNamespace(event={"event_type": "lighting_fault"}, pending=[])
        gated = hooks.preflight(runner, proposal("set_mode"), ordinary)
        assert gated.actions[0].readiness == "ready"
        assert ordinary.pending == []
        emergency = SimpleNamespace(event={"event_type": "load_spike"}, pending=[])
        assert hooks.preflight(runner, proposal("discharge"), emergency).actions[0].readiness == "ready"
        monkeypatch.setattr(hooks, "_document_support", lambda *_: [SimpleNamespace(document=SimpleNamespace(document_id="doc"))])
        monkeypatch.setattr(hooks, "_library", lambda: SimpleNamespace(upload_file=lambda _: tmp_path / "evidence.db"))
        monkeypatch.setattr(hooks, "_conflicts", lambda *_: [({"subject": "x"}, EvidenceRef(evidence_id="doc", locator=Locator(clause="1")))])
        conflict_state = SimpleNamespace(event={"event_type": "lighting_fault"}, pending=[])
        assert hooks.preflight(runner, proposal("set_mode"), conflict_state).actions[0].readiness == "ready"
        context = Context(plant_id="plant", industry_pack="manufacturing", actor=None, channel="offline",
                          run_id="run", trace_id="scenario-test", as_of=at)
        events = SqliteEvidenceSink(writer, "plant").list("run", None, context).items
        assert {e.params["disposition"] for e in events if "disposition" in e.params} == {"record_only"}
        assert any(e.event_type == "conflict_detected" for e in events)
    finally:
        writer.close()


def test_b28_missing_evidence_preserves_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_graph_support", lambda *_: None)
    monkeypatch.setattr(hooks, "_document_support", lambda *_: [])
    monkeypatch.setattr(hooks, "_conflicts", lambda *_: [])
    scenario = load_scenario("configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    db = open_db(tmp_path / "scenario.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, mode="evaluation", group="coordinated")
        runner.run(until=scenario.instant("08:20"))
        assert any(d.result.outcome == "executed" for d in runner.trace.decisions)
        assert any(a["source"].get("proposal_id") for a in runner.trace.actions)
        assert any(a["action"]["command"] == "discharge" for a in runner.trace.actions)
    finally:
        db.close()


@pytest.mark.parametrize("group", ["independent", "coordinated"])
def test_evidence_write_failure_does_not_interrupt_dispatch(tmp_path, monkeypatch, caplog, group):
    original = SqliteEvidenceSink.append
    def fail_execution(self, event):
        if event.event_type == "execution_recorded":
            raise RuntimeError("injected evidence failure")
        return original(self, event)
    monkeypatch.setattr(SqliteEvidenceSink, "append", fail_execution)
    scenario = load_scenario("configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    db = open_db(tmp_path / "failure.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, mode="evaluation", group=group)
        runner.run(until=scenario.instant("08:20"))
        assert runner.trace.actions
        assert "evidence hook action_recorded failed" in caplog.text
        assert runner.now >= scenario.instant("08:20")
        if group == "coordinated":
            assert runner.trace.decisions
    finally:
        db.close()


def test_action_query_contains_observed_conflict(tmp_path, monkeypatch):
    scenario = load_scenario("configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    db = open_db(tmp_path / "conflict.db", world.site.plant_id)
    monkeypatch.setattr(hooks, "_conflicts", lambda docs: [({"subject": "test", "graph_safety": {"tags": []}},
                                                            EvidenceRef(evidence_id="conflicting-doc"))] if docs else [])
    try:
        runner = ScenarioRunner(world, db, mode="evaluation", group="coordinated")
        runner.run(until=scenario.instant("08:20"))
        context = runner._context(runner.run_info.version_manifest_id).model_copy(
            update={"run_id": runner.run_info.run_id, "as_of": runner.now})
        provider = rest_providers(SqliteEvidenceSink(db.writer, db.plant_id))["/evidence"]
        pages = [provider(context, {}, {"run_id": context.run_id, "action_id": a["action_id"]})
                 for a in runner.trace.actions if runner._b31_action_runs.get(a["action_id"]) == context.run_id]
        assert any(e.event_type == "conflict_detected" and e.action_id for page in pages for e in page.items)
    finally:
        db.close()


def test_graph_relation_is_required_and_missing_path_falls_back(tmp_path, monkeypatch):
    from plantkg.knowledge.graph.graph import GlobalGraph
    library, questions = _corpus(tmp_path)
    q = questions[0]
    with_path, _ = _answer(library, q["question"], ["GRID", q["gold_upstream"], q["entity"]])
    without_path, _ = _answer(library, q["question"])
    assert with_path["upstream"] == q["gold_upstream"]
    assert without_path["upstream"] is None
    missing = questions[-1]
    fallback, hits = _answer(library, missing["question"])
    assert fallback["upstream"] == missing["gold_upstream"]
    assert {h.document.document_id for h in hits} == set(missing["required_documents"])
    monkeypatch.setattr(GlobalGraph, "trace_impact", lambda *_: {"affected": []})
    evaluate(tmp_path / "no-graph")
    result = json.loads((tmp_path / "no-graph" / "results.json").read_text(encoding="utf-8"))
    assert result["summary"]["graph-first"]["correct"] == 1


def test_second_act_actions_have_preflight_sources(tmp_path):
    scenario = load_scenario("configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    db = open_db(tmp_path / "second-act.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, mode="evaluation", group="coordinated")
        runner.run(until=scenario.instant("10:40"))
        context = runner._context(runner.run_info.version_manifest_id).model_copy(
            update={"run_id": runner.run_info.run_id, "as_of": runner.now})
        sink = SqliteEvidenceSink(db.writer, db.plant_id)
        actions = [a for a in runner.trace.actions if runner._b31_action_runs.get(a["action_id"]) == context.run_id]
        # B47 may route the second-act actions to human work orders instead of executing them.
        if not actions:
            pending = [w for w in runner.trace.work_orders.values() if w.status == "proposed"]
            assert {a.command for w in pending for a in w.actions} >= {"repair", "switch_circuit"}
            return
        assert {a["action"]["command"] for a in actions} >= {"switch_circuit"}
        # isolate_leak 归一成 stop 后沿压缩空气命中洁净区硬要求（#174），协同组不再直接执行它；
        # B47 路径的证据预检由 #171 覆盖后，执行动作必须带 preflight 证据（#201 第 3 条）。
        for action in actions:
            events = sink.list(context.run_id, None, context, action_id=action["action_id"]).items
            assert any(e.event_type == "execution_recorded" and e.params["evidence_status"] == "preflight_checked" for e in events)
            assert any(e.event_type == "retrieval" and e.sources for e in events)
            assert any(e.event_type == "graph_query" for e in events)
    finally:
        db.close()


def test_independent_dispatch_binds_actual_proposal_and_event(tmp_path):
    scenario = load_scenario("configs/scenarios/factory_demo_day.yaml")
    world = build_world(scenario)
    db = open_db(tmp_path / "independent.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, mode="evaluation", group="independent")
        runner.run(until=scenario.instant("08:20"))
        context = runner._context(runner.run_info.version_manifest_id).model_copy(
            update={"run_id": runner.run_info.run_id, "as_of": runner.now})
        actions = [a for a in runner.trace.actions if runner._b31_action_runs.get(a["action_id"]) == context.run_id
                   and a["source"].get("proposal_id")]
        assert actions
        sink = SqliteEvidenceSink(db.writer, db.plant_id)
        for action in actions:
            events = sink.list(context.run_id, None, context, action_id=action["action_id"]).items
            basis = next(e for e in events if e.event_type == "action_decided" and e.params.get("proposal"))
            assert basis.params["proposal"]["proposal_id"] == action["source"]["proposal_id"]
            assert basis.params["trigger_event"]["event_id"] in {r.evidence_id for r in basis.sources}
            assert basis.version_manifest_id == context.version_manifest_id

    finally:
        db.close()


def test_minute_controller_records_actual_plan_and_inputs(tmp_path):
    from plantkg.experts.energy.assembly import build_demo_assembly
    from plantkg.experts.energy.day import run_day

    scenario = load_scenario("configs/scenarios/factory_demo_day.yaml")
    assembly = build_demo_assembly(tmp_path / "energy.db", "demo-electronics")
    try:
        day = run_day(scenario=scenario, assembly=assembly, date="2026-07-01")
        day.execute(start_at=scenario.instant("08:00"), until=scenario.instant("08:10"))
        runner = day.runner
        context = runner._context(runner.run_info.version_manifest_id).model_copy(
            update={"run_id": runner.run_info.run_id, "as_of": runner.now})
        actions = [a for a in runner.trace.actions if a["source"].get("layer") == "minute"]
        assert actions
        sink = SqliteEvidenceSink(runner.db.writer, runner.db.plant_id)
        for action in actions:
            events = sink.list(context.run_id, None, context, action_id=action["action_id"]).items
            basis = next(e for e in events if e.event_type == "action_decided" and e.params.get("minute_state"))
            assert basis.params["action"] == action["action"]
            assert basis.params["plan_id"] == action["source"]["plan_id"]
            assert basis.params["controller_result"]["command"] == action["action"]["command"]
            assert basis.params["plan_version"].startswith("sha256:")
            assert basis.sources[0].evidence_id == basis.params["plan_event_id"]
        all_events = sink.list(context.run_id, None, context, page_size=200).items
        assert any(e.event_type == "plan_created" and e.event_id == basis.params["plan_event_id"] for e in all_events)
    finally:
        assembly.close()


def test_action_evidence_http_keeps_run_trace_and_visibility(tmp_path):
    from starlette.testclient import TestClient
    from plantkg.api import create_app
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings

    services = build_services(load_settings(profile="mock", overrides={"packs": []}))
    writer = SQLiteWriter(tmp_path / "http.db")
    MigrationRunner(writer).apply()
    try:
        context = services.guard.anonymous_read_context()
        sink = SqliteEvidenceSink(writer, context.plant_id)
        for event_id, trace, delta in (("visible", "trace-a", -1), ("future", "trace-a", 1), ("other", "trace-b", -1)):
            at = context.as_of + timedelta(days=delta)
            sink.append(EvidenceEvent(event_id=event_id, seq=0, occurred_at=at, available_at=at,
                run_id="run", trace_id=trace, action_id="action", event_type="execution_recorded", result_status="ok"))
        services.registry.rest[("GET", "/evidence")] = rest_providers(sink)["/evidence"]
        with TestClient(create_app(services)) as client:
            response = client.get("/api/v1/evidence", params={"run_id": "run", "action_id": "action", "trace_id": "trace-a"})
            assert response.status_code == 200
            assert [item["event_id"] for item in response.json()["items"]] == ["visible"]
            assert client.get("/api/v1/evidence", params={"action_id": "action"}).status_code == 400
    finally:
        writer.close()
