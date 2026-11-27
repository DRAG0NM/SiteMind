"""看效果：电梯专家的方案表。

    python -m plantkg.experts.elevator.demo configs/scenarios/factory_demo_day.yaml
    python -m plantkg.experts.elevator.demo configs/scenarios/factory_demo_day.yaml --json

读剧本的 B18 装配与人流/物料曲线，跑 SimPy 群控仿真，再对剧本里的电梯事件调用
`ElevatorExpert.propose`，打印人梯/货梯策略对照与提案表。结果是 simulated：人流曲线与事件来自剧本，
“600 在册 + 200 访客”的早高峰放大与货梯压力样本是 synthetic，物理参数 configured。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import unicodedata
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from plantkg.experts.elevator import ElevatorExpert
from plantkg.experts.elevator.expert import congestion_metrics
from plantkg.world.scenario.__main__ import tariff_prices
from plantkg.world.scenario.inputs import SiteInputProvider
from plantkg.world.scenario.script import load_scenario
from plantkg.world.sim.elevator import (
    ElevatorConfig,
    FreightStrategy,
    FreightTask,
    PassengerResult,
    PassengerStrategy,
    build_demand,
    run_freight_sim,
    run_passenger_sim,
)

LOCAL = ZoneInfo("Asia/Shanghai")
MORNING_MINUTES = 30
MORNING_VISITORS = 200  # synthetic：早高峰除在册员工外的访客/承包商


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.experts.elevator.demo")
    parser.add_argument("scenario", help="剧本 YAML，例如 configs/scenarios/factory_demo_day.yaml")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    scene = load_scenario(args.scenario)
    prices, price_note = tariff_prices(scene)
    inputs = SiteInputProvider(scene, prices, price_note=price_note)
    config = ElevatorConfig.from_site(inputs.site)
    run_id = f"elevator-demo:{scene.scenario_id}:{scene.date}"
    staff = int(inputs.site.config["schedule"]["admin_staff"]) + MORNING_VISITORS

    demand = build_demand(config, "peak_up", minutes=MORNING_MINUTES, staff=staff)
    fixed = run_passenger_sim(config, demand, PassengerStrategy("fixed", "peak_up", return_floor=1))
    zone = run_passenger_sim(config, demand, PassengerStrategy("zone", "peak_up", return_floor=1))
    modes = {
        "午间双向（分区）": run_passenger_sim(config, build_demand(config, "lunch", minutes=60, staff=staff, seed=11),
                                              PassengerStrategy("zone", "lunch")),
        "晚高峰下行（分区）": run_passenger_sim(config, build_demand(config, "peak_down", minutes=60, staff=staff, seed=13),
                                                PassengerStrategy("zone", "peak_down")),
        "低谷少开 2 台": run_passenger_sim(config, build_demand(config, "off_peak", minutes=120, staff=staff, seed=17),
                                           PassengerStrategy("zone", "off_peak", active_cars=4)),
    }
    freight = _freight_results(config)
    expert = ElevatorExpert(config)
    proposals = _proposals(scene, inputs, expert, fixed, zone)
    payload = {
        "run_id": run_id, "scenario": scene.scenario_id, "date": scene.date, "nature": "simulated",
        "source": f"{scene.path} sha256={scene.sha256[:12]}", "price_note": price_note,
        "morning_peak": {"staff": staff, "minutes": MORNING_MINUTES, "fixed": asdict(fixed), "zone": asdict(zone)},
        "modes": {name: asdict(result) for name, result in modes.items()},
        "freight": {name: asdict(result) for name, result in freight.items()},
        "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"# 电梯专家方案（{scene.title} / {scene.scenario_id}）")
    print(f"运行 {run_id}，数据性质 simulated；来源 {scene.path}（sha256 {scene.sha256[:12]}）")
    print(f"装配：{len(config.passenger_cars)} 台人梯（低区 3 / 高区 3）、{len(config.freight_cars)} 台货梯，"
          f"行政楼 {config.floors} 层；早高峰按 {staff} 人次 / {MORNING_MINUTES} 分钟（600 在册 + 200 访客，synthetic）\n")
    _print_passenger_table([("固定派梯", fixed), ("分区派梯", zone)] + list(modes.items()))
    _print_freight_table(freight)
    _print_proposals(proposals)
    print("\n## 知识依据（synthetic 摘要，B26 接入原文）")
    for ref in sorted({ref for proposal in proposals for ref in proposal.evidence_refs}):
        print(f"- {ref}")
    print("\n复现：同一剧本、同一随机种子下结果一致；外部平台运行不在本命令里调用。")
    return 0


def _freight_results(config: ElevatorConfig) -> dict[str, object]:
    """压力样本（synthetic）：2 小时内 660 件物料，其中 60 件紧急；峰电 1～1.5 小时。"""
    rng = random.Random(11)
    tasks = [FreightTask(f"N{index}", rng.uniform(0, 2 * 3600), 1, rng.choice((2, 3, 4)), False) for index in range(600)]
    tasks += [FreightTask(f"U{index}", rng.uniform(0, 2 * 3600), 1, rng.choice((2, 3, 4)), True) for index in range(60)]
    common = {"passenger_peak_windows": ((0.0, 1800.0),), "peak_price_windows": ((3600.0, 5400.0),), "horizon_s": 4 * 3600}
    return {
        "先来先到": run_freight_sim(config, tuple(tasks), FreightStrategy("fifo"), **common),
        "优先级+避开人梯高峰+峰电合批": run_freight_sim(
            config, tuple(tasks), FreightStrategy("priority", avoid_passenger_peak=True, batch_at_peak=True), **common),
    }


def _proposals(scene, inputs: SiteInputProvider, expert: ElevatorExpert, fixed: PassengerResult, zone: PassengerResult):
    out = []
    for event in scene.expanded_events():
        if event.type not in ("elevator_congestion", "elevator_trapped"):
            continue
        item = event.model_dump(mode="json")
        item["at"] = scene.instant(event.at)
        item["ticket_id"] = f"ticket://{event.id or event.type}"
        if event.type == "elevator_congestion":
            item["entity_id"] = _group_eid(inputs, event.entity_id, "admin_low")
            at = scene.instant(event.at)
            state = {"confirmed": False, "price_period": _period(inputs, at), "price_cny_per_kwh": inputs.prices[inputs.slot_index[_slot(at)]],
                     "metrics": congestion_metrics(fixed, zone)}
        else:
            state = {"confirmed": False, "price_period": _period(inputs, scene.instant(event.at)), "metrics": {}}
        out.append(expert.propose(item, state))
    return out


def _group_eid(inputs: SiteInputProvider, entity_id: str | None, default: str) -> str:
    plant = inputs.site.plant_id
    if not entity_id:
        return f"entity://{plant}/elevator_group/{default}"
    if entity_id.startswith("entity://"):
        return entity_id
    if entity_id in {e.code for e in inputs.site.equipment}:
        return next(e.entity_id for e in inputs.site.equipment if e.code == entity_id)
    kind, _, code = entity_id.partition(":")
    if kind in ("elevator_group", "group"):
        return f"entity://{plant}/elevator_group/{code}"
    return entity_id


def _slot(at: datetime) -> datetime:
    return at - timedelta(minutes=at.minute % 15, seconds=at.second, microseconds=at.microsecond)


def _period(inputs: SiteInputProvider, at: datetime) -> str:
    try:
        return inputs.price_period(_slot(at))
    except KeyError:
        return "flat"


def _print_passenger_table(results: list[tuple[str, PassengerResult]]) -> None:
    print("## 人梯策略对照（simulated；候梯秒数含到末了仍在排队者按已等到末了计；拥堵 ≥20 人排队）")
    rows = [[name, result.mode, f"{result.served}/{result.arrivals}", result.unserved, f"{result.avg_wait_s:.0f}", f"{result.max_wait_s:.0f}",
             f"{result.congestion_min:.0f}", f"{result.trip_kwh:.1f}", f"{result.standby_kwh:.1f}", result.trips] for name, result in results]
    _table(["策略", "模式", "已服务/到达", "未服务", "平均候梯", "最长候梯", "拥堵分钟", "行程kWh", "待机kWh", "趟数"], rows)


def _print_freight_table(results: dict[str, object]) -> None:
    print("\n## 货梯策略对照（压力样本 synthetic：660 件 / 2 小时，其中 60 件紧急；峰电 1～1.5 小时）")
    rows = [[name, f"{result.done}/{result.tasks}", f"{result.avg_wait_min:.1f}", f"{result.urgent_avg_wait_min:.1f}",
             f"{result.max_wait_min:.1f}", result.deferred, result.batches, f"{result.energy_kwh:.1f}"]
            for name, result in results.items()]
    _table(["策略", "完成/任务", "平均排队min", "紧急平均min", "最长min", "推迟件数", "合批次数", "能耗kWh"], rows)
    fifo, priority = results["先来先到"], results["优先级+避开人梯高峰+峰电合批"]
    print(f"说明：优先级策略把 {priority.deferred} 件普通任务推迟到人梯高峰/峰电之后，平均排队变长是代价；"
          f"紧急件从 {fifo.urgent_avg_wait_min:.1f} 降到 {priority.urgent_avg_wait_min:.1f} 分钟，"
          f"能耗从 {fifo.energy_kwh:.1f} 降到 {priority.energy_kwh:.1f} kWh。")


def _print_proposals(proposals) -> None:
    print("\n## 专家提案表（电梯专家，动作以确定性提案为准）")
    rows = []
    for proposal in proposals:
        first = True
        for action in proposal.actions or ():
            rows.append([
                (proposal.ticket_id or "").removeprefix("ticket://") if first else "",
                action.interval.start.astimezone(LOCAL).strftime("%m-%d %H:%M"),
                _short(action.entity_id), action.command + (f" {json.dumps(action.params, ensure_ascii=False)}" if action.params else ""),
                action.readiness, f"{proposal.cost.cny:.1f}", f"{proposal.confidence:.2f}",
                "、".join(proposal.safety_tags) or "-", proposal.evidence_refs[0] if first and proposal.evidence_refs else "",
                (proposal.diagnosis or "") if first else "",
            ])
            first = False
    _table(["事件单", "时刻", "对象", "动作", "就绪", "代价元", "置信度", "安全标记", "依据", "诊断"], rows)


def _short(entity_id: str | None) -> str:
    if not entity_id:
        return "-"
    return entity_id.split("/", 3)[-1] if entity_id.startswith("entity://") else entity_id


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _table(headers: list[str], rows: list[list[object]]) -> None:
    data = [[str(cell) for cell in row] for row in rows]
    widths = [max(_width(str(header)), *(_width(row[index]) for row in data)) for index, header in enumerate(headers)]
    print("  ".join(_pad(str(header), width) for header, width in zip(headers, widths)))
    for row in data:
        print("  ".join(_pad(cell, width) for cell, width in zip(row, widths)))


if __name__ == "__main__":
    raise SystemExit(main())
