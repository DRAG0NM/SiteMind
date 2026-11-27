"""B47 看效果：第一幕重大损失立即开单 + 预案保护动作，第三幕困人工单确认后协同处置。

    python -m plantkg.agent.execution_policy.demo [configs/scenarios/factory_demo_day.yaml]

数据性质：剧本 synthetic；重大损失线 10000 元、预案动作与广播文本是演示用 configured/synthetic 值。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from plantkg.world.scenario.__main__ import build_world
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry, pack_registry
from plantkg.world.scenario.script import ROOT, Scenario, load_scenario

DEMO_LOSS_LINE_CNY = 10_000.0  # 演示用：08:05 突增估算损失 12000 元，超过此线即按重大损失开单
DEFAULT_DB = ROOT / "runtime" / "b47-demo.db"
TASK_CARD_THIRD_ACT = "第三幕剧本"
TZ = None  # 由剧本的 site 时区给出


def _scenario(path: str | Path) -> Scenario:
    scenario = load_scenario(path)
    return scenario.model_copy(update={"policy": {**scenario.policy, "major_loss_cny": DEMO_LOSS_LINE_CNY}})


def _scenario_path(value: str) -> Path | str:
    if value == TASK_CARD_THIRD_ACT:
        return ROOT / "configs/scenarios/factory_demo_day.yaml"
    return value


def _open(path: str | Path, name: str, db_dir: Path | None = None) -> tuple[Any, Any, Any, Any]:
    scenario = _scenario(path)
    world = build_world(scenario)
    db_path = Path(db_dir or DEFAULT_DB.parent) / f"b47-demo-{name}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    writer = SQLiteWriter(db_path)
    MigrationRunner(writer).apply()
    db = ScenarioDB.from_writer(writer, world.site.plant_id)
    registry = default_registry(plugins=pack_registry())
    runner = ScenarioRunner(world, db, registry=registry, mode="evaluation", group="coordinated")
    runner.start()
    return world, db, registry, runner


def _service(registry: Any) -> Any:
    hooks = registry.hooks_for("router.urgent")
    return hooks[0].__self__ if hooks else None


def _slot(trace: Any, scenario: Scenario, hhmm: str) -> Any:
    at = scenario.instant(hhmm)
    return next(s for s in trace.slots if s.interval.start == at)


def _fmt_slot(label: str, slot: Any) -> str:
    cost = "-" if slot.cost_cny is None else f"{slot.cost_cny:.2f}"
    return (f"{label} 放电 {slot.discharge_kwh:.1f} kWh｜购电 {slot.grid_import_kwh:.1f} kWh｜"
            f"SOC 末 {slot.soc_end:.3f}｜电费 {cost} 元")


def first_act(path: str | Path, db_dir: Path | None = None) -> dict[str, Any]:
    world, main_db, registry, runner = _open(path, "main", db_dir)
    service = _service(registry)
    try:
        trace = runner.run_until(_scenario(path).instant("08:31"))
        router = runner.dispatcher.router
        event = next(e for e in trace.events if e.event_id == "act1-spike")
        ticket_id = router.store.event_ticket(event.event_id)
        ticket = router.store.ticket(ticket_id)
        estimate = router.store.severity_of(ticket_id) or {}
        tasks = service.store.tasks(ticket_id)
        decision = next((d for d in trace.decisions if d.ticket_id == ticket_id), None)
        actions = [a for a in trace.actions if a.get("source", {}).get("ticket_id") == ticket_id]
        executed = [a for a in actions if a["source"].get("b47") == "protection"]
        queued = [a for a in actions if a["source"].get("proposal_id")]
        slots = {label: _slot(trace, _scenario(path), label) for label in ("08:00", "08:15")}
        result = {"ticket": ticket, "estimate": estimate, "tasks": tasks, "event": event,
                  "decision": decision, "executed": executed, "queued": queued, "slots": slots}
        print("=== 第一幕：08:05 突增（D55 重大损失入口）===")
        print(f"严重程度：停产={estimate.get('production_stop')}，未保护损失 {estimate.get('loss_cny')} 元；"
              f"重大损失线 {DEMO_LOSS_LINE_CNY:.0f} 元（演示配置）")
        print(f"事件单 {ticket_id}：通道 {router.store.channel_of(ticket_id)}，第 {ticket.round} 轮，"
              f"专家 {','.join(ticket.experts) or '-'}")
        order = next((t for t in tasks if t.kind == "work_order"), None)
        if order is not None:
            print(f"操作工单 {order.work_order_id}（{order.status}）：{order.reason}")
            print(f"工单动作 {len(order.actions)} 个：" + "；".join(
                f"{a.command}@{a.entity_id.rsplit('/', 1)[-1]}" for a in order.actions))
        if executed:
            print("预案保护动作（已自动执行）：" + "；".join(
                f"{a['action']['command']}@{a['action']['entity_id'].rsplit('/', 1)[-1]}"
                f" {a['action']['interval']['start'][11:16]} 起" for a in executed))
        if decision is not None:
            print(f"主 Agent 裁决（第 {decision.layer} 层）：{decision.result.outcome}，"
                  f"选中 {', '.join(decision.result.selected_proposal_ids)}；{decision.result.summary}")
        if queued:
            print("专家提案动作（经 B47 执行）：" + "；".join(
                f"{a['action']['command']}@{a['action']['entity_id'].rsplit('/', 1)[-1]}" for a in queued))
        for label, slot in slots.items():
            print(_fmt_slot(f"槽 {label}：", slot))
        return result | {"main": (main_db, registry, runner, trace)}
    except BaseException:
        main_db.close()
        raise


def control_act(path: str | Path, db_dir: Path | None = None) -> dict[str, Any]:
    """对照：同一剧本、同一策略，只关掉预案保护动作（工单照开、裁决照跑，动作不自动执行）。"""
    world, db, registry, runner = _open(path, "control", db_dir)
    service = _service(registry)
    service.protections = False
    try:
        trace = runner.run_until(_scenario(path).instant("08:31"))
        scene = _scenario(path)
        return {label: _slot(trace, scene, label) for label in ("08:00", "08:15")}
    finally:
        db.close()


def third_act(path: str | Path, main: tuple[Any, Any, Any, Any]) -> dict[str, Any]:
    db, registry, runner, _ = main
    service = _service(registry)
    scenario = _scenario(path)
    before = [a for a in runner.trace.actions if a.get("source", {}).get("ticket_id", "").endswith("TK-0011")]
    trace = runner.run_until(scenario.instant("14:40"))
    router = runner.dispatcher.router
    event = next(e for e in trace.events if e.event_id == "act3-trapped")
    ticket_id = router.store.event_ticket(event.event_id)
    tasks = service.store.tasks(ticket_id)
    after = [a for a in trace.actions if a.get("source", {}).get("ticket_id") == ticket_id]
    done = next((t for t in tasks if t.kind == "work_order"), None)
    print("\n=== 第三幕：14:20 困人（安全标记，工单确认后协同处置）===")
    assert ticket_id == "ticket://TK-0011"
    print(f"事件单 {ticket_id}：通道 {router.store.channel_of(ticket_id)}；确认前已执行 {len(before)} 个动作")
    if done is not None:
        print(f"操作工单 {done.work_order_id}：{done.status}；{done.reason}")
        print("协同处置动作：" + "；".join(
            f"{a.command}@{a.entity_id.rsplit('/', 1)[-1]}" for a in done.actions))
    note = next((n for n in runner.trace.notes if n.get("kind") == "b47_work_order_done"
                 and n.get("ticket_id") == ticket_id), None)
    if note is not None:
        print(f"两段耗时：事件→工单 {note['event_to_order_min']:.0f} 分钟，"
              f"工单→处置完成 {note['order_to_done_min']:.0f} 分钟；确认后执行 {note['executed']} 个动作")
    print("确认后仿真执行的动作：" + ("；".join(
        f"{a['action']['command']}@{a['action']['entity_id'].rsplit('/', 1)[-1]}" for a in after) or "无"))
    return {"ticket": ticket_id, "task": done, "note": note, "before": before, "after": after}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.agent.execution_policy.demo")
    parser.add_argument("scenario", nargs="?", default=str(ROOT / "configs/scenarios/factory_demo_day.yaml"))
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    scenario_path = _scenario_path(args.scenario)
    first = first_act(scenario_path)
    control = control_act(scenario_path)
    print("\n对照（关掉预案保护动作）：")
    changed = False
    dearer: list[str] = []
    for label in ("08:00", "08:15"):
        a, b = first["slots"][label], control[label]
        changed = changed or a.discharge_kwh != b.discharge_kwh
        if a.cost_cny >= b.cost_cny:
            dearer.append(label)
        print(_fmt_slot(f"槽 {label}（分级执行）：", a))
        print(_fmt_slot(f"槽 {label}（对照）：  ", b))
    third = third_act(scenario_path, first["main"])
    main_db = first["main"][0]
    main_db.close()
    warn = (f"注意：{'、'.join(dearer)} 槽电费对照已不成立（#258 统一窗口后对照组也会放电，这些槽主运行电费不低于对照，叙事待重写）；"
            if dearer else "")
    passed = (changed and first["ticket"].status in ("resolved", "waiting")
              and third["task"] is not None and third["task"].status == "done" and not third["before"])
    print(f"\n结论：{'达到验收' if passed else '未达到验收'}"
          f"（预案保护动作改变两槽：{changed}；"
          f"{warn}"
          f"困人确认前未自动执行：{not third['before']}；"
          f"工单完成：{third['task'] is not None and third['task'].status == 'done'}）")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())




