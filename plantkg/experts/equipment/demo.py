"""看效果：python -m plantkg.experts.equipment.demo configs/scenarios/factory_demo_day.yaml

跑第二幕的设备监测泳道：10:20 由故障注入器把 AC-2 的空压泄漏写进场景（信号形状取 MetroPT-3 官方
泄漏时段，按本厂额定参数缩放，性质 derived），10:30 剧本告警到达，专家在场景内研判、切换备用机、
隔离泄漏段并开维修工单；10:40 再走一条 D05 计量异常事件，只开计量检修工单、不切换设备。
最后跑极端场景“传感器说谎”（压力传感器卡死、候选收敛、短时启动备用机试探、改用推算值不停机），
并对 MetroPT-3 泄漏/同段对照两个小样本各研判一次。

终端打印方案表、注入记录、工单与空压能耗；`--json` 输出同样的结构化结果。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.core.contracts import Interval, LoadCategory, SimAction
from plantkg.world.scenario import ScenarioRunner, load_scenario
from plantkg.world.scenario.__main__ import build_world, open_db
from plantkg.world.scenario.script import Scenario, ScriptEvent
from plantkg.world.sensors import SensorFault, SensorFaultInjector
from plantkg.world.sim.faults import FaultInjector, Injection, load_calibration

from .expert import EquipmentExpert
from .metering import load_events, to_script_event

LEAK_AT, LIGHT_AT, METER_AT = "10:20", "10:18", "10:40"


def _pick(world: Any, equipment_type: str, preferred: str) -> str:
    codes = [e.code for e in world.site.equipment if e.type == equipment_type]
    if not codes:
        raise SystemExit(f"装配里没有 {equipment_type} 设备")
    return preferred if preferred in codes else codes[0]


def _with_faults(world: Any, injector: FaultInjector | None = None, sensor_faults: SensorFaultInjector | None = None) -> FaultInjector | None:
    """场景装配由 SIM_HOOK 提供；演示只替换其中的设备故障/传感器故障计划，保留观测层与真实专家登记。"""
    replaced = []
    for sim in world.simulators:
        name = getattr(sim, "name", "")
        if name == "equipment_faults" and injector is not None:
            replaced.append(injector)
        elif name == "sensor_faults" and sensor_faults is not None:
            replaced.append(sensor_faults)
        else:
            replaced.append(sim)
    world.simulators = replaced
    return injector


def _air_energy(trace: Any, start: datetime, end: datetime) -> float:
    return round(sum((minute.entry.load_breakdown or {}).get(LoadCategory.air_compressor, 0.0)
                     for minute in trace.ledger if start <= minute.entry.interval.start < end), 3)


def _metro_case(expert: EquipmentExpert, calibration: Any, *, leak: bool) -> dict:
    window = calibration.leak if leak else calibration.baseline
    baseline = calibration.baseline_for(window.samples[-1].at.hour)
    ids = {"AC-M": "entity://metro/air_compressor/AC-M", "AC-M2": "entity://metro/air_compressor/AC-M2",
           "air_system": "entity://metro/air_system/AIR"}
    state = {"ticket_id": "TK-metro-leak" if leak else "TK-metro-baseline", "trigger_at": window.samples[-1].at,
             "ids": ids, "entity_code": "AC-M", "entity_id": ids["AC-M"], "rated_kw": 110.0, "standby": "AC-M2",
             "knowledge": ["compressor_leak_rule"], "readings": window.reading_state(), "baseline": baseline,
             "evidence_refs": (f"metropt3:{window.name}",)}
    event = {"event_type": "compressor_pressure_drop", "entity_id": ids["AC-M"], "summary": "MetroPT-3 小样本研判"}
    return {"window": window.name, "nature": window.nature, "stats": window.stats.to_dict(), "baseline": baseline,
            "proposal": expert.propose(event, state).model_dump(mode="json")}


def _run_proposal(trace: Any, needle: str) -> dict:
    proposal = next(p for p in trace.proposals if p.expert == "equipment" and needle in (p.diagnosis or ""))
    run = next(r for r in trace.agent_runs if r.agent_name == "equipment" and proposal.proposal_id in r.proposal_ids)
    return {"proposal": proposal.model_dump(mode="json"),
            "agent_run": {"run_id": run.binding.run_id, "agent_run_id": run.agent_run_id, "latency_ms": run.timing.latency_ms,
                          "trigger_at": run.timing.trigger_at.isoformat(), "effective_at": run.timing.effective_at.isoformat(),
                          "source": run.source, "status": run.status},
            "actions": [a for a in trace.actions if a["source"].get("proposal_id") == proposal.proposal_id],
            "work_orders": [w.model_dump(mode="json") for w in trace.work_orders.values() if w.ticket_id == proposal.ticket_id]}


def _sensor_lie_case(expert: EquipmentExpert, *, with_rule: bool = False) -> dict:
    """极端场景“传感器说谎”：压力传感器卡死骤降，电流与产线正常；专家先试探再下结论。
    with_rule：09:30 先上传手册加入 compressor_leak_rule（主演示的顺序），规则也不能绕过观测核对。"""
    operator_actions = [{"at": "09:30", "kind": "upload", "payload": {"document": "空压机手册", "adds": ["compressor_leak_rule"]}}] if with_rule else []
    scenario = Scenario.model_validate({"operator_actions": operator_actions,
        "scenario_id": "sensor_lie_demo", "title": "传感器说谎", "nature": "synthetic", "date": "2026-07-01", "days": 1,
        "seed": 20260701, "initial": {"soc": 0.5},
        "events": [{"at": "10:04", "id": "sensor-lie", "type": "compressor_pressure_drop", "entity_id": "AC-2", "severity": 3,
                    "summary": "压力骤降，但电流与产线正常", "payload": {"effects": []}}]})
    world = build_world(scenario)
    faults = [SensorFault("AIR-HDR.P", "stuck", scenario.instant("09:40"), None, {"value": 5.6})]
    _with_faults(world, sensor_faults=SensorFaultInjector(faults))
    with TemporaryDirectory() as folder:
        db = open_db(Path(folder) / "sensor_lie_demo.db", world.site.plant_id)
        try:
            runner = ScenarioRunner(world, db, mode="evaluation")
            runner.start()
            start = runner.state.at  # 备用机 AC-1 开局停机，试探“启动备用机”才是真的由停转开
            runner.apply(SimAction(entity_id=world.ids()["AC-1"], command="stop", params={},
                                   interval=Interval(start=start, end=start + timedelta(minutes=1))))
            trace = runner.run_until(scenario.instant("10:05"))
            before = copy.deepcopy(runner.state.sims["sensors"]["latest"])
            first = next(p for p in trace.proposals if p.expert == "equipment" and "先短时启动备用机" in (p.diagnosis or ""))
            ticket = runner.tickets.tickets[first.ticket_id]
            trace = runner.run_until(scenario.instant("10:08"))
            after = copy.deepcopy(runner.state.sims["sensors"]["latest"])
            runner.dispatcher.dispatch(ticket, scenario.instant("10:08"), reason="trial")
            trace = runner.run_until(scenario.instant("10:10"))
            second = next(p for p in trace.proposals if p.expert == "equipment" and "判压力传感器卡死" in (p.diagnosis or ""))
            work_orders = [w.model_dump(mode="json") for w in trace.work_orders.values() if w.ticket_id == second.ticket_id]
            trial_action = next((a for a in trace.actions if a["source"].get("proposal_id") == first.proposal_id
                                 and a["action"].get("command") == "start"), None)
        finally:
            db.close()
    def brief(snapshot: dict) -> dict:
        readings = snapshot.get("readings") or {}
        return {"at": snapshot.get("at"), "alarms": snapshot.get("alarms"),
                "pressure": (readings.get("AIR-HDR.P") or {}).get("value"),
                "pressure_quality": (readings.get("AIR-HDR.P") or {}).get("quality"),
                "virtual_pressure": ((snapshot.get("virtuals") or {}).get("VS-AIR-P") or {}).get("value"),
                "candidates": [c["id"] for c in snapshot.get("candidates") or ()],
                "candidate_labels": [c["label"] for c in snapshot.get("candidates") or ()],
                "candidate_categories": [c["category"] for c in snapshot.get("candidates") or ()],
                "trial": snapshot.get("trial")}
    return {"event": {"at": "10:04", "type": "compressor_pressure_drop", "entity": "AC-2"},
            "before": brief(before), "after": brief(after), "first": first.model_dump(mode="json"),
            "second": second.model_dump(mode="json"), "trial_action": trial_action, "work_orders": work_orders}


def run(scenario_path: str | Path, *, until: str | datetime | None = None) -> dict:
    scenario = load_scenario(scenario_path)
    scenario.dispatch.setdefault("metering_anomaly", ["equipment"])
    world = build_world(scenario)
    metering_event = to_script_event(load_events()[0], plant_id=world.site.plant_id, at=METER_AT)
    scenario.events.append(ScriptEvent.model_validate(metering_event))
    expert = EquipmentExpert()
    injections = [Injection("compressor_leak", _pick(world, "air_compressor", "AC-2"), scenario.instant(LEAK_AT)),
                  Injection("lighting_circuit_loss", _pick(world, "lighting", "LT-A03"), scenario.instant(LIGHT_AT))]
    injector = FaultInjector(world.site, injections)
    _with_faults(world, injector)
    until_at = scenario.instant(until) if isinstance(until, str) else until
    with TemporaryDirectory() as folder:
        db = open_db(Path(folder) / "equipment_demo.db", world.site.plant_id)
        try:
            runner = ScenarioRunner(world, db, mode="evaluation")
            runner.start()
            trace = runner.run_until(until_at or scenario.instant("10:45"))
        finally:
            db.close()
    before, after = _air_energy(trace, scenario.instant("10:00"), scenario.instant("10:20")), \
        _air_energy(trace, scenario.instant("10:20"), scenario.instant("10:40"))
    calibration = load_calibration()
    return {
        "scenario": scenario.scenario_id, "site_tz": world.site.plant_tz, "nature": scenario.nature,
        "injections": injector.records,
        "leak": _run_proposal(trace, "空压机泄漏"),
        "metering": _run_proposal(trace, "数据异常"),
        "metering_event": metering_event,
        "air_energy": {"before_10_00_10_20_kwh": before, "after_10_20_10_40_kwh": after,
                       "delta_pct": round((after - before) / before * 100, 1) if before else None},
        "metropt3": {"leak": _metro_case(expert, calibration, leak=True), "baseline": _metro_case(expert, calibration, leak=False)},
        "sensor_lie": _sensor_lie_case(expert),
    }


def _clock(value: str | datetime, tz: ZoneInfo, *, seconds: bool = True) -> str:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    return at.astimezone(tz).strftime("%H:%M:%S" if seconds else "%H:%M")


def _short(eid: str) -> str:
    return eid.rsplit("/", 1)[-1] if "/" in eid else eid


def _latency(ms: int | None) -> str:
    return "—" if ms is None else ("<1 ms" if ms == 0 else f"{ms} ms")


def _actions(proposal: dict) -> str:
    parts = []
    for a in proposal.get("actions") or ():
        who = "human" if a["readiness"] == "needs_human" else "auto"
        extra = f"→{_short(a['params']['to'])}" if "to" in a.get("params", {}) else ""
        parts.append(f"{a['command']}{extra}({who})")
    return ", ".join(parts) or "—"


def _row(when: str, event: str, target: str, proposal: dict, *, note: str = "", work_order: str = "—", latency: str = "—") -> str:
    diagnosis = (proposal.get("diagnosis") or "").replace("\n", " ")
    if len(diagnosis) > 60:
        diagnosis = diagnosis[:59] + "…"
    return " | ".join((when, event, target, diagnosis, f"{proposal['confidence']:.2f}", _actions(proposal), work_order, latency, note)).rstrip(" |")


def report(data: dict) -> str:
    tz = ZoneInfo(data["site_tz"])
    lines = [f"# 设备监测专家（B22）· 第二幕泳道（{data['scenario']}，数据性质 {data['nature']}）"]
    for record in data["injections"]:
        lines.append(f"注入 {_clock(record['at'], tz)}  {record['entity']:<8} {record['kind']:<24}"
                     f"数据性质 {record['nature']}，来源 {record['source']}；{record['note']}")
    leak, metering = data["leak"], data["metering"]
    run = leak["agent_run"]
    leak_injection = next(r for r in data["injections"] if r["kind"] == "compressor_leak")
    lines += ["", f"过程：注入 {_clock(leak_injection['at'], tz)} → 告警/研判 {_clock(run['trigger_at'], tz)}"
                  f"（墙钟 {_latency(run['latency_ms'])}，仿真响应预算 1 分钟）→ 生效 {_clock(run['effective_at'], tz)}；"
                  f"事件单 {leak['proposal']['ticket_id']}，提案 {leak['proposal']['proposal_id']}，run_id {run['run_id']}",
              "", "## 方案表（时刻 | 事件 | 对象 | 诊断 | 置信度 | 动作 | 工单 | 研判耗时 | 备注）"]
    work_orders = leak["work_orders"] + metering["work_orders"]
    lines.append(_row(_clock(run["trigger_at"], tz, seconds=False), "compressor_pressure_drop", leak_injection["entity"], leak["proposal"],
                      work_order=leak["work_orders"][0]["work_order_id"] if leak["work_orders"] else "—",
                      latency=_latency(run["latency_ms"])))
    lines.append(_row(_clock(metering["agent_run"]["trigger_at"], tz, seconds=False), "metering_anomaly", str(data["metering_event"]["payload"]["meter"]), metering["proposal"],
                      work_order=metering["work_orders"][0]["work_order_id"] if metering["work_orders"] else "—",
                      latency=_latency(metering["agent_run"]["latency_ms"]), note="数据异常、设备正常，不切换设备"))
    for key, label in (("leak", "泄漏段"), ("baseline", "对照段")):
        case = data["metropt3"][key]
        lines.append(_row(label, "metropt3_sample", "AC-M", case["proposal"],
                          note=f"运转占比 {case['stats']['running_ratio']:.0%}，电流 {case['stats']['current_mean_a']:.2f} A，"
                               f"油温 {case['stats']['oil_start_c']:.0f}→{case['stats']['oil_end_c']:.0f} ℃，性质 {case['nature']}"))
    lines += ["", "## 工单"]
    for wo in work_orders:
        lines.append(f"{wo['work_order_id']}  {wo['status']}  {wo['reason']}  动作 {_actions({'actions': wo['actions']})}")
    lines += ["", "## 空压能耗（分钟账本 air_compressor 分项）",
              f"泄漏前 10:00～10:20 {data['air_energy']['before_10_00_10_20_kwh']} kWh；"
              f"泄漏后 10:20～10:40 {data['air_energy']['after_10_20_10_40_kwh']} kWh（{data['air_energy']['delta_pct']:+.1f}%）"]
    leak_m, base = data["metropt3"]["leak"], data["metropt3"]["baseline"]
    lines += ["", "## MetroPT-3 小样本（10 秒采样）",
              f"{'窗口':<22}{'运转占比':>8}{'电流均值A':>10}{'油温首末℃':>14}{'压力斜率bar/h':>14}  诊断",
              f"{leak_m['window']:<22}{leak_m['stats']['running_ratio']:>8.0%}{leak_m['stats']['current_mean_a']:>10.2f}"
              f"{leak_m['stats']['oil_start_c']:>7.0f}→{leak_m['stats']['oil_end_c']:<6.0f}{leak_m['stats']['pressure_drop_bar_per_h']:>14.2f}  "
              f"{leak_m['proposal']['diagnosis'].split('；')[0]}",
              f"{base['window']:<22}{base['stats']['running_ratio']:>8.0%}{base['stats']['current_mean_a']:>10.2f}"
              f"{base['stats']['oil_start_c']:>7.0f}→{base['stats']['oil_end_c']:<6.0f}{base['stats']['pressure_drop_bar_per_h']:>14.2f}  "
              f"{base['proposal']['diagnosis'].split('；')[0]}"]
    lie = data["sensor_lie"]
    lines += ["", "## 极端场景：传感器说谎（压力传感器卡死，压力读数骤降但电流与产线正常）",
              f"{'时刻':<10}{'压力读数':>8}{'质量':>8}{'虚拟压力':>10}  报警 / 候选",
              _obs_row(lie["before"], tz), _obs_row(lie["after"], tz)]
    trial = lie["after"].get("trial") or {}
    if trial:
        lines.append(f"试探：{_clock(trial.get('started_at'), tz)} 启动备用机 {_short((trial.get('action') or {}).get('entity_id', '—'))} "
                     f"{trial.get('window_min')} 分钟；压力读数变化 {trial.get('pressure_delta')} bar，"
                     f"虚拟传感器变化 {trial.get('virtual_delta')} bar，结论候选 {trial.get('verdict')}")
        lines.append("试探后的读数（新时间戳）：" + "，".join(
            f"{_clock(item['at'], tz, seconds=False)} 压力 {item['pressure']} / 推算 {item['virtual_pressure']}"
            for item in (trial.get("readings_at") or [])[-4:]))
    lines.append(f"试探提案：{_actions(lie['first'])}；{lie['first']['diagnosis']}")
    lines.append(f"结论：{_actions(lie['second'])}；{lie['second']['diagnosis']}")
    for wo in lie["work_orders"]:
        lines.append(f"{wo['work_order_id']}  {wo['status']}  {wo['reason']}  动作 {_actions({'actions': wo['actions']})}")
    return "\n".join(lines)


def _obs_row(item: dict, tz: ZoneInfo) -> str:
    pressure = "—" if item.get("pressure") is None else f"{item['pressure']:.2f}"
    virtual = "—" if item.get("virtual_pressure") is None else f"{item['virtual_pressure']:.2f}"
    quality = (item.get("pressure_quality") or {}).get("flag", "—")
    alarms = "、".join(item.get("alarms") or ()) or "无"
    labels = "、".join(item.get("candidate_labels") or ()) or "无"
    return f"{_clock(item['at'], tz):<10}{pressure:>8}{quality:>8}{virtual:>10}  {alarms} / {labels}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.experts.equipment.demo")
    parser.add_argument("scenario", help="剧本 YAML，如 configs/scenarios/factory_demo_day.yaml")
    parser.add_argument("--until", default="10:45", help="跑到剧本的哪个时刻（默认 10:45，覆盖第二幕）")
    parser.add_argument("--json", action="store_true", help="输出结构化结果而不是方案表")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    data = run(args.scenario, until=args.until)
    print(json.dumps(data, ensure_ascii=False, indent=1, default=str) if args.json else report(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
