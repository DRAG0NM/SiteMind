"""#217-3 / #238：事件单与工单状态迁移只追加 status_history，导出按当日末状态带出。"""
from __future__ import annotations

from plantkg.world.scenario import ScenarioRunner
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.export import export_day

from .conftest import local, scenario


def test_status_history_appends_only_on_transition_and_survives_fork(tmp_path):
    # 困人开单（等人确认）→ 08:10 上传资料分叉并重派 → 08:15 确认；另一张单子走纠错关闭
    sc = scenario(events=[
        {"at": "08:05", "id": "tr-1", "type": "elevator_trapped", "entity_id": "FE-2", "severity": 5, "summary": "货梯困人"},
        {"at": "08:06", "id": "lt-1", "type": "lighting_fault", "entity_id": "LT-A03", "severity": 2, "summary": "照明回路故障"},
    ], operator_actions=[
        {"at": "08:10", "kind": "upload", "payload": {"document": "手册", "adds": ["x"]}},
        {"at": "08:15", "kind": "confirm", "payload": {"event": "tr-1"}},
        {"at": "08:20", "kind": "correct", "payload": {"event": "lt-1", "summary": "已修复"}},
    ])
    world = build_world(sc)
    db = open_db(tmp_path / "status_history.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db)
        trace = runner.run(until=local("08:25"))
        assert len(trace.runs) > 1  # 分叉后历史仍连续
        record = export_day(db, trace.run_id, "2026-07-01")
    finally:
        db.close()
    tickets = {t.ticket_id: t for t in record.tickets}
    trapped = next(t for t in record.tickets if "tr-1" in t.event_ids)
    history = [c.status for c in trapped.status_history]
    assert trapped.status == "resolved" and history[0] == "open" and history[-1] == "resolved"
    assert trapped.status_history[0].at == trapped.opened_at  # 初始 open 一条
    assert "waiting" in history
    assert all(a != b for a, b in zip(history, history[1:]))  # 只在状态变化时追加
    closed = next(t for t in tickets.values() if "lt-1" in t.event_ids)
    assert closed.status == "closed" and [c.status for c in closed.status_history][-1] == "closed"  # 纠错关闭路径
    orders = [w for w in record.work_orders if w.ticket_id == trapped.ticket_id and w.status == "confirmed"]
    assert orders and [c.status for c in orders[-1].status_history] == ["proposed", "confirmed"]
    assert orders[-1].status_history[0].at == orders[-1].created_at  # proposed 一条按创建时刻
