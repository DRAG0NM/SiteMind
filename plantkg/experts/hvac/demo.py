"""看效果（B21）：

    python -m plantkg.experts.hvac.demo configs/scenarios/factory_demo_day.yaml [--until 20:00]

跑剧本第一天的仿真（B17 的 ScenarioWorld 默认装配：暖通领域包经 SCENARIO_HOOK 把 ThermalSim 换进去，
逐分钟走 ScenarioWorld.advance 与分钟账本）：打印各区温度曲线、暖通能耗与越界分钟；
再取剧本里的突增事件，让暖通专家提一次错峰方案，按同一输入重跑一遍并对比峰段负荷、温度与电耗。

输入：B18 工厂装配的负荷与人流（derived/synthetic）、PVGIS 气温（derived）；电价用平段演示值，只作输入。
输出：温度轨迹、功率与电耗是本模型的仿真结果（simulated），不是实测；数据性质随每条结果注明。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from plantkg.core.contracts import LoadCategory, SimAction
from plantkg.core.ledger import LedgerMeta
from plantkg.world.scenario.inputs import SiteInputProvider
from plantkg.world.scenario.runner import ScenarioWorld
from plantkg.world.scenario.script import Scenario, load_scenario
from plantkg.world.scenario.sims import event_effects
from plantkg.world.sim.thermal import ThermalSim

from .expert import HvacExpert, _event_minutes

DEFAULT_SCENARIO = "configs/scenarios/factory_demo_day.yaml"
PRICE_CNY_PER_KWH = 0.65  # 平段演示价，只用于把让出的负荷折成元
EVENT_ORDER = {"load_spike": 0, "sim_limit": 1, "heat_wave": 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.experts.hvac.demo", description="暖通专家：温度曲线与一次错峰")
    parser.add_argument("scenario", nargs="?", default=DEFAULT_SCENARIO, help="剧本 YAML")
    parser.add_argument("--until", default=None, help="只跑到厂区当地这个时刻（如 20:00），默认跑满第一天")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    scenario = load_scenario(Path(args.scenario))
    world = build_world(scenario)
    day = scenario.start + timedelta(hours=24)
    end = min(day, scenario.instant(args.until)) if args.until else day
    tz = ZoneInfo(scenario.site_tz)

    print(f"# 暖通专家 {scenario.scenario_id}（{scenario.title}）：区域温度、预冷与错峰")
    print(f"运行区间 {scenario.start.astimezone(tz):%Y-%m-%d %H:%M} ～ {end.astimezone(tz):%Y-%m-%d %H:%M}（{scenario.site_tz}）"
          f"；输入性质 {scenario.nature}，温度与电耗为 simulated")
    print(f"来源：剧本 sha256 {scenario.sha256[:16]}…，输入 provider {world.inputs.provider_id}"
          f"（本命令不写运行存储，run_id 由 B17 的 ScenarioRunner 在正式运行里分配）\n")

    event, effects, at = pick_event(scenario, world, end)
    if event is None:
        print("剧本里没有可响应的事件（load_spike / sim_limit / heat_wave），只打印温度曲线。")
        _print_curve(run_day(world, end)[0], tz)
        return 0

    # 两次运行注入同一事件，只有暖通动作不同
    baseline, base_thermal = run_day(world, end, effects=effects)
    row = next(r for r in baseline if r["at"] == at)
    proposal = HvacExpert().propose(event, {"at": at, "outdoor_c": row["outdoor_c"], "zones": row["zones"]})
    _print_proposal(proposal, tz)

    begin, finish, what = _compare_window(proposal, at)
    shifted, thermal = run_day(world, end, actions=proposal.actions, effects=effects)
    _print_curve(shifted, tz)
    _print_compare(baseline, shifted, begin, finish, what, tz, (at, at + timedelta(minutes=_event_minutes(event) or 15)),
                   base_thermal.get("comfort_minutes", {}), thermal.get("comfort_minutes", {}))
    _print_notes(thermal, begin, finish, tz)
    return 0


# ---------------------------------------------------------------- 世界与推进


def build_world(scenario: Scenario) -> ScenarioWorld:
    inputs = SiteInputProvider(scenario, lambda slots: [(PRICE_CNY_PER_KWH, "flat") for _ in slots],
                               price_note={"source": "demo-flat", "note": "平段演示价，只作输入"})
    return ScenarioWorld.build(scenario, inputs)


def run_day(world: ScenarioWorld, end: datetime, *, actions=(), effects=()) -> tuple[list[dict], dict]:
    """用 ScenarioWorld.advance 逐分钟推进到 end（仿真器 → 分钟账本），不写运行存储。
    effects 为 [(时刻, 事件对世界的影响)]，两次运行注入同样的影响，保证只有专家动作不同。"""
    thermal = next(s for s in world.simulators if isinstance(s, ThermalSim))
    state, energy = world.initial_state()
    state.sims["actions"] = [{"action": SimAction(entity_id=a.entity_id, command=a.command, params=a.params,
                                                  interval=a.interval).model_dump(mode="json"), "source": {"expert": "hvac"}}
                             for a in actions]
    meta = LedgerMeta(ledger_id="demo:hvac", kind="execution", run_id="demo:hvac", plan_id="demo:hvac", plan_revision=0,
                      version_manifest_id="demo:hvac", source_refs=(world.inputs.provider_id,))
    pending = sorted(effects, key=lambda item: item[0])
    rows = []
    while state.at < end:
        while pending and pending[0][0] <= state.at:
            state.effects.extend(pending.pop(0)[1])
        t = state.at
        record, energy = world.advance(state, energy, meta)
        view = thermal.situation(state)
        entry = record.entry
        rows.append({"at": t, "load_kw": round(entry.load_kwh * 60, 1),
                     "hvac_kw": round((entry.load_breakdown or {}).get(LoadCategory.hvac, 0.0) * 60, 1),
                     "outdoor_c": view["outdoor_c"], "zones": view["zones"]})
    return rows, state.sims["thermal"]


def pick_event(scenario: Scenario, world: ScenarioWorld, end: datetime) -> tuple[dict | None, list, datetime | None]:
    """挑一个事件：优先负荷突增，其次容量告警、热浪；影响按 B17 的 event_effects 生成。"""
    usable = [e for e in scenario.events if e.severity > 0 and scenario.instant(e.at) < end]
    if not usable:
        return None, [], None
    chosen = min(usable, key=lambda e: (EVENT_ORDER.get(e.type, 9), scenario.instant(e.at)))
    at = scenario.instant(chosen.at)
    entity = world.index.eid(chosen.entity_id) if chosen.entity_id else None
    effects = [(at, event_effects(chosen.type, entity, chosen.payload, at, world.index))]
    event = {"event_id": chosen.id or chosen.type, "at": at.isoformat(), "event_type": chosen.type, "entity_id": entity,
             "severity": chosen.severity, "summary": chosen.summary, "payload": chosen.payload}
    return event, effects, at


def _window_minutes(proposal) -> int:
    return max((int((a.interval.end - a.interval.start).total_seconds() // 60) for a in proposal.actions), default=15)


def _compare_window(proposal, at: datetime) -> tuple[datetime, datetime, str]:
    """推迟预冷看动作窗口（让出的容量）；预冷看动作之后两小时（冷量已提前买进去，峰段少用）。"""
    minutes = max(1, _window_minutes(proposal))
    modes = {a.params.get("mode") for a in proposal.actions}
    if modes == {"precool"}:
        begin = at + timedelta(minutes=minutes)
        return begin, begin + timedelta(hours=2), "预冷之后的峰段"
    return at, at + timedelta(minutes=minutes), "错峰窗口"


# ---------------------------------------------------------------- 打印


def _print_proposal(proposal, tz: ZoneInfo) -> None:
    print("## 方案表（暖通专家对事件单的提案）")
    print(f"诊断：{proposal.diagnosis}")
    print(f"置信度 {proposal.confidence:.2f}；代价 cny {proposal.cost.cny:+.1f} 元（负数为省）、"
          f"温度越界 {proposal.cost.comfort_min} 分钟")
    if not proposal.actions:
        print("动作：无（专家认为不需要或没有可让容量的区域）")
        return
    print(f"{'#':<3}{'对象':<34}{'命令':<14}{'起':<10}{'止':<10}{'参数'}")
    for i, action in enumerate(proposal.actions, 1):
        start, finish = action.interval.start.astimezone(tz), action.interval.end.astimezone(tz)
        print(f"{i:<3}{_short(action.entity_id):<34}{action.command:<14}{start:%H:%M}    {finish:%H:%M}    "
              f"{action.params or ''}（{action.readiness}）")
    print()


def _print_curve(rows: list[dict], tz: ZoneInfo) -> None:
    zones = list(rows[0]["zones"])
    names = {"production": "车间", "admin": "行政", "cleanroom": "洁净", "datacenter": "机房"}
    head = f"{'时刻':<6}{'室外℃':>7}{'全厂kW':>9}{'暖通kW':>8}" + "".join(f"{names.get(z, z) + '℃':>8}" for z in zones)
    print("## 各区温度与负荷（每 30 分钟，simulated）")
    print(head)
    for row in rows:
        local = row["at"].astimezone(tz)
        if local.minute not in (0, 30) or not 5 <= local.hour < 21:
            continue
        line = f"{local:%H:%M}  {row['outdoor_c']:>7.1f}{row['load_kw']:>9.0f}{row['hvac_kw']:>8.0f}"
        line += "".join(f"{row['zones'][z]['temperature']:>8.2f}" for z in zones)
        print(line)
    print()


def _print_compare(baseline: list[dict], shifted: list[dict], begin: datetime, finish: datetime, what: str, tz: ZoneInfo,
                   event_window: tuple[datetime, datetime], base_comfort: dict[str, int], comfort: dict[str, int]) -> None:
    base_win = [r for r in baseline if begin <= r["at"] < finish]
    shift_win = [r for r in shifted if begin <= r["at"] < finish]
    base_peak = max(r["load_kw"] for r in base_win)
    shift_peak = max(r["load_kw"] for r in shift_win)
    base_day = max(baseline, key=lambda r: r["load_kw"])
    shift_day = max(shifted, key=lambda r: r["load_kw"])
    base_kwh = sum(r["hvac_kw"] for r in baseline) / 60
    shift_kwh = sum(r["hvac_kw"] for r in shifted) / 60
    label = f"{what} {begin.astimezone(tz):%H:%M}–{finish.astimezone(tz):%H:%M}"
    print("## 错峰前后（同一输入，只有暖通动作不同）")
    print(f"{'项目':<30}{'基线':>10}{'错峰后':>10}{'差':>9}")
    print(f"{label + ' 峰值 kW':<30}{base_peak:>10.0f}{shift_peak:>10.0f}{shift_peak - base_peak:>+9.0f}")
    ev_base = max(r["load_kw"] for r in baseline if event_window[0] <= r["at"] < event_window[1])
    ev_shift = max(r["load_kw"] for r in shifted if event_window[0] <= r["at"] < event_window[1])
    ev_label = f"事件窗口 {event_window[0].astimezone(tz):%H:%M}–{event_window[1].astimezone(tz):%H:%M} 峰值 kW"
    print(f"{ev_label:<30}{ev_base:>10.0f}{ev_shift:>10.0f}{ev_shift - ev_base:>+9.0f}")
    print(f"{label + ' 暖通峰值 kW':<30}{max(r['hvac_kw'] for r in base_win):>10.0f}"
          f"{max(r['hvac_kw'] for r in shift_win):>10.0f}")
    print(f"{'全天峰值 kW':<30}{base_day['load_kw']:>10.0f}{shift_day['load_kw']:>10.0f}{shift_day['load_kw'] - base_day['load_kw']:>+9.0f}")
    print(f"{'全天峰值时刻':<30}{base_day['at'].astimezone(tz):%H:%M}      {shift_day['at'].astimezone(tz):%H:%M}")
    print(f"{'暖通日电量 kWh':<30}{base_kwh:>10.0f}{shift_kwh:>10.0f}{shift_kwh - base_kwh:>+9.0f}")
    print()
    names = {"production": "车间", "admin": "行政", "cleanroom": "洁净", "datacenter": "机房"}
    print(f"{'区域':<8}{'基线 起→止 ℃':<20}{'错峰后 起→止 ℃':<20}{'越界分钟 基线→错峰':>20}")
    for zone in base_win[0]["zones"]:
        base_span = (base_win[0]["zones"][zone]["temperature"], base_win[-1]["zones"][zone]["temperature"])
        shift_span = (shift_win[0]["zones"][zone]["temperature"], shift_win[-1]["zones"][zone]["temperature"])
        print(f"{names.get(zone, zone):<8}{base_span[0]:>6.2f} → {base_span[1]:<9.2f}"
              f"{shift_span[0]:>6.2f} → {shift_span[1]:<9.2f}{base_comfort.get(zone, 0):>11} → {comfort.get(zone, 0):<6}")
    print()


def _print_notes(thermal: dict, begin: datetime, finish: datetime, tz: ZoneInfo) -> None:
    hard = thermal.get("hard_zones") or []
    print("## 说明")
    print(f"- 对比窗口 {begin.astimezone(tz):%H:%M}–{finish.astimezone(tz):%H:%M}：推迟预冷看窗口内让出的容量，"
          f"预冷看窗口之后的峰段少用多少（冷量已提前买进去）。")
    print("- 让出的冷量会在之后补回：暖通日电量基本不变，错峰不是省电；窗口后有回拖，全天峰值可能略升或改时刻。")
    print(f"- 硬约束区域（{'、'.join(hard) or '无'}）的温湿度按硬约束保供，提案里不会出现它们的削峰动作。")
    print("- 越界分钟按各区舒适区间（见 configs/hvac/thermal.yaml）统计，只计供冷时段；它同时是提案代价 comfort_min。")
    print("- 数据性质：温度与功率轨迹 simulated；区域参数 configured；负荷与人流来自 B18（derived/synthetic）；气温 PVGIS（derived）。")


def _short(entity_id: str) -> str:
    return entity_id.split("/", 3)[-1] if entity_id.startswith("entity://") else entity_id


if __name__ == "__main__":
    raise SystemExit(main())
