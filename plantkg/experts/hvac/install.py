"""暖通专家接入注册表（B21）：登记 hvac_propose 业务函数，由 B23 的提案包装落库成 MCP 工具 plantkg_hvac_propose。

业务函数签名 handler(context, binding, focus=None) -> ExpertProposal（proposal_id 为空的草稿），
B23 的 proposals.install 把它包成“落库 → 只返回 proposal_id 与摘要”的工具（03 §3.0）。

现场情境（当分钟事件单与区域温度）由会诊方经 hvac_situation 钩子注入：fn(context, binding) -> (event, state) | None，
state 的形状见 HvacExpert.propose；ThermalSim.situation(state) 正好给出这个形状。
场景默认装配（SCENARIO_HOOK）：ThermalSim 经 SIM_HOOK 原位替换 B17 的同名 HvacSim，HvacExpert 登记为 deterministic 专家 hvac；
直派器每次派到暖通时，这里按**完整绑定**记下当时的事件与区域视图（运行、事件单、轮次、触发时刻、状态版本），
取用时逐项核验，跨运行、跨事件单、跨轮次一律不命中 → 返回保守提案，不编数字。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from plantkg.core.contracts import AgentResultKind, DataNature, DayMetric, ErrorCode, ExpertProposal, PlantkgError, ProposalCost, RunBinding
from plantkg.core.ports import AgentResultSource, RunStore
from plantkg.core.registry import Registry
from plantkg.knowledge.rules.normalise import subject_zone
from plantkg.world.scenario.export import CONFIG_METRICS_HOOK
from plantkg.world.scenario.runner import SCENARIO_HOOK
from plantkg.world.scenario.sims import SIM_HOOK
from plantkg.world.sim.thermal import ThermalSim, load_zones

from .expert import HvacExpert

SITUATION_HOOK = "hvac_situation"  # 会诊方（B28）登记现场情境的钩子名
TOOL = "hvac_propose"
SITUATION_LIMIT = 64  # 保留最近多少条情境：同一次运行里每个事件单、每一轮一条


def _binding_key(binding: RunBinding) -> tuple:
    """情境的绑定键：换运行、换事件单、换轮次、换触发时刻或状态版本都不复用。"""
    return (binding.run_id, binding.ticket_id, binding.round, binding.trigger_at.isoformat(), binding.state_version)


class HvacExpertSource:
    """AgentResultSource（deterministic）：payload 由 B17 直派器给出（event、state=运行简报、ids），返回 {"proposal": ...}。
    简报里的 situation 是 ThermalSim.situation 的区域视图；按完整绑定存档，供 hvac_propose 取同一事件单的情境。"""

    def __init__(self, expert: HvacExpert, limit: int = SITUATION_LIMIT, registry: Registry | None = None) -> None:
        self.expert = expert
        self.registry = registry
        self._limit = limit
        self._situations: OrderedDict[tuple, tuple[Mapping[str, Any], Mapping[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if kind != AgentResultKind.expert.value and kind != AgentResultKind.expert:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"暖通专家只实现 expert，不实现 {kind}")
        event, situation = payload.get("event") or {}, (payload.get("state") or {}).get("situation")
        if situation is None:  # 调用方显式给了仿真器、没有 ThermalSim 时简报里没有区域视图
            return {"proposal": _conservative(binding).model_dump(mode="json")}
        state = dict(situation) | {"confirmed": bool(payload.get("confirmed"))}
        self.remember(binding, event, state)
        manifest_id, as_of = None, binding.trigger_at
        if self.registry is not None and RunStore in self.registry.ports:
            try:
                run = self.registry.resolve(RunStore).get_run(binding.run_id)
                manifest_id = run.version_manifest_id
                as_of = max(run.as_of, run.forked_at) if run.forked_at else run.as_of
            except PlantkgError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
        expert, refs = _knowledge_expert(self.registry, self.expert, manifest_id, binding.trigger_at, as_of)
        proposal = expert.propose(event, state, ticket_id=binding.ticket_id)
        if refs:
            proposal = proposal.model_copy(update={"evidence_refs": proposal.evidence_refs + refs})
        return {"proposal": proposal.model_dump(mode="json")}

    def remember(self, binding: RunBinding, event: Mapping[str, Any], state: Mapping[str, Any]) -> None:
        key = _binding_key(binding)
        with self._lock:
            self._situations[key] = (dict(event), dict(state))
            self._situations.move_to_end(key)
            while len(self._situations) > self._limit:
                self._situations.popitem(last=False)

    def situation(self, binding: RunBinding) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
        """只返回与该绑定完全匹配的情境；没有匹配（新运行、新事件单、新一轮）时给出 None，由调用方走保守提案。"""
        with self._lock:
            found = self._situations.get(_binding_key(binding))
            if found is not None:
                self._situations.move_to_end(_binding_key(binding))
            return found


def install(registry: Registry, settings: Any = None) -> None:
    """pack.yaml 的入口：plantkg.experts.hvac.install:install（领域包只能依赖 core，实现放在专家平面）。"""
    expert = HvacExpert()
    source = HvacExpertSource(expert, registry=registry)
    # 暖通自己的消费端区域钩子（#241/#225）：router.entity_zone 保持图谱原义，热模型区域过滤只在这里，
    # 发布期与运行时都从这里取。调用时才取图谱钩子，暖通包先于 global_graph 装载也能解析。
    # 图谱节点按所在区域、区域主体按自身；热模型里没有的区域（site.yaml 的 hazmat/park）返回 None，发布被拒。

    def consumer_zone(entity: Any) -> str | None:
        hooks = registry.hooks_for("router.entity_zone")
        zone = hooks[-1](entity) if hooks else None
        if zone is None and str(entity).startswith("zone:"):
            zone = str(entity).split(":", 1)[1]
        return zone if zone in expert.zones else None

    registry.register_hook("hvac.consumer_zone", consumer_zone)

    def propose(context: Any, binding: RunBinding, focus: str | None = None) -> ExpertProposal:
        found = _situation(registry, context, binding)
        if found is None:
            return _conservative(binding)
        as_of = getattr(context, "as_of", binding.trigger_at)
        if context and context.run_id and RunStore in registry.ports:
            try:
                run = registry.resolve(RunStore).get_run(context.run_id)
            except PlantkgError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            else:
                if run.version_manifest_id != context.version_manifest_id:
                    raise PlantkgError(ErrorCode.PRECONDITION, "暖通运行与知识版本不一致")
                if run.forked_at:
                    as_of = max(as_of, run.forked_at)
        selected, refs = _knowledge_expert(registry, expert, getattr(context, "version_manifest_id", None), binding.trigger_at, as_of)
        result = selected.propose(found[0], found[1], ticket_id=binding.ticket_id)
        return result.model_copy(update={"evidence_refs": result.evidence_refs + refs}) if refs else result

    registry.register_tool(TOOL, propose)
    registry.register_hook(SITUATION_HOOK, lambda context, binding: source.situation(binding))

    def scenario(scenario_registry: Registry) -> None:
        scenario_registry.register_mode(AgentResultSource, "deterministic", source, key="hvac")
        scenario_registry.register_hook(SIM_HOOK, ThermalSim)
        scenario_registry.register_hook(CONFIG_METRICS_HOOK, config_metrics)

    registry.register_hook(SCENARIO_HOOK, scenario)


def config_metrics(site=None, policy=None) -> list[DayMetric]:
    """#238 方案 1：导出暖通限值（config 组）。生产区送风设定温度从 thermal.yaml 读，读不到就不输出。"""
    try:
        production = load_zones().get("production")
    except (OSError, ValueError):  # 配置缺文件或格式不对：不输出这项，导出不因限值缺项失败
        return []
    if production is None:
        return []
    return [DayMetric(metric="hvac_supply_setpoint_c", group="config", value=production.setpoint_c, unit="degC",
                      note="生产区送风设定温度（thermal.yaml setpoint_c，configured，不参与改善率）",
                      nature=DataNature.configured)]


def _knowledge_expert(registry, expert, manifest_id, at, as_of=None):
    """取本运行固定清单的暖通限值，不修改共享专家或旧运行。"""
    if registry is None or not manifest_id:
        return expert, ()
    providers = registry.hooks_for("knowledge.b30.rules")
    if not providers:
        return expert, ()
    zones, refs = dict(expert.zones), []
    zone_hooks = registry.hooks_for("hvac.consumer_zone")
    zone_of = zone_hooks[-1] if zone_hooks else None
    for rule in providers[-1](manifest_id, at, as_of or at):
        if rule.kind != "zone_temperature_limit" or rule.unit != "degC" or rule.bounds is None:
            continue
        if rule.bounds.lower is None or rule.bounds.upper is None:
            continue
        # 规则主体按区域或图谱所在区域解析（#175 方案 3c）：zone/admin、zone:admin 直接取，
        # 设备主体（如 CR-AHU1）经 hvac.consumer_zone 过滤到热模型区域，解析不到的主体忽略
        matched = []
        for subject in rule.subject_ids:
            zone = subject_zone(subject, zone_of)
            if zone in zones and zone not in matched:
                matched.append(zone)
        for zone in matched:
            zones[zone] = replace(zones[zone], comfort_c=(rule.bounds.lower, rule.bounds.upper))
        if matched:
            refs.append(f"rule:{rule.rule_id}")
    return (HvacExpert(zones=zones, price_cny_per_kwh=expert.price_cny_per_kwh), tuple(refs)) if refs else (expert, ())


def _conservative(binding: RunBinding) -> ExpertProposal:
    return ExpertProposal(expert="hvac", ticket_id=binding.ticket_id, actions=(), cost=ProposalCost(cny=0.0, comfort_min=0),
                          confidence=0.3,
                          diagnosis="[synthetic] 没拿到当前事件单与区域温度（会诊方须经 hvac_situation 钩子注入）；"
                                    "本提案只给保守建议：维持设定温度，不做削峰")


def _situation(registry: Registry, context: Any, binding: RunBinding) -> tuple[Any, Any] | None:
    for provider in reversed(registry.hooks_for(SITUATION_HOOK)):
        found = provider(context, binding)
        if found:
            return found
    return None
