"""B17/B36/B49 day.json 数据面（#182）：运行数据来源、账本摘要单位、提案/Agent 运行/问答、证据与资产。

都经默认装配（`default_registry`、真实 SQLite 库、`export_day`），导出文件再用 `plantkg.contracts.validate` 校验。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from plantkg.contracts.validate import validate
from plantkg.core.contracts import AssetVersion, DayMetric, EvidenceEvent, EvidenceEventType, EvidenceRef, QaEntry, ResultStatus
from plantkg.integrations.nexent.records import SqliteAgentRunStore
from plantkg.world.scenario import ScenarioRunner, load_scenario
from plantkg.world.scenario.__main__ import ROOT, build_world, open_db
from plantkg.world.scenario.export import export_day
from plantkg.world.scenario.inputs import SiteInputProvider
from plantkg.world.scenario.runner import ScenarioWorld
from plantkg.world.site import SiteModel
from plantkg.world.site.loads import synthesize

from .conftest import local, scenario

DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"
SITE = ROOT / "configs/sites/factory/site.yaml"


def export(tmp_path, db, run_id: str, **kw):
    record = export_day(db, run_id, "2026-07-01", **kw)
    path = tmp_path / f"day-{run_id}.json"
    path.write_text(record.model_dump_json(indent=1), encoding="utf-8")
    return record, validate(path)


def test_coordinated_day_export_carries_runs_proposals_agent_runs_qa(tmp_path):
    scenario_ = load_scenario(DEMO)
    world = build_world(scenario_)
    db = open_db(tmp_path / "coordinated.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, group="coordinated")
        trace = runner.run()
        assert len(trace.runs) > 1  # 跑过 07:30 的知识发布分叉
        runner.record_qa(QaEntry(qa_id="qa-001", at=scenario_.instant("08:06"), question="行政楼低区为什么拥堵？",
                                 answer="早高峰访客叠加，建议低区分流。", agent_run_id=trace.agent_runs[0].agent_run_id))
        record, problems = export(tmp_path, db, trace.run_id)
    finally:
        db.close()

    assert problems == []  # 导出文件过 schema 与 DayRecord 校验
    assert record.temp_rules and all(r.created_at and r.effective for r in record.temp_rules)  # B47 备注行被跳过，真实 TempRule 仍在
    assert any("行政楼" in r.text for r in record.temp_rules)
    # 数据来源：每条运行都记，分叉子运行与父一致（fork 继承）
    assert all(r.data_sources for r in record.runs)
    assert {"d05", "pvgis", "tariff"} <= {ref.source for r in record.runs for ref in r.data_sources}
    assert len({tuple(r.data_sources) for r in record.runs}) == 1
    # 提案、Agent 运行、问答：沿 runs 链合并，按 id 去重
    assert record.proposals and len({p.proposal_id for p in record.proposals}) == len(record.proposals)
    assert record.agent_runs and len({r.agent_run_id for r in record.agent_runs}) == len(record.agent_runs)
    assert [q.qa_id for q in record.qa] == ["qa-001"]
    assert record.qa[0].agent_run_id == trace.agent_runs[0].agent_run_id
    assert len({p.binding.run_id for p in record.proposals}) > 1  # 分叉前后都有提案


def test_ledger_summary_uses_ratio_for_both_soc_fields(make, tmp_path):
    sc = scenario(events=[{"at": "08:05", "type": "load_spike", "entity_id": "AC-1", "summary": "突增"}],
                  operator_actions=[{"at": "09:00", "kind": "upload", "payload": {"document": "手册", "adds": ["x"]}}])
    runner = make(sc)
    trace = runner.run()
    assert len(trace.runs) == 2  # 09:00 分叉
    capacity = runner.world.storage.capacity_kwh
    summary = trace.ledger_summary()
    assert 0 <= summary["soc_start"] <= 1 and 0 <= summary["soc_end"] <= 1  # 同前缀同单位：都是比例
    assert summary["soc_start"] == pytest.approx(trace.ledger[0].entry.energy_start_kwh / capacity, abs=1e-3)
    assert summary["soc_end"] == pytest.approx(trace.ledger[-1].entry.energy_end_kwh / capacity, abs=1e-6)
    assert summary["energy_start_kwh"] == pytest.approx(trace.ledger[0].entry.energy_start_kwh, abs=0.05)  # 另给原值
    assert summary["energy_end_kwh"] == pytest.approx(trace.ledger[-1].entry.energy_end_kwh, abs=0.05)
    finished = [e for e in runner.db.runs.events(trace.run_id) if e["event_type"] == "finished"]
    assert finished and json.loads(finished[0]["payload"]) == summary  # run finished payload 与 CLI 同一份摘要


def test_hipe_base_changes_load_source_in_run_info(tmp_path):
    berlin = ZoneInfo("Europe/Berlin")
    rows = ["SensorDateTime,Machine,P_kW"]
    t = datetime(2017, 10, 23, tzinfo=berlin)
    while t < datetime(2017, 10, 24, tzinfo=berlin):
        rows.append(f"{t.isoformat()},MainTerminal,{10 + 6 * (8 <= t.hour < 18):.1f}")
        t += timedelta(minutes=1)
    csv_path = tmp_path / "main.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    from plantkg.adapters import HipeAdapter

    bundle = HipeAdapter().normalize(str(csv_path), {"source_start": "2017-10-23", "plant_start": "2026-07-01", "days": 1,
                                                    "load_scale": {"factor": 25}}, {"delay_seconds": 600})
    sc = scenario()
    site = SiteModel.load(SITE)
    provider = SiteInputProvider(sc, lambda slots: [(0.6, "flat")] * len(slots), loads=synthesize(site, bundle, 1))
    assert provider.data_sources()[0].source == "hipe"
    world = ScenarioWorld.build(sc, provider)
    db = open_db(tmp_path / "hipe.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        runner.start()
        sources = {ref.source: ref.dataset_version for ref in runner.run_info.data_sources}
        assert sources["hipe"] == bundle.bundle_id  # 负荷来源不再是 d05
        assert sources["pvgis"] and sources["tariff"]
    finally:
        db.close()


def test_export_merges_b23_agent_runs_and_referenced_assets(tmp_path):
    sc = scenario(events=[{"at": "08:05", "type": "lighting_fault", "entity_id": "LT-A03", "summary": "照明故障"}])
    world = build_world(sc)
    db = open_db(tmp_path / "evidence.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        trace = runner.run()
        # B23 运行记录库与场景 b17_records 是两套记录：同一编号只导一条
        store = SqliteAgentRunStore(db.writer, db.plant_id)
        for record in trace.agent_runs:
            store.save(record)
        store.save(trace.agent_runs[0].model_copy(update={"agent_run_id": "agent-run-only-b23"}))
        asset_id = "asset://demo-electronics/aaaaaaaa/policy/policy.md"
        asset = AssetVersion(asset_version_id=asset_id, content_hash="sha256:" + "a" * 64, industry="demo-electronics",
                             asset_type="policy", source_uri="file:///demo/policy.md", media_type="text/markdown",
                             nature="synthetic", available_at=local("00:00"), ingested_at=local("00:00"), title="演示规程")
        unused = asset.model_copy(update={"asset_version_id": "asset://demo-electronics/bbbbbbbb/policy/unused.md",
                                          "content_hash": "sha256:" + "b" * 64})
        evidence = (EvidenceEvent(event_id="ev-1", seq=1, occurred_at=local("08:06"), run_id=trace.run_id, trace_id="t",
                                  event_type=EvidenceEventType.retrieval, result_status=ResultStatus.ok,
                                  sources=(EvidenceRef(asset_version_id=asset_id, excerpt="规程原文"),)),)
        record, problems = export(tmp_path, db, trace.run_id, agent_runs=store, evidence=evidence, assets=(asset, unused))
    finally:
        db.close()

    assert problems == []
    ids = [r.agent_run_id for r in record.agent_runs]
    assert "agent-run-only-b23" in ids and len(ids) == len(set(ids))
    assert [a.asset_version_id for a in record.assets] == [asset_id]  # 只导出被证据引用到的资产
    assert [e.event_id for e in record.evidence] == ["ev-1"]


def test_export_day2_carries_previous_day_ticket_and_its_day2_records(tmp_path):
    """前一日打开、次日仍有提案/决策的事件单不能因 opened_at 不在当天就从次日导出里消失。"""
    sc = scenario(days=2, events=[
        {"at": "08:05", "type": "elevator_congestion", "entity_id": "elevator_group:admin_low", "severity": 3, "summary": "首日拥堵"},
        {"at": "+1 08:05", "type": "elevator_congestion", "entity_id": "elevator_group:admin_low", "severity": 3, "summary": "次日拥堵"},
    ])
    world = build_world(sc)
    db = open_db(tmp_path / "crossday.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db, group="coordinated").run(until=local("08:10", day=2))
        day1 = export_day(db, trace.run_id, "2026-07-01")
        record = export_day(db, trace.run_id, "2026-07-02")
        day2 = tmp_path / "day-2.json"
        day2.write_text(record.model_dump_json(indent=1), encoding="utf-8")
        problems = validate(day2)
        all_work_orders = {w["work_order_id"] for r in trace.runs for w in db.store.records(r.run_id, "work_order")}
        all_proposals = {p["proposal_id"] for r in trace.runs for p in db.store.records(r.run_id, "proposal")}
        all_decisions = {d["decision_id"] for r in trace.runs for d in db.store.records(r.run_id, "decision")}
    finally:
        db.close()

    assert problems == []
    start = local("00:00", day=2)
    day1_start = local("00:00", day=1)
    end = local("00:00", day=3)
    carried = [t for t in record.tickets if t.opened_at < start]
    assert len(carried) == 1  # 前一日打开的事件单仍在次日导出里
    ticket_id = carried[0].ticket_id
    assert {p.ticket_id for p in record.proposals} == {ticket_id}
    assert {d.ticket_id for d in record.decisions} == {ticket_id}
    assert all(p.binding and start <= p.binding.trigger_at < end for p in record.proposals)  # 提案仍按当日时间窗过滤
    # 工单/提案/决策都按当日时间窗导出：两天不重复、不丢失
    day1_orders = {w.work_order_id for w in day1.work_orders}
    day2_orders = {w.work_order_id for w in record.work_orders}
    assert day1_orders and day2_orders
    assert day1_orders.isdisjoint(day2_orders)
    assert day1_orders | day2_orders == all_work_orders
    assert all(day1_start <= w.created_at < start for w in day1.work_orders)
    assert all(start <= w.created_at < end for w in record.work_orders)
    day1_proposals = {p.proposal_id for p in day1.proposals}
    day2_proposals = {p.proposal_id for p in record.proposals}
    assert day1_proposals.isdisjoint(day2_proposals)
    assert day1_proposals | day2_proposals == all_proposals
    day1_decisions = {d.decision_id for d in day1.decisions}
    day2_decisions = {d.decision_id for d in record.decisions}
    assert day1_decisions.isdisjoint(day2_decisions)
    assert day1_decisions | day2_decisions == all_decisions
    assert all(day1_start <= d.at < start for d in day1.decisions)
    assert all(day1_start <= p.binding.trigger_at < start for p in day1.proposals if p.binding)


def test_export_day2_carries_ticket_only_referenced_by_operator_confirm(tmp_path):
    """跨日事件单只有次日确认/纠错事件时，也要带进次日导出，不能只导出孤立的事件。"""
    sc = scenario(days=2, events=[
        {"at": "23:50", "id": "day1-trapped", "type": "elevator_trapped", "entity_id": "FE-2", "severity": 5, "summary": "2 号货梯困人"},
    ], operator_actions=[
        {"at": "+1 00:05", "kind": "confirm", "payload": {"event": "day1-trapped"}},
    ])
    world = build_world(sc)
    db = open_db(tmp_path / "crossday-confirm.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db, group="coordinated").run(until=local("00:10", day=2))
        day1 = export_day(db, trace.run_id, "2026-07-01")
        record = export_day(db, trace.run_id, "2026-07-02")
        day2 = tmp_path / "day-2-confirm.json"
        day2.write_text(record.model_dump_json(indent=1), encoding="utf-8")
        problems = validate(day2)
    finally:
        db.close()

    assert problems == []
    assert [e.event_type for e in record.events] == ["operator_confirm"]
    assert len(record.tickets) == 1 and record.tickets[0].opened_at < local("00:00", day=2)
    assert record.tickets[0].event_ids == ("day1-trapped",)
    # 工单按 A 的当日时间窗留在创建日 7-01，7-02 不重复带入（状态时间线限制见 PR 正文）
    assert [w.ticket_id for w in day1.work_orders] == [record.tickets[0].ticket_id]
    assert record.work_orders == ()


def test_export_day2_ignores_event_reference_from_plain_events(tmp_path):
    """普通事件的 payload["event"] 不是值班确认，不能借此把前一日事件单带进次日导出。"""
    sc = scenario(days=2, events=[
        {"at": "23:50", "id": "day1-trapped", "type": "elevator_trapped", "entity_id": "FE-2", "severity": 5, "summary": "2 号货梯困人"},
        {"at": "+1 00:05", "type": "metering_anomaly", "entity_id": "AC-1", "severity": 1, "summary": "计量异常",
         "payload": {"event": "day1-trapped"}},
    ])
    world = build_world(sc)
    db = open_db(tmp_path / "crossday-plain.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db, group="coordinated").run(until=local("00:10", day=2))
        day1 = export_day(db, trace.run_id, "2026-07-01")
        record = export_day(db, trace.run_id, "2026-07-02")
        day2 = tmp_path / "day-2-plain.json"
        day2.write_text(record.model_dump_json(indent=1), encoding="utf-8")
        problems = validate(day2)
    finally:
        db.close()

    assert problems == []
    assert day1.tickets  # 前一日困人事件确实开了事件单
    assert any(e.event_type == "metering_anomaly" for e in record.events)  # 次日普通事件已入库，payload 里带前一日事件 id
    assert day1.tickets[0].ticket_id not in {t.ticket_id for t in record.tickets}  # 不再被普通事件反查带出


def test_coordinated_two_day_export_carries_layer_three_decision(tmp_path):
    """#220：第 3 层裁决按仿真时刻落库，两日导出才收得到；链内决策并集应与两天导出并集一致。"""
    base = load_scenario(DEMO)
    # 评测模式下 09:30 上传的《空压机运行维护手册》会让 10:30 压力事件直接出 ready 动作，第 3 层不触发（实测 0 条）；
    # 去掉这一个上传动作，让 10:30 走 needs_diagnosis → 第 3 层，其余两日剧本不变。
    sc = base.model_copy(update={"operator_actions": [a for a in base.operator_actions
                                                      if not (a.at == "09:30" and a.kind == "upload")]})
    world = build_world(sc)
    db = open_db(tmp_path / "layer3.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db, group="coordinated").run()
        day1 = export_day(db, trace.run_id, "2026-07-01")
        day2 = export_day(db, trace.run_id, "2026-07-02")
        stored = {d["decision_id"]: d for r in trace.runs for d in db.store.records(r.run_id, "decision")}
    finally:
        db.close()

    exported = {d.decision_id for d in day1.decisions} | {d.decision_id for d in day2.decisions}
    layer3 = [d for d in stored.values() if d["layer"] == 3]
    assert layer3  # 这个剧本要真的跑到第 3 层，否则断言是空的
    assert exported == set(stored)  # 链内决策并集 == 两天导出并集（#220 的导出校验）
    assert all(sc.start <= datetime.fromisoformat(d["at"]) < sc.end for d in layer3)  # 裁决时刻在剧本两日内
    assert {d["decision_id"] for d in layer3} <= exported  # 第 3 层裁决至少出现在其中一天的导出里


def test_day_record_carries_knowledge_metrics_and_cache_version(tmp_path):
    sc = scenario()
    world = build_world(sc)
    db = open_db(tmp_path / "metrics.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        trace = runner.run(until=local("00:05"))
        metrics = (DayMetric(metric="knowledge_recall", group="knowledge", value=0.82, unit="ratio", note="synthetic 演示值"),)
        record, problems = export(tmp_path, db, trace.run_id, metrics=metrics)
    finally:
        db.close()
    assert problems == []
    assert record.metrics[:len(metrics)] == metrics
    catalog = record.metrics[len(metrics):]
    assert [m.metric for m in catalog] == [
        "asset_total", "asset_activated", "asset_state_counts", "asset_activation_rate"
    ]
    assert all(m.group == "knowledge" and m.value is None and m.counts is None
               and m.nature == "derived" and m.as_of is not None
               and "资产目录未装配" in m.note for m in catalog)
    assert [m.unit for m in catalog] == ["asset_version"] * 3 + ["ratio"]
    assert record.cache_version is None  # 无生产者就不填，不伪造
