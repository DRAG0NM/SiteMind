"""电梯专家装配入口（`packs/elevator/pack.yaml` 的 entrypoint）。

领域包只声明清单；专家的实现放在 `plantkg.experts.elevator`，由这里把 `expert_propose:elevator`
钩子登记到注册表。B23 的提案包装层随后把它包成 MCP 工具 `plantkg_elevator_propose`：
工具按运行绑定落库，模型只拿 proposal_id 与摘要。

B27 的事件单上下文接入后，登记 `expert_context:elevator` 钩子返回 (event, state) 即可替换本卡的
合成上下文；没有登记时按关注词造一个标 synthetic 的事件，数字仍由一次早高峰仿真给出。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from plantkg.core.contracts import AgentResultKind, ErrorCode, PlantkgError
from plantkg.core.ports import AgentResultSource, RunStore
from plantkg.knowledge.rules.normalise import ELEVATOR_ACTIONS, entity_keys
from plantkg.world.scenario.runner import SCENARIO_HOOK
from plantkg.world.scenario.sims import DEFAULT_EFFECTS, SIM_HOOK
from plantkg.world.site.model import SiteModel
from plantkg.world.sim.elevator import ElevatorConfig, ScenarioElevatorSim

from .expert import ElevatorExpert

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SITE = "configs/sites/factory/site.yaml"
PROPOSE_HOOK = "expert_propose:elevator"
CONTEXT_HOOK = "expert_context:elevator"


class ElevatorExpertSource:
    """AgentResultSource（deterministic）：把 B17 直派器的 payload 适配成专家的 (event, state)。"""

    def __init__(self, expert: ElevatorExpert, registry: Any = None) -> None:
        self.expert, self.registry = expert, registry

    def get(self, kind: Any, binding: Any, payload: Any) -> dict:
        if kind != AgentResultKind.expert.value and kind != AgentResultKind.expert:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"电梯专家只实现 expert，不实现 {kind}")
        event = dict(payload["event"]) | {"at": binding.trigger_at, "ticket_id": binding.ticket_id}
        # 剧本没写 effects 时，世界按 DEFAULT_EFFECTS 施加影响；专家按同一份判断（如默认 3 倍突增）
        event["payload"] = {"effects": DEFAULT_EFFECTS.get(event.get("event_type"), [])} | dict(event.get("payload") or {})
        state = dict(payload.get("state") or {}) | {"confirmed": bool(payload.get("confirmed"))}
        if self.registry is not None and RunStore in self.registry.ports:
            try:
                run = self.registry.resolve(RunStore).get_run(binding.run_id)
            except PlantkgError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            else:
                as_of = max(run.as_of, run.forked_at) if run.forked_at else run.as_of
                _apply_published_rule(self.registry, run.version_manifest_id, binding.trigger_at, as_of, event, state)
        if event.get("event_type") == "elevator_congestion":  # 直派器的 brief 不带候梯数字：按早高峰仿真给出（simulated）
            state["metrics"] = self.expert.peak_metrics()
        proposal = self.expert.propose(event, state)
        return {"proposal": proposal.model_dump(mode="json")}


def install(registry: Any, settings: Any = None) -> None:
    site_path = ((settings.section("elevator") if settings is not None else None) or {}).get("site", DEFAULT_SITE)
    site = SiteModel.load(ROOT / site_path)
    expert = ElevatorExpert(ElevatorConfig.from_site(site))

    def handler(context: Any, binding: Any, focus: str | None = None):
        providers = registry.hooks_for(CONTEXT_HOOK)
        if providers:
            event, state = providers[-1](context, binding, focus)
        else:
            event, state = expert.synthetic_context(focus)
        event = dict(event)
        state = dict(state or {})
        event.setdefault("at", binding.trigger_at)
        event["ticket_id"] = binding.ticket_id
        as_of = getattr(context, "as_of", binding.trigger_at)
        if context and context.run_id and RunStore in registry.ports:
            try:
                run = registry.resolve(RunStore).get_run(context.run_id)
            except PlantkgError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            else:
                if run.version_manifest_id != context.version_manifest_id:
                    raise PlantkgError(ErrorCode.PRECONDITION, "电梯运行与知识版本不一致")
                if run.forked_at:
                    as_of = max(as_of, run.forked_at)
        _apply_published_rule(registry, getattr(context, "version_manifest_id", None), binding.trigger_at, as_of, event, state)
        return expert.propose(event, state)

    registry.register_hook(PROPOSE_HOOK, handler)

    def scenario_plugin(scenario_registry: Any) -> None:
        """场景装配：真实专家替换 B17 替身，ScenarioElevatorSim 原位替换 B17 的 ElevatorSim。"""
        scenario_registry.register_mode(AgentResultSource, "deterministic", ElevatorExpertSource(expert, registry), key="elevator")
        scenario_registry.register_hook(SIM_HOOK, lambda index: ScenarioElevatorSim(index.site))

    registry.register_hook(SCENARIO_HOOK, scenario_plugin)


def _apply_published_rule(registry, manifest_id, at, as_of, event, state):
    providers = registry.hooks_for("knowledge.b30.rules")
    if not manifest_id or not providers:
        return
    entity = event.get("entity_id")
    subjects = set(entity_keys(entity))
    for rule in providers[-1](manifest_id, at, as_of):
        action = rule.condition.get("action")
        if (rule.kind == "elevator_congestion_rule" and rule.unit == "s" and rule.bounds and rule.bounds.upper is not None
                and subjects & {key for subject in rule.subject_ids for key in entity_keys(subject)}
                and (action in (None, "") or action in ELEVATOR_ACTIONS)):
            state["published_elevator_rule"] = rule.model_dump(mode="json")
            break
