"""看效果（B19 能源专家）：

    python -m plantkg.experts.energy.demo configs/scenarios/factory_demo_day.yaml
    python -m plantkg.experts.energy.demo configs/scenarios/factory_demo_day.yaml --from 07:30 --until 08:30

第一条打印日前方案表（逐 15 分钟：负荷、光伏、充放、购电目标线、SOC、电价、电费）、B08 计划账本摘要、
门控决定与相对基线的省钱；第二条在 07:30 起跑实时层（分钟跟踪 + 每整格滚动重解），08:10 发布一次知识
版本分叉，打印 08:00 / 08:15 两槽的计划与实际对比、08:05 前后逐分钟账本、储能放电省下的钱与重解记录。
默认数据库 runtime/energy_expert.db，JSON 写到 runtime/energy_day.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from plantkg.experts.energy.assembly import DEFAULT_DB
from plantkg.experts.energy.day import DayRun, run_day
from plantkg.world.scenario.script import load_scenario

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_JSON = ROOT / "runtime" / "energy_day.json"


def _local(scenario, text: str) -> datetime:
    return scenario.instant(text)


def plan_rows(day: DayRun) -> list[dict]:
    rows = []
    for entry in day.plan.ledger.entries:
        rows.append({"start": entry.interval.start.isoformat(), "load_kw": round(entry.load_kwh * 4, 1),
                     "pv_kw": round(entry.pv_available_kwh * 4, 1), "charge_kw": round(entry.charge_kwh * 4, 1),
                     "discharge_kw": round(entry.discharge_kwh * 4, 1), "grid_import_kw": round(entry.grid_import_kwh * 4, 1),
                     "soc_end": round(entry.soc_end, 4), "price_cny_per_kwh": entry.price_cny_per_kwh,
                     "cost_cny": None if entry.cost_cny is None else round(entry.cost_cny, 3)})
    return rows


def print_plan(day: DayRun) -> None:
    tz = day.scenario.tz
    print("## 日前方案（B09 共享规划，B08 计划账本）\n")
    print(f"{'时刻':<6}{'负荷kW':>8}{'光伏kW':>8}{'充电kW':>8}{'放电kW':>8}{'购电kW':>8}{'SOC':>7}{'电价':>7}{'电费元':>9}")
    for row in plan_rows(day):
        at = datetime.fromisoformat(row["start"]).astimezone(tz).strftime("%H:%M")
        print(f"{at:<6}{row['load_kw']:>8.0f}{row['pv_kw']:>8.0f}{row['charge_kw']:>8.0f}{row['discharge_kw']:>8.0f}"
              f"{row['grid_import_kw']:>8.0f}{row['soc_end']:>7.3f}{row['price_cny_per_kwh']:>7.3f}{row['cost_cny']:>9.2f}")
    ledger = day.plan.ledger
    print(f"\n计划账本：{ledger.ledger_id}  balanced={ledger.balanced}  {len(ledger.entries)} 时段  "
          f"总电费 {ledger.total_cost_cny:.2f} 元  峰值购电 {ledger.peak_import_kw:.0f} kW  "
          f"期末 SOC {ledger.entries[-1].soc_end:.3f}（目标 {day.plan.end_target_kwh:.0f} kWh）")
    print(f"求解：{day.plan.result.solver_status}  {day.plan.elapsed_s:.2f} s  动作 {len(day.plan.plan.actions)} 条  "
          f"计划 {day.plan.plan.plan_id}")


def print_gate(day: DayRun) -> None:
    decision = day.decision
    print(f"\n门控（B15）：{decision.outcome}  计划 {day.executable.plan_id}@{day.executable.revision}  "
          f"策略 {decision.policy_version}")
    for reason in decision.reasons:
        print(f"  - {reason}")
    print(f"  门控后才执行：executable={day.executable.status}  "
          f"执行依据已落库（回执 {day.plan.receipt.sha256[:12]}…）")


def print_execution(day: DayRun, start: datetime, end: datetime) -> None:
    tz = day.scenario.tz
    trace = day.trace
    loc = lambda t: t.astimezone(tz).strftime("%H:%M")  # noqa: E731
    chain = " → ".join(f"{r.run_id}(v{r.knowledge_version}{'，' + loc(r.forked_at) + ' 分叉' if r.forked_at else ''})"
                       for r in trace.runs)
    print(f"\n## 实时执行（B17 场景世界，{loc(start)}～{loc(end)}）")
    print(f"运行链：{chain}")
    print("数据性质：仿真场景（外部工厂实测缩放底座 + 仿真分项）；分钟账本来源见 day.json 的 sources")
    plan_slots = {e.interval.start: e for e in day.plan.ledger.entries}
    print(f"\n{'槽':<6}{'实际负荷kWh':>12}{'计划充电':>9}{'实际充电':>9}{'计划放电':>9}{'实际放电':>9}"
          f"{'购电kWh':>9}{'分钟峰值kW':>11}{'平衡':>5}")
    for slot in day.slot_summaries(start, end):
        planned = plan_slots.get(slot.interval.start)
        charge_plan = round(planned.charge_kwh, 1) if planned else 0.0
        discharge_plan = round(planned.discharge_kwh, 1) if planned else 0.0
        print(f"{loc(slot.interval.start):<6}{slot.load_kwh:>12.1f}{charge_plan:>9.1f}{slot.charge_kwh:>9.1f}"
              f"{discharge_plan:>9.1f}{slot.discharge_kwh:>9.1f}{slot.grid_import_kwh:>9.1f}{slot.peak_minute_kw:>11.1f}"
              f"{'是' if slot.minutes_balanced else '否':>5}")
    print("\n08:05 前后逐分钟（事件 08:05，响应预算 1 分钟，08:06 起分钟层放电）：")
    for m in day.minutes(start + timedelta(minutes=32), start + timedelta(minutes=40)):
        e = m.entry
        print(f"  {loc(e.interval.start)}  负荷 {e.load_kwh * 60:>7.0f} kW  放电 {e.discharge_kwh * 60:>6.0f} kW  "
              f"购电 {e.grid_import_kwh * 60:>6.0f} kW  SOC {e.soc_end:.3f}  {'平衡' if m.balanced else '不平'}")
    cost = day.window_cost(start, end)
    print(f"\n窗口购电 {cost['actual_cny']:.2f} 元；不充不放基线 {cost['no_storage_cny']:.2f} 元；"
          f"省 {cost['saved_cny']:.2f} 元（放电 {cost['discharge_kwh']:.1f} kWh，充电 {cost['charge_kwh']:.1f} kWh）")
    print(f"\n整格滚动重解（初始 SOC 取分钟账本实际值；期末下界固定 {day.end_target_kwh:.0f} kWh）：")
    for replan in day.replans:
        print(f"  {loc(replan.at)}  SOC {replan.initial_soc:.3f}（{replan.energy_kwh:.1f} kWh）  "
              f"{replan.solver_status} {replan.elapsed_s:.2f} s  期末下界 {replan.end_floor_kwh:.0f} kWh  {replan.plan_id}")
    if len(trace.runs) > 1:
        child = trace.runs[-1]
        snapshot = day.db.store.snapshot(child.run_id, child.forked_at) if child.forked_at else None
        energy = snapshot["pack_states"]["storage"]["energy_kwh"] if snapshot else None
        print(f"\n知识版本分叉：{child.forked_from} → {child.run_id}，forked_at={loc(child.forked_at)}，"
              f"子运行快照储能 {energy:.3f} kWh")
    minutes = day.minutes(start, end)
    print_checks(day, start, end)
    print(f"\n分钟账本：{len(minutes)} 条，全部平衡={all(m.balanced for m in minutes)}，"
          f"槽问题 {len(trace.slot_problems)}，容量告警 {capacity_alarm_count(trace)} 次")


def print_checks(day: DayRun, start: datetime, end: datetime) -> None:
    check = day.verify_soc(start, end)
    print(f"\n独立核对（SOC 变化 == 充*eta_c - 放/eta_d、SOC 在上下界内、功率不超上限）：{'通过' if check['ok'] else '未通过'}"
          f"（{check['checked_minutes']} 分钟）")
    for problem in check["problems"]:
        print(f"  - {problem}")
    ending = day.end_report()
    if ending["final_energy_kwh"] is not None and ending["reached_day_end"]:
        print(f"期末储能 {ending['final_energy_kwh']:.1f} kWh，目标 {ending['end_target_kwh']:.0f} kWh，缺口 {ending['end_gap_kwh']:.1f} kWh")
    for failure in ending["replan_failures"]:
        print(f"  重解不可行 {failure['at']}：期末缺口 {failure['end_gap_kwh']:.1f} kWh（{failure['error']}）")


def capacity_alarm_count(trace) -> int:
    return sum(1 for m in trace.ledger if m.capacity_violations)


def export(day: DayRun, start: datetime | None, end: datetime | None) -> dict:
    data = {"scenario_id": day.scenario.scenario_id, "nature_note": "仿真场景（外部工厂实测缩放底座 + 仿真分项）",
            "day": day.day_start.isoformat(), "plan": {"plan_id": day.plan.plan.plan_id,
            "solver_status": day.plan.plan.solver_status, "rows": plan_rows(day),
            "ledger": json.loads(day.plan.ledger.model_dump_json())},
            "gate": {"outcome": day.decision.outcome, "reasons": list(day.decision.reasons),
                     "executable": day.executable.model_dump(mode="json")},
            "sources": day.inputs.source_table()}
    if day.trace is not None and start is not None and end is not None:
        data["execution"] = {
            "runs": day.run_chain(),
            "slots": [json.loads(s.model_dump_json()) for s in day.slot_summaries(start, end)],
            "minutes": [m.to_json() for m in day.minutes(start, end)],
            "replans": [{"at": r.at.isoformat(), "energy_kwh": r.energy_kwh, "plan_id": r.plan_id,
                         "solver_status": r.solver_status, "elapsed_s": r.elapsed_s,
                         "end_target_kwh": r.end_target_kwh, "end_floor_kwh": r.end_floor_kwh,
                         "initial_soc": r.initial_soc} for r in day.replans],
            "window_cost": day.window_cost(start, end),
            "balanced": all(m.balanced for m in day.minutes(start, end)),
            "soc_check": day.verify_soc(start, end),
            "end": day.end_report(),
        }
    return data


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m plantkg.experts.energy.demo", description="B19 能源专家：日前计划与实时削峰")
    p.add_argument("scenario", help="场景剧本 YAML")
    p.add_argument("--date", default=None, help="厂区当地日期，默认剧本首日")
    p.add_argument("--from", dest="start", default=None, help="实时窗口起点（剧本时刻写法，如 07:30）")
    p.add_argument("--until", default=None, help="实时窗口终点（如 08:30）")
    p.add_argument("--fork-at", default="08:10", help="知识版本分叉时刻，传 none 关闭")
    p.add_argument("--db", default=None, help=f"SQLite 路径，默认 {DEFAULT_DB}；同一数据库同时只能一个进程写")
    p.add_argument("--out", default=None, help=f"JSON 输出路径，默认 {DEFAULT_JSON}")
    p.add_argument("--json", action="store_true", help="只打印 JSON")
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    scenario = load_scenario(a.scenario)
    try:
        day = run_day(scenario=scenario, date=a.date, db_path=a.db or DEFAULT_DB)
    except RuntimeError as exc:
        if "already owns" in str(exc):
            sys.exit(f"{exc}：另一个进程正在用这个数据库，加 --db 指定别的路径")
        raise
    try:
        start = _local(scenario, a.start) if a.start else None
        end = _local(scenario, a.until) if a.until else None
        fork = None
        if a.fork_at.lower() not in ("none", "no", "off", ""):
            fork = _local(scenario, a.fork_at)
            if start is not None and (fork < start or (end is not None and fork >= end)):
                fork = None
        windowed = end is not None
        if windowed:
            day.execute(end, start_at=start, fork_at=fork)
        else:  # 不给窗口就整天跑一遍，只为给出独立核对与期末储能
            start, end = day.day_start, day.day_end
            day.execute(fork_at=fork)
        data = export(day, start, end)
        out = Path(a.out) if a.out else DEFAULT_JSON
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        if a.json:
            print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
        else:
            print_plan(day)
            print_gate(day)
            if windowed:
                print_execution(day, start, end)
            else:
                print_checks(day, start, end)
                print("\n提示：加 --from 07:30 --until 08:30 看实时闭环（分钟跟踪、滚动重解、知识版本分叉）。")
            print(f"\nJSON 已写到 {out}")
    finally:
        day.assembly.close()


if __name__ == "__main__":
    main()
