"""#201 第 2 条：分钟层装上时，transformer_overload 预案不再下发电池保护动作（储能由分钟层接管）。

第一幕 08:05 突增走 `transformer_overload` 预案；`install_minute_layer` 之后电池充放由分钟层独占，
图谱模板（graph.yaml）与代码缺省映射（playbooks.py）两条来源都必须过滤掉 role=battery 的保护动作，
其余保护动作与分钟层动作照常。
"""
from pathlib import Path

from plantkg.agent.execution_policy import demo, playbooks
from plantkg.experts.energy.minute import install_minute_layer

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"


def test_minute_layer_takes_over_transformer_battery_protection(tmp_path):
    world, db, registry, runner = demo._open(DEMO, "minute", tmp_path)
    try:
        install_minute_layer(runner)
        trace = runner.run_until(demo._scenario(DEMO).instant("08:31"))
        router = runner.dispatcher.router
        event = next(e for e in trace.events if e.event_id == "act1-spike")
        ticket_id = router.store.event_ticket(event.event_id)
        battery = runner.world.index.battery_id

        protection = [item for item in trace.actions
                      if item["source"].get("ticket_id") == ticket_id and item["source"].get("b47") == "protection"]
        assert {item["action"]["command"] for item in protection} == {"set_mode"}  # 其余保护动作照常
        assert all(item["action"]["entity_id"] != battery for item in protection)  # 不下发电池动作
        note = next(item for item in trace.notes
                    if item.get("kind") == "b47_playbook_skipped" and item.get("role") == "battery")
        assert "储能由分钟层接管" in note["reason"]
        minutes = [item for item in trace.actions if item["source"].get("layer") == "minute"]
        assert minutes and all(item["action"]["entity_id"] == battery for item in minutes)

        # 图谱没给动作模板时走 playbooks.py 的代码缺省映射，同样过滤电池、保留区域管理员动作
        fallback, _, notes = playbooks.plan({"id": "transformer_overload"}, event, runner, event.at)
        assert {action.entity_id for action in fallback} == {runner.world.index.zone_eid["admin"]}
        assert [action.command for action in fallback] == ["set_mode"]
        assert any("储能由分钟层接管" in item["reason"] for item in notes)
    finally:
        db.close()
