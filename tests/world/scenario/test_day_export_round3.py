"""#238 导出完整性（第三轮）：跨日 context、当日末状态、链校验、CLI 数据补齐与 config 限值。

都经真实默认装配（`default_registry` / `build_world` / 真实 SQLite / HTTP 路由），不直接改内部状态。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

from plantkg.contracts.validate import validate
from plantkg.core.config import load_settings
from plantkg.core.contracts import (
    Actor, Context, ContextChannel, DayDecision, DecisionConflict, DecisionConversion, DecisionResult, EvidenceEvent,
    EvidenceEventType, EvidenceRef, Interval, ResultStatus, Ticket,
)
from plantkg.core.evidence import SqliteEvidenceSink
from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog
from plantkg.world.scenario import ScenarioRunner, load_scenario
from plantkg.world.scenario.__main__ import ROOT, build_world, open_db
from plantkg.world.scenario.export import export_day, validate_chain

from .conftest import local, scenario


def write_day(tmp_path, record, name: str) -> list[str]:
    path = tmp_path / name
    path.write_text(record.model_dump_json(indent=1), encoding="utf-8")
    return validate(path)


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_cross_midnight_context_carries_previous_day_proposal_and_event(tmp_path):
    """23:55 触发、次日 00:05 裁决：次日导出没有悬空 ID，前一日提案与事件进 context。"""
    sc = scenario(days=2, events=[
        {"at": "23:55", "type": "load_spike", "entity_id": "AC-1", "severity": 3, "summary": "深夜负荷突增"},
    ])
    world = build_world(sc)
    db = open_db(tmp_path / "midnight.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, group="coordinated")
        trace = runner.run(until=local("00:10", day=2))
        proposals = {p["proposal_id"]: p for r in trace.runs for p in db.store.records(r.run_id, "proposal")}
        tickets = {t["ticket_id"]: t for r in trace.runs for t in db.store.records(r.run_id, "ticket")}
        assert proposals and tickets  # 探针前提：23:55 的事件确实开了单、出了提案
        proposal = next(p for p in proposals.values() if _at(p["binding"]["trigger_at"]) < local("00:00", day=2))
        ticket = tickets[proposal["ticket_id"]]
        runner.record_decision(DayDecision(
            decision_id="decision-cross-midnight", ticket_id=ticket["ticket_id"], round=ticket["round"], layer=3,
            at=local("00:05", day=2),
            conflict=DecisionConflict(summary="跨零点裁决", proposal_ids=(proposal["proposal_id"],)),
            conversion=DecisionConversion(policy_version="scenario-policy:test", scores={}),
            result=DecisionResult(selected_proposal_ids=(proposal["proposal_id"],), outcome="executed", summary="执行"),
        ))
        event_id = ticket["event_ids"][0]
        db.store.write_records(trace.run_id, [("sensor_case", event_id, local("23:55"), {
            "event_id": event_id, "sensor": "AC-1.P", "reading": 210.0, "baseline": 150.0, "residual_c": 3.2,
            "residual_limit_c": 2.0, "quality": "suspect", "candidates": ["sensor_fault"], "conclusion": "读数可疑",
            "verified_by": "b22", "excluded": [], "resolution": "probe", "nature": "simulated"})])
        day1 = export_day(db, trace.run_id, "2026-07-01")
        day2 = export_day(db, trace.run_id, "2026-07-02")
        problems = write_day(tmp_path, day2, "day2-midnight.json")
    finally:
        db.close()

    assert problems == []
    assert [d.decision_id for d in day2.decisions] == ["decision-cross-midnight"]
    assert "decision-cross-midnight" not in {d.decision_id for d in day1.decisions}  # 次日裁决不进前一日
    # 前一日提案不进当日提案小节，进 context；决策引用因此不悬空
    assert proposal["proposal_id"] not in {p.proposal_id for p in day2.proposals}
    assert proposal["proposal_id"] in {p.proposal_id for p in day2.context.proposals}
    referenced = {pid for d in day2.decisions for pid in d.conflict.proposal_ids}
    assert referenced <= {p.proposal_id for p in (*day2.proposals, *day2.context.proposals)}
    # 事件单的事件在当日没导出时也进 context
    assert set(ticket["event_ids"]) - {e.event_id for e in day2.events}
    assert set(ticket["event_ids"]) <= {e.event_id for e in (*day2.events, *day2.context.events)}
    # b17_records(kind="sensor_case") 的字段合并进事件 payload 顶层键（当日事件与 context 事件都合并）
    day1_event = next(e for e in day1.events if e.event_id == event_id)
    assert day1_event.payload["sensor"] == "AC-1.P" and day1_event.payload["conclusion"] == "读数可疑"
    context_event = next(e for e in day2.context.events if e.event_id == event_id)
    assert context_event.payload["residual_c"] == 3.2


def test_ticket_and_work_order_status_follow_day_end(tmp_path):
    """#217-3：7-01 导出 waiting / proposed，7-02 导出 resolved；status_history 截到当日结束。"""
    sc = scenario(days=2, events=[
        {"at": "23:55", "id": "day1-trapped", "type": "elevator_trapped", "entity_id": "FE-2", "severity": 5, "summary": "2 号货梯困人"},
    ], operator_actions=[
        {"at": "+1 00:05", "kind": "confirm", "payload": {"event": "day1-trapped"}},
    ])
    world = build_world(sc)
    db = open_db(tmp_path / "status-history.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        trace = runner.run(until=local("00:10", day=2))
        tickets = {t["ticket_id"]: t for r in trace.runs for t in db.store.records(r.run_id, "ticket")}
        orders = {w["work_order_id"]: w for r in trace.runs for w in db.store.records(r.run_id, "work_order")}
        day1 = export_day(db, trace.run_id, "2026-07-01")
        day2 = export_day(db, trace.run_id, "2026-07-02")
        problems = write_day(tmp_path, day2, "day2-status.json") + write_day(tmp_path, day1, "day1-status.json")
    finally:
        db.close()

    assert problems == []
    assert len(tickets) == 1 and orders  # 23:55 困人开单并生成工单；状态迁移由真实写入侧追加
    ticket = next(iter(tickets.values()))
    order = next(iter(orders.values()))
    ticket_states = [c["status"] for c in ticket["status_history"]]
    assert ticket_states[0] == "open" and ticket_states[-1] == "resolved"  # 初始状态在 history 里
    assert all(a != b for a, b in zip(ticket_states, ticket_states[1:]))  # 只在状态变化时追加
    assert [c["status"] for c in order["status_history"]] == ["proposed", "confirmed"]
    day1_ticket = next(t for t in day1.tickets if t.ticket_id == ticket["ticket_id"])
    assert day1_ticket.status == "waiting"  # 确认发生在次日，7-01 导出不能显示 resolved
    # 截到当日结束：7-01 导出不带 7-02 00:05 的 resolved 迁移
    assert all(c.at < local("00:00", day=2) for c in day1_ticket.status_history)
    assert "resolved" not in [c.status for c in day1_ticket.status_history]
    assert [c.status for c in day1_ticket.status_history] == ticket_states[:-1]
    day1_order = next(w for w in day1.work_orders if w.work_order_id == order["work_order_id"])
    assert day1_order.status == "proposed"
    assert [c.status for c in day1_order.status_history] == ["proposed"]  # 次日的 confirmed 迁移也不提前带出
    day2_ticket = next(t for t in day2.tickets if t.ticket_id == ticket["ticket_id"])
    assert day2_ticket.status == "resolved"  # 7-02 导出取当日结束时的状态
    assert [c.status for c in day2_ticket.status_history] == ticket_states  # 跨日记录整条历史都在当日结束前


def test_validate_chain_cli_passes_on_real_two_day_run(tmp_path):
    """验收命令：export-day 两天后 `validate --chain` 通过（链内并集 + 引用完整性 + DayRecord）。"""
    sc = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    world = build_world(sc)
    db_path = tmp_path / "chain.db"
    db = open_db(db_path, world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db, group="coordinated").run()
    finally:
        db.close()
    out = subprocess.run([sys.executable, "-m", "plantkg.contracts.validate", "--chain", str(db_path), "--run", trace.run_id],
                         cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                         env={"PYTHONIOENCODING": "utf-8", **os.environ})
    assert out.returncode == 0, out.stdout + out.stderr
    assert "通过" in out.stdout


def test_validate_chain_is_read_only_for_missing_or_foreign_db(tmp_path):
    """只读校验：不存在的库不新建；不是场景库的文件不建表、不改结构。"""
    env = {"PYTHONIOENCODING": "utf-8", **os.environ}
    missing = tmp_path / "missing.db"
    out = subprocess.run([sys.executable, "-m", "plantkg.contracts.validate", "--chain", str(missing), "--run", "norun"],
                         cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode != 0 and "不存在" in out.stderr + out.stdout
    assert not missing.exists()

    foreign = tmp_path / "foreign.db"
    con = sqlite3.connect(foreign)
    con.execute("CREATE TABLE plain(id TEXT)")
    con.commit()
    con.close()
    out = subprocess.run([sys.executable, "-m", "plantkg.contracts.validate", "--chain", str(foreign), "--run", "norun"],
                         cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
    assert out.returncode != 0 and "不是场景库" in out.stderr + out.stdout
    con = sqlite3.connect(foreign)
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"plain"}
    con.close()


def test_validate_chain_accepts_relative_db_path(tmp_path):
    """--chain 传相对路径也要能跑通（在临时目录 chdir 后用相对文件名调用 CLI）。"""
    sc = scenario()
    world = build_world(sc)
    db = open_db(tmp_path / "relative.db", world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db).run(until=local("00:05"))
    finally:
        db.close()
    out = subprocess.run([sys.executable, "-m", "plantkg.contracts.validate", "--chain", "relative.db", "--run", trace.run_id],
                         cwd=str(tmp_path), capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stdout + out.stderr
    assert "通过" in out.stdout


def test_validate_chain_covers_last_partial_day(tmp_path):
    """运行结束在 7-02 00:10 时，链校验要覆盖 7-02，才能发现那天的悬空引用。"""
    sc = scenario(days=1)  # 起始 7-01 00:00
    world = build_world(sc)
    db = open_db(tmp_path / "partial.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        runner.start()
        runner.run_until(local("00:05"))
        context = Context(plant_id=world.site.plant_id, industry_pack="manufacturing",
                          actor=Actor(actor_id="chain-verify", roles=("operator",)), channel=ContextChannel.offline,
                          trace_id="partial-day", as_of=local("22:00"),
                          version_manifest_id=runner.run_info.version_manifest_id, run_mode="evaluation")
        run = db.runs.create_run(context, "scenario", Interval(start=sc.start, end=local("00:10", day=2)),
                                 forked_from=runner.run_info.run_id, forked_at=local("00:00", day=2))
        ticket = Ticket(ticket_id="ticket://TL-late", opened_at=local("23:55"), status="open", round=0, event_ids=())
        db.store.write_records(run.run_id, [("ticket", ticket.ticket_id, ticket.opened_at, ticket.model_dump(mode="json"))])
        decision = DayDecision(decision_id="decision://D-late", ticket_id=ticket.ticket_id, round=1, layer=3,
                               at=local("00:05", day=2),
                               conflict=DecisionConflict(summary="跨日裁决", proposal_ids=("proposal://missing",)),
                               conversion=DecisionConversion(policy_version="scenario-policy:test", scores={}),
                               result=DecisionResult(selected_proposal_ids=("proposal://missing",), outcome="executed", summary="执行"))
        db.store.write_records(run.run_id, [("decision", decision.decision_id, decision.at, decision.model_dump(mode="json"))])
        problems = validate_chain(db, run.run_id)
    finally:
        db.close()
    assert any("2026-07-02" in problem and "proposal://missing" in problem for problem in problems)


def test_export_day_cli_carries_metrics_evidence_assets_and_config(tmp_path):
    """export-day 从同库读证据/资产、合并 B33 metrics（dict 与裸列表两种写法），并自动带 config 限值。"""
    sc = scenario()
    world = build_world(sc)
    db_path = tmp_path / "cli.db"
    db = open_db(db_path, world.site.plant_id)
    try:
        trace = ScenarioRunner(world, db).run()
        doc = tmp_path / "policy.md"
        doc.write_text("# 演示规程\n按规程执行。\n", encoding="utf-8")
        context = Context(plant_id=world.site.plant_id, industry_pack=load_settings().industry_pack,
                          actor=Actor(actor_id="cli-test", roles=()), channel=ContextChannel.offline,
                          trace_id="cli-test", as_of=local("12:00"))
        catalog = SQLiteAssetCatalog(db.writer, world.site.plant_id, tmp_path / "asset_blobs", base_dir=tmp_path)
        asset = catalog.ingest(context, str(doc), "policy", local("08:00"), title="演示规程")
        SqliteEvidenceSink(db.writer, world.site.plant_id).append(EvidenceEvent(
            event_id="ev-cli-1", seq=0, occurred_at=local("08:06"), run_id=trace.run_id, trace_id="cli-test",
            event_type=EvidenceEventType.retrieval, result_status=ResultStatus.ok,
            sources=(EvidenceRef(asset_version_id=asset.asset_version_id, excerpt="规程原文"),)))
    finally:
        db.close()
    report_file = tmp_path / "metrics.json"
    report_file.write_text(json.dumps({"metrics": [
        {"metric": "electricity_cost", "group": "coordinated", "value": 1234.5, "unit": "CNY", "note": "whole scenario window"}]}), encoding="utf-8")
    list_file = tmp_path / "metrics-list.json"
    list_file.write_text(json.dumps([
        {"metric": "knowledge_recall", "group": "knowledge", "value": 0.82, "unit": "ratio", "note": "synthetic"}]), encoding="utf-8")
    out = tmp_path / "day1.json"
    run = subprocess.run([sys.executable, "-m", "plantkg.world.scenario", "export-day", "--db", str(db_path),
                          "--run", trace.run_id, "--date", "2026-07-01", "--metrics", str(report_file),
                          "--metrics", str(list_file), "--out", str(out)],
                         cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                         env={"PYTHONIOENCODING": "utf-8", **os.environ})
    assert run.returncode == 0, run.stdout + run.stderr
    record = json.loads(out.read_text(encoding="utf-8"))
    configured = [m for m in record["metrics"] if m["group"] == "config"]
    assert {m["metric"] for m in configured} == {"transformer_limit_pct", "contract_capacity_kw", "battery_soc_floor_pct",
                                                 "hvac_supply_setpoint_c", "compressed_air_floor_mpa",
                                                 "compressed_air_leak_threshold"}
    assert all(m["nature"] == "configured" for m in configured)
    assert {"electricity_cost", "knowledge_recall"} <= {m["metric"] for m in record["metrics"]}
    assert [e["event_id"] for e in record["evidence"]] == ["ev-cli-1"]
    assert [a["asset_version_id"] for a in record["assets"]] == [asset.asset_version_id]  # 只带被证据引用的资产
    assert validate(out) == []

    out2 = tmp_path / "day2.json"
    run2 = subprocess.run([sys.executable, "-m", "plantkg.world.scenario", "export-day", "--db", str(db_path),
                           "--run", trace.run_id, "--date", "2026-07-02", "--out", str(out2)],
                          cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                          env={"PYTHONIOENCODING": "utf-8", **os.environ})
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert "metrics 0 条（未传 --metrics）" in run2.stdout
    assert "evidence 0 条（库内无证据）" in run2.stdout
    assert {m["metric"] for m in json.loads(out2.read_text(encoding="utf-8"))["metrics"] if m["group"] == "knowledge"} == {"asset_total", "asset_activated", "asset_state_counts", "asset_activation_rate"}
