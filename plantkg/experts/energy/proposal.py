"""B19 能源专家：`EnergyExpert.propose(event, state) → ExpertProposal`。

事件级确定性提案（第 1 层）：照明回路故障切备用回路、用电突增/容量告警时储能放电削峰（照明作为可调
负荷按比例降功率），并给出估算代价与置信度。分钟级的持续跟踪与 15 分钟滚动重解在 `realtime.py` 与
`day.py`，本模块只回答“这个事件现在该做什么”。

`register_agent_source` 把它登记为 B17 直派器使用的 deterministic 专家；`install` 把
`expert_propose:energy` 钩子挂到注册表，供 B23 的 `plantkg_energy_propose` 工具调用（经统一提案
包装落库）。演示运行时可用 `registry.register_hook("energy_state:<run_id>", fn)` 提供当前事件与状态。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from plantkg.core.ports import AgentResultSource
from plantkg.world.scenario.export import CONFIG_METRICS_HOOK
from plantkg.world.scenario.runner import SCENARIO_HOOK
from plantkg.core.contracts import (
    AgentResultKind,
    DataNature,
    DayMetric,
    ErrorCode,
    ExpertProposal,
    Interval,
    PlantkgError,
    ProposalCost,
    ProposedAction,
    RunBinding,
)

SPIKE_EVENTS = frozenset({"load_spike", "sim_limit", "pv_drop", "heat_wave", "capacity_shed_request"})
# 演示假设（configured）：峰谷价差与放电时段，用于估算削峰省下的钱
DEFAULT_SPREAD_CNY = 0.35
DEFAULT_DISCHARGE_KW = 400.0
DEFAULT_SHED_CUT = 0.2  # 照明削峰缺省削 20%
ENERGY_CONFIG = Path(__file__).resolve().parents[3] / "nexent_assets" / "skills" / "domain-energy" / "config" / "config.yaml"
FALLBACK_MAX_DISCHARGE_KW = 500.0  # 运行简报没给储能放电上限时的演示假设（configured）


@cache
def _shed_cut_max() -> float:
    """domain-energy 配置里照明最大降功率比例（lighting.shed_ratio_max）。"""
    return float(yaml.safe_load(ENERGY_CONFIG.read_text(encoding="utf-8"))["lighting"]["shed_ratio_max"])


def _action(entity_id: str, command: str, start: datetime, minutes: int, readiness: str = "ready", depends_on=(), **params) -> ProposedAction:
    return ProposedAction(entity_id=entity_id, command=command, params=params,
                          interval=Interval(start=start, end=start + timedelta(minutes=minutes)),
                          readiness=readiness, depends_on=tuple(depends_on))


class EnergyExpert:
    """事件 → 能源动作。`state` 为运行简报：soc / soc_min / battery_nodes / ids / 可选实时价格。"""

    def propose(self, event: Mapping[str, Any] | None, state: Mapping[str, Any] | None, *, ticket_id: str = "TK-manual",
                trigger_at: datetime | None = None, focus: str | None = None) -> ExpertProposal:
        event = dict(event or {})
        state = dict(state or {})
        kind = event.get("event_type") or event.get("type") or "manual_request"
        t = trigger_at or _instant(state.get("at")) or _instant(event.get("at")) or datetime(2026, 7, 1, tzinfo=_utc())
        ids = state.get("ids") or {}
        battery = state.get("battery_id") or ids.get("battery")
        soc = float(state.get("soc", 1.0))
        soc_min = float(state.get("soc_min", 0.1))

        if kind == "lighting_fault":
            entity = event.get("entity_id") or ids.get("LT-P")
            if not entity:
                return self._no_action(ticket_id, "照明回路故障，但运行简报里没有照明对象", 0.3)
            return ExpertProposal(expert="energy", ticket_id=ticket_id,
                                  actions=(_action(entity, "switch_circuit", t, 1),),
                                  cost=ProposalCost(cny=0.0), confidence=0.9,
                                  diagnosis=f"照明回路 {_short(entity)} 跳闸，切到备用回路（仿真动作 switch_circuit）",
                                  safety_tags=())

        if kind in SPIKE_EVENTS:
            node = (event.get("payload") or {}).get("node")
            nodes = state.get("battery_nodes") or []
            if node and nodes and node not in nodes:
                return self._no_action(ticket_id, f"储能接在 {'、'.join(nodes[:2])}，放电缓解不了 {node}", 0.65)
            if soc <= soc_min + 0.01:
                return self._no_action(ticket_id, f"储能 SOC {soc:.2f} 已到下限 {soc_min:.2f}，无法放电削峰", 0.6)
            if not battery:
                return self._no_action(ticket_id, "运行简报里没有储能对象", 0.3)
            minutes = 60 if kind in ("pv_drop", "heat_wave") else 30
            max_kw = float(state.get("discharge_kw_max") or FALLBACK_MAX_DISCHARGE_KW)
            kw = min(float(event.get("payload", {}).get("discharge_kw", DEFAULT_DISCHARGE_KW)), max_kw)
            price = state.get("price_cny_per_kwh")
            spread = DEFAULT_SPREAD_CNY if price is None else max(0.0, float(price) - 0.217)
            savings = -round(kw * minutes / 60 * spread, 2)
            actions = [_action(battery, "discharge", t, minutes, power_kw=kw)]
            lighting = ids.get("LT-P")
            if kind in ("load_spike", "sim_limit", "capacity_shed_request") and lighting:
                cut = float((event.get("payload") or {}).get("shed_ratio", DEFAULT_SHED_CUT))
                cut = min(max(cut, 0.0), _shed_cut_max())  # 请求比例按配置的最大削减比例夹取
                # B17 ProfileSim 的 shed 参数是“剩余功率比例”：0.8 表示削减 20%
                actions.append(_action(lighting, "shed", t, 15, ratio=round(1 - cut, 6)))
            return ExpertProposal(expert="energy", ticket_id=ticket_id, actions=tuple(actions),
                                  cost=ProposalCost(cny=savings), confidence=0.82,
                                  diagnosis=(f"储能放电 {kw:.0f} kW 削峰 {minutes} 分钟（放到下限为止），"
                                             + (f"生产照明降 {cut:.0%} 参与削峰" if len(actions) > 1 else "不外送")),
                                  evidence_refs=("knowledge:energy_realtime_sop",))

        if kind == "power_quality_dip":
            return self._no_action(ticket_id, "判为电网侧电压暂降，空压机欠压保护动作，无需检修", 0.6)
        if kind == "manual_request":
            return self._no_action(ticket_id, f"人工询问能源状态{('：' + focus) if focus else ''}；没有可执行动作", 0.5)
        return self._no_action(ticket_id, f"事件 {kind} 不属能源领域", 0.45)

    @staticmethod
    def _no_action(ticket_id: str, diagnosis: str, confidence: float) -> ExpertProposal:
        return ExpertProposal(expert="energy", ticket_id=ticket_id, actions=(), cost=ProposalCost(cny=0.0),
                              confidence=confidence, diagnosis=diagnosis)


class EnergyExpertSource:
    """AgentResultSource（deterministic）：payload 由 B17 直派器给出，返回 {"proposal": ...}。"""

    source_kind = "deterministic"
    name = "b19-energy"

    def __init__(self, expert: EnergyExpert | None = None) -> None:
        self.expert = expert or EnergyExpert()

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if kind != AgentResultKind.expert.value and kind != AgentResultKind.expert:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"能源专家只实现 expert，不实现 {kind}")
        state = dict(payload.get("state") or {})
        state.setdefault("ids", payload.get("ids") or {})
        proposal = self.expert.propose(payload.get("event"), state, ticket_id=binding.ticket_id, trigger_at=binding.trigger_at)
        return {"proposal": proposal.model_dump(mode="json")}


def register_agent_source(registry, expert: EnergyExpert | None = None, *, mode: str = "deterministic") -> EnergyExpertSource:
    """把能源专家登记为 AgentResultSource（键 energy）；B17 直派器按专家名取。"""
    source = EnergyExpertSource(expert)
    registry.register_mode(AgentResultSource, mode, source, key="energy")
    return source


def config_metrics(site, policy=None) -> list[DayMetric]:
    """#238 方案 1：导出能源限值（config 组）。数值从 site.yaml 与同目录 storage.yaml 读，读不到就不输出。"""
    data = getattr(site, "config", site) or {}
    distribution = data.get("distribution") or {}
    out = []

    def add(metric: str, value, unit: str, note: str) -> None:
        if value is not None:
            out.append(DayMetric(metric=metric, group="config", value=float(value), unit=unit, note=note,
                                 nature=DataNature.configured))

    add("transformer_limit_pct", distribution.get("transformer_load_limit_pct"), "%",
        "变压器负载显示上限（configured）；硬约束仍是 capacity_kva × power_factor，不参与改善率")
    add("contract_capacity_kw", distribution.get("contract_capacity_kw"), "kW",
        "电网接入合同容量（configured，不参与改善率）")
    soc_min = _storage_soc_min(site)
    if soc_min is not None:
        add("battery_soc_floor_pct", soc_min * 100, "%", "储能 SOC 下限（storage.yaml soc_min，configured，不参与改善率）")
    return out


def _storage_soc_min(site) -> float | None:
    """储能配置与 site.yaml 放同一装配目录；没有文件或没有 soc_min 时返回 None。"""
    path = getattr(site, "config_path", None)
    if path is None:
        return None
    storage = Path(path).parent / "storage.yaml"
    if not storage.is_file():
        return None
    data = yaml.safe_load(storage.read_text(encoding="utf-8")) or {}
    soc_min = (data.get("storage") or {}).get("soc_min")
    return float(soc_min) if soc_min is not None else None


def install(registry, settings=None, *, expert: EnergyExpert | None = None) -> EnergyExpert:
    """领域包入口（pack.yaml）。登记 `expert_propose:energy` 钩子：B23 的统一包装按本次运行的绑定调用它。

    事件与状态由运行方通过 `registry.register_hook("energy_state:<run_id>", provider)` 提供，provider 收 RunBinding，
    事件必须是该单本轮的事件，不是运行里最近的告警；没有时给出
    人工询问的只读回答，不凭空造动作。同时登记 SCENARIO_HOOK：场景默认装配用真实 EnergyExpert。
    """
    expert = expert or EnergyExpert()

    def handler(context, binding, focus: str | None = None) -> ExpertProposal:
        run_id = binding.run_id or context.run_id  # 工具调用的 ctx 不一定带场景运行号，事件单绑定的运行才是场景运行
        providers = registry.hooks_for(f"energy_state:{run_id}") if run_id else ()
        event, state = providers[-1](binding) if providers else ({}, {})
        return expert.propose(event, state, ticket_id=binding.ticket_id, trigger_at=binding.trigger_at, focus=focus)

    registry.register_hook("expert_propose:energy", handler)
    from .planner import compare_tariff_interpretations, published_tariff_price
    registry.register_hook("knowledge.counterfactual.tariff", compare_tariff_interpretations)
    registry.register_hook("knowledge.tariff.slot_price", published_tariff_price)
    if settings is not None and settings.profile == "mock":
        from .synthetic_score import synthetic_score_inputs
        registry.register_hook("knowledge.counterfactual.inputs", synthetic_score_inputs)
    from plantkg.packs.tariff import parse_schedule
    registry.register_hook("knowledge.validate.tariff", parse_schedule)
    if settings is not None:  # 计划账本经 world/record 的查询钩子提供，bootstrap 不直接依赖本包
        from plantkg.world.record.providers import PLAN_LEDGER_HOOK
        from .ledger import PlanLedgerStore
        registry.register_hook(PLAN_LEDGER_HOOK, lambda writer, context, run_id, cursor, size:
                               PlanLedgerStore(writer, settings.plant_id).query(context, run_id, cursor, size))
    def scenario_plugin(scenario_registry) -> None:
        register_agent_source(scenario_registry, expert)
        scenario_registry.register_hook(CONFIG_METRICS_HOOK, config_metrics)

    registry.register_hook(SCENARIO_HOOK, scenario_plugin)
    return expert


def energy_state_hook(runner: Any) -> Callable[[Any], tuple[dict, dict]]:
    """给演示运行注册的状态提供者：按 RunBinding 返回该单本轮的事件与运行简报（MCP 工具在同一进程里读）。

    运行级状态共享；事件按 (run_id, ticket_id, round) 取事件单的事件，对不上就给空事件，不回退到最近告警。
    """

    def current(binding: Any) -> tuple[dict, dict]:
        ticket = runner.tickets.tickets.get(binding.ticket_id)
        mine = binding.run_id == runner.run_info.run_id and ticket is not None and ticket.ticket.round == binding.round
        state = runner.brief() | {"ids": runner.ids, "discharge_kw_max": runner.world.discharge_kw}
        return (ticket.event if mine else {}), state

    return current


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
        return value if value.tzinfo is not None else None
    return None


def _utc():
    from datetime import timezone
    return timezone.utc


def _short(entity_id: str) -> str:
    return entity_id.rsplit("/", 1)[-1]
