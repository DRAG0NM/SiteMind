"""#170 D55 工单来源结构化与轮次生命周期：默认装配 + 真实 Sqlite store 的集成测试。"""

from __future__ import annotations

from plantkg.world.scenario import ScenarioRunner
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.script import Scenario

ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]


def _scenario(events, policy=None) -> Scenario:
    return Scenario.model_validate({
        "scenario_id": "d55", "title": "D55 第二轮测试", "date": "2026-07-01", "seed": 12,
        "initial": {"soc": 0.5}, "events": events, "policy": dict(policy or {})})


def _runner(tmp_path, events, policy=None, name="d55.db"):
    scenario = _scenario(events, policy)
    world = build_world(scenario)
    db = open_db(tmp_path / name, world.site.plant_id)
    runner = ScenarioRunner(world, db, group="coordinated")
    runner.start()
    return runner, db, scenario


def _service(runner):
    return next(h.__self__ for h in runner.registry.hooks_for("router.urgent")
                if getattr(h, "__self__", None) is not None)


def test_d55_second_round_does_not_expire_work_order_or_auto_execute(tmp_path):
    """#102：D55 第 2 轮仍只进工单；第 1 轮工单不被 _expire_older 过期，动作不自动执行。"""
    runner, db, scenario = _runner(tmp_path, events=[
        {"at": "08:05", "id": "spike1", "type": "load_spike", "entity_id": "AC-2", "severity": 4,
         "summary": "空压站与冲压线同时启动，总用电突增",
         "payload": {"effects": [{"kind": "extra_load", "kw": 550, "minutes": 40}]}},
        {"at": "08:10", "id": "spike2", "type": "load_spike", "entity_id": "AC-2", "severity": 3,
         "summary": "同类告警再次到达"}],
        policy={"major_loss_cny": 10000})
    try:
        trace = runner.run_until(scenario.instant("08:20"))
        router = runner.dispatcher.router
        ticket_id = router.store.event_ticket("spike1")
        service = _service(runner)

        event = router.store.event("spike1")["event"]
        assert event.payload.get("reason_code") == "d55"
        assert router.store.ticket_state(ticket_id, "urgent_reason_code") == "d55"

        d55_tasks = [task for task in service.store.tasks(ticket_id) if task.origin == "d55"]
        assert d55_tasks and all(task.status != "expired" for task in d55_tasks)
        assert any(task.status == "proposed" for task in d55_tasks)

        executed_round2 = [a for a in trace.actions
                           if a["source"].get("ticket_id") == ticket_id and a["source"].get("round") == 2]
        assert executed_round2 == []
        decisions = [d for d in trace.decisions if d.ticket_id == ticket_id and d.round == 2]
        assert decisions and decisions[-1].result.outcome != "executed"
    finally:
        db.close()


def test_safety_urgent_carries_safety_veto_reason_code(tmp_path):
    """安全标记入口的来源码与 D55 分开；第 2 轮仍进工单，不自动执行处置动作。"""
    runner, db, scenario = _runner(tmp_path, events=[
        {"at": "09:00", "id": "trap1", "type": "elevator_trapped", "entity_id": "FE-2", "severity": 5,
         "summary": "2 号货梯困人"},
        {"at": "09:05", "id": "trap2", "type": "elevator_trapped", "entity_id": "FE-2", "severity": 5,
         "summary": "同一货梯再次报困人"}])
    try:
        trace = runner.run_until(scenario.instant("09:15"))
        router = runner.dispatcher.router
        ticket_id = router.store.event_ticket("trap1")
        service = _service(runner)

        event = router.store.event("trap1")["event"]
        assert event.payload.get("reason_code") == "safety_veto"
        assert router.store.ticket_state(ticket_id, "urgent_reason_code") == "safety_veto"
        assert any(task.origin == "safety_veto" and task.status == "proposed"
                   for task in service.store.tasks(ticket_id))
        executed = [a for a in trace.actions if a["source"].get("ticket_id") == ticket_id]
        assert executed == []
    finally:
        db.close()
