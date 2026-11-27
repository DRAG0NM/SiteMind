"""B31/B04 验收：#97 写入端口、#98 as_of、#105 B47×B31 预检、#122 未来证据、#147 trace 过滤。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Context, ContextChannel, EvidenceEvent
from plantkg.core.evidence import SqliteEvidenceSink
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry
from plantkg.world.scenario.script import load_scenario

SCENARIO = "configs/scenarios/factory_demo_day.yaml"


def _services(tmp_path):
    settings = load_settings(overrides={"storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
                                        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")})
    return build_services(settings)


def _client(services):
    from plantkg.api import create_app

    return TestClient(create_app(services))


def _event(event_id, run_id, at, *, trace="trace-a", available_at=None, action_id=None, agent_run_id=None):
    return EvidenceEvent(event_id=event_id, seq=0, occurred_at=at, available_at=available_at or at,
                         run_id=run_id, trace_id=trace, action_id=action_id, agent_run_id=agent_run_id,
                         event_type="execution_recorded", result_status="ok")


def test_default_assembly_scenario_evidence_visible_with_preflight(tmp_path):
    """#97/#98/#105：默认装配下协同组场景证据经 B04 端口写入，按运行时钟可查，B47 路径也有预检来源。"""
    services = _services(tmp_path)
    try:
        scenario = load_scenario(SCENARIO)
        world = build_world(scenario)
        db = ScenarioDB.from_writer(services.storage.writer, world.site.plant_id)
        runner = ScenarioRunner(world, db, registry=default_registry(plugins=services.registry),
                                mode="evaluation", group="coordinated")
        runner.run(until=scenario.instant("08:20"))
        run_id = runner.run_info.run_id
        resolve_policy = services.registry.hooks_for("evolution.policy_for_run")[-1]
        assert resolve_policy(run_id, runner.policy) == runner.policy
        altered_policy = runner.policy.model_copy(update={"major_loss_cny": 123456})
        assert resolve_policy(run_id, altered_policy) is None
        actions = [a for a in runner.trace.actions if a["source"].get("proposal_id")]
        assert actions  # 演示日 08:20 前有专家提案执行的动作

        sink = services.version_run.evidence
        context = runner._context(runner.run_info.version_manifest_id).model_copy(update={"as_of": runner.now})
        stored = sink.list(run_id, None, context, page_size=200).items
        assert stored and {event.params.get("assembly") for event in stored} == {"storage"}

        client = _client(services)
        pages = [client.get("/api/v1/evidence", params={"run_id": run_id, "action_id": action["action_id"],
                                                        "page_size": 200}).json() for action in actions]
        items = [item for page in pages for item in page["items"]]
        assert items  # 旧实现用默认 as_of 把场景日证据全滤掉（#98）
        assert any(item["params"].get("link_type") == "preflight_observation" for item in items)  # #105
        assert any(item["event_type"] == "graph_query" for item in items)
        assert any(item["params"].get("evidence_status") == "preflight_checked" for item in items)
    finally:
        services.close()


def test_scenario_evidence_marks_fallback_assembly_without_port(tmp_path):
    """#97：事件命令行的场景库没有装配端口时退回 runner.db.writer，并在证据上标 assembly=scenario_db。"""
    scenario = load_scenario(SCENARIO)
    world = build_world(scenario)
    db = open_db(tmp_path / "scenario.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, mode="evaluation", group="coordinated")
        runner.run(until=scenario.instant("08:20"))
        context = runner._context(runner.run_info.version_manifest_id).model_copy(update={"as_of": runner.now})
        events = SqliteEvidenceSink(db.writer, db.plant_id).list(runner.run_info.run_id, None, context, page_size=200).items
        assert events and {event.params.get("assembly") for event in events} == {"scenario_db"}
    finally:
        db.close()


def test_evidence_run_clock_filters_future_events_and_available_at(tmp_path):
    """#122：run 级查询在 SQL 里按运行时钟过滤 occurred_at 与 available_at，未来证据不返回。"""
    services = _services(tmp_path)
    try:
        sink = services.version_run.evidence
        cutoff = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        sink.run_clock = lambda run_id: cutoff
        sink.append(_event("visible", "run-clock", cutoff - timedelta(hours=1), trace="trace-a"))
        sink.append(_event("future-occurred", "run-clock", cutoff + timedelta(hours=1), trace="trace-a"))
        sink.append(_event("future-available", "run-clock", cutoff - timedelta(minutes=30),
                           available_at=cutoff + timedelta(hours=1), trace="trace-a"))

        client = _client(services)
        page = client.get("/api/v1/evidence", params={"run_id": "run-clock", "page_size": 200}).json()
        assert [item["event_id"] for item in page["items"]] == ["visible"]
        # 过滤在分页前：第一页就是可见事件，不会出现“空页带走游标、可见证据被跳过”
        first = client.get("/api/v1/evidence", params={"run_id": "run-clock", "page_size": 1}).json()
        assert [item["event_id"] for item in first["items"]] == ["visible"]
    finally:
        services.close()


def test_evidence_trace_filter_applies_to_run_query(tmp_path):
    """#147：不带 action 的 run 查询也要按 trace 过滤；旧实现只在 action 分支过滤。"""
    services = _services(tmp_path)
    try:
        sink = services.version_run.evidence
        at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
        sink.run_clock = lambda run_id: at + timedelta(hours=1)
        sink.append(_event("mine", "run-trace", at, trace="trace-a"))
        sink.append(_event("other", "run-trace", at, trace="trace-b"))

        client = _client(services)
        page = client.get("/api/v1/evidence", params={"run_id": "run-trace", "trace_id": "trace-a"}).json()
        assert [item["event_id"] for item in page["items"]] == ["mine"]
    finally:
        services.close()
