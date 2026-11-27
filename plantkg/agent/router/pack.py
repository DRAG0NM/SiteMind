"""领域包入口（B27）：把值守层接进注册表。

存储启用时装配完整值守层：ProposalSink、召回、慢通道工具、分叉钩子与确定性研判/调查，并把带版本的 PolicyConfig 传给严重程度入口。
观测读数（B22）、图谱能力（B25）由各自的包登记 router.* 钩子；试算台（B17）没有自己的包，在这里按 severity.yaml 的
trial_scenario 装配。值守层运行时才取钩子，与包的加载先后无关。
未启用存储时仍登记同名工具，但一律返回“值守层未启用存储”，避免把没有落库能力的工具伪装成可用。
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from plantkg.agent.router.router import (
    EXPERTS_HOOK, INSTRUCTION_HOOK, PROBE_HOOK, PROPOSALS_READY_HOOK, REPLIES_READY_HOOK, ROUTER_HOOK, SCENARIO_BIND_HOOK, STEP_HOOK,
    URGENT_HOOK, install,
)
from plantkg.agent.router.proposals import SqliteProposalSink
from plantkg.agent.router.recall import Recall
from plantkg.agent.router.store import TICKET_WRITEBACK_HOOK, WATCHDOG_TICKETS_HOOK, RouterStore, TicketInspectionSource
from plantkg.agent.router.severity import load_severity_config
from plantkg.core.contracts import Actor, Context, ErrorCode, Interval, PlantkgError, PolicyConfig, ToolResult, tool_error
from plantkg.core.ports import Repository, RunStore, VersionStore
from plantkg.world.replay.clock import REPLAY_ADVANCED_HOOK

ROOT = Path(__file__).resolve().parents[3]
log = logging.getLogger("plantkg.router")
# 场景注册表是另一份：能力钩子与下游业务钩子都要带过去，协同组才会调用它们
SCENARIO_ROUTER_HOOKS = ("router.locate", "router.safety_hit", "router.downstream_lines", "router.entity_type", "router.sources", "router.readings",
                         EXPERTS_HOOK, PROPOSALS_READY_HOOK, REPLIES_READY_HOOK, URGENT_HOOK, STEP_HOOK, PROBE_HOOK, INSTRUCTION_HOOK, SCENARIO_BIND_HOOK)


def _unavailable(*args: Any, **kwargs: Any) -> ToolResult:
    return tool_error(ErrorCode.DEPENDENCY_UNAVAILABLE, "需要 storage.enabled=true（值守状态与时序数据无处落库）")


def _scenario_plugin(source: Any) -> Any:
    """把协同组派发器登记进场景注册表，并带上领域包登记的 router.* 钩子。"""
    def plugin(registry: Any) -> None:
        from plantkg.agent.router.scenario import CoordinatedDispatcher
        from plantkg.world.scenario.runner import DISPATCHER_HOOK

        registry.register_hook(DISPATCHER_HOOK, CoordinatedDispatcher)
        for name in SCENARIO_ROUTER_HOOKS:
            for fn in source.hooks_for(name):
                registry.register_hook(name, fn)
    return plugin


def register(registry: Any, settings: Any) -> None:
    from plantkg.world.scenario.runner import SCENARIO_HOOK

    registry.register_hook(SCENARIO_HOOK, _scenario_plugin(registry))
    # B32 的 RuleMatcher 用它做兜底：没有已发布规则的类别仍按 B27 的静态白名单进快通道
    registry.register_hook("router.rule_covered_events",
                           lambda: tuple(load_severity_config().get("rule_covered_events", ())))
    registry.register_tool("ts_query", _unavailable, placeholder=True)  # 主 Agent 绑定了它；B07 正式实现随存储装配，已登记时这里跳过
    repository = registry.ports.get(Repository)
    if repository is None:
        _register_unavailable(registry)
        return
    from plantkg.world.scenario.__main__ import build_world
    from plantkg.world.scenario.bench import install as install_bench
    from plantkg.world.scenario.script import load_scenario

    config = load_severity_config()
    scenario = load_scenario(ROOT / config["trial_scenario"])
    install_bench(registry, lambda: build_world(scenario))
    router = install(registry, repository.writer, settings.plant_id, run_id=f"router-{settings.plant_id}",
                     knowledge_version=_knowledge_version(registry), policy=_policy(scenario.policy_config(), config),
                     mode=_router_mode(settings),
                     entity_lookup=_hook_lookup(registry, "router.recall_entity"),
                     asset_lookup=_hook_lookup(registry, "router.recall_asset"))
    registry.register_hook(ROUTER_HOOK, router)
    # B42 每次巡检取一次当前运行视图：主路由与共用同一写者的协同组运行（plant#run）都按实际作用域读
    registry.register_hook(WATCHDOG_TICKETS_HOOK, lambda: [TicketInspectionSource(RouterStore(repository.writer, scope))
                                                          for scope in RouterStore.inspection_scopes(repository.writer, settings.plant_id)])
    # B47 在 runner/router 未绑定时按任务的 run 作用域兜底把事件单置 blocked（#240 复审）
    registry.register_hook(TICKET_WRITEBACK_HOOK,
                           lambda ticket_id, run_id: RouterStore.block_ticket_for_reject(repository.writer, ticket_id, run_id))
    if router.mode == "live":
        _ensure_live_run(registry, settings, router)
    log.info("值守路由已装配：模式 %s，运行 %s，知识版本 %d", router.mode, router.run_id, router.knowledge_version)

    replay_routers: dict[str, Any] = {}
    replay_router_lock = threading.Lock()

    def router_for_run(run_id: str) -> Any:
        if run_id == router.run_id:
            return router
        with replay_router_lock:
            replay_router = replay_routers.get(run_id)
            if replay_router is not None:
                return replay_router
            run = registry.resolve(RunStore).get_run(run_id)
            if run.kind not in ("replay_day", "replay_week"):
                return None
            replay_router = copy.copy(router)
            replay_router.run_id = run.run_id
            replay_router._replay = True
            replay_router._seq = 0
            replay_router.runs = []
            replay_router.knowledge_version = registry.resolve(VersionStore).knowledge_version(run.version_manifest_id)
            scope = f"{router.plant_id}#{run.run_id}"
            replay_router.store = RouterStore(router.store.writer, scope)
            replay_router.sink = SqliteProposalSink(router.store.writer, scope, run_id=run.run_id)
            replay_router.recall = Recall(replay_router.store, replay_router.sink, plant_id=scope,
                                          entity_lookup=router.recall.entity_lookup, asset_lookup=router.recall.asset_lookup)
            replay_router._run_router = router_for_run
            replay_routers[run.run_id] = replay_router
            for bind in registry.hooks_for(SCENARIO_BIND_HOOK):
                bind(replay_router, None)
            return replay_router

    router._run_router = router_for_run

    def replay_advanced(context: Any, instant: Any) -> None:
        replay_router = router_for_run(context.run_id)
        if replay_router is not None:
            replay_router.step(instant)

    registry.register_hook(REPLAY_ADVANCED_HOOK, replay_advanced)


def _hook_lookup(registry: Any, hook: str) -> Any:
    """按钩子取寻址实现（B25 图谱 next 一跳、B06 资产目录）；没有登记钩子返回 None，召回会如实提示未接入。"""
    hooks = registry.hooks_for(hook)
    if not hooks:
        return None

    def lookup(identifier: str) -> dict | None:
        for fn in reversed(hooks):
            value = fn(identifier)
            if value is not None:
                return value
        return None
    return lookup


def _policy(policy: PolicyConfig, config: dict[str, Any]) -> PolicyConfig:
    """版本策略没带金额线时取 severity.yaml（configured）；PolicyConfig 里写了就以它为准。"""
    return policy if policy.major_loss_cny is not None else policy.model_copy(update={"major_loss_cny": config.get("major_loss_cny")})


def _register_unavailable(registry: Any) -> None:
    for name in ("recall", "ask_operator", "escalate"):
        registry.register_tool(name, _unavailable)
    registry.register_tool("trial", _unavailable, placeholder=True)


def _knowledge_version(registry: Any) -> int:
    store = registry.ports.get(VersionStore)
    try:
        return store.knowledge_version(store.active().version_manifest_id) if store is not None else 1
    except Exception:  # 版本存储还没激活任何清单：从 v1 起
        return 1


def _router_mode(settings: Any) -> str:
    """Nexent 打开且配了密钥才是 live；只有环境变量不足以把 mock/dev 装配切成 live（#166）。"""
    return "live" if settings.nexent.enabled and os.getenv(settings.nexent.api_key_env) else "evaluation"


def _ensure_live_run(registry: Any, settings: Any, router: Any) -> None:
    """给 live 主路由建当天运行。没有活动清单时只登记 verified 引导清单绑定本次运行，不激活全局活动版本。"""
    runs = registry.resolve(RunStore)
    versions = registry.resolve(VersionStore)
    actor = Actor(actor_id="plantkg-router", roles=("operator",))
    bootstrapped = False
    try:
        manifest = versions.active()
    except PlantkgError:
        try:
            versions.register_verifier("router-live:", lambda _: "complete")
        except ValueError:  # 已登记过（重复装配）
            pass
        manifest = versions.prepare(
            {"rules": {"ref": "router-live:rules", "content_hash": "sha256:" + "0" * 64}},
            "主路由 live 引导清单（synthetic，不激活）",
        )
        manifest = versions.check_integrity(manifest.version_manifest_id)
        bootstrapped = True
        log.warning("主路由 live 使用临时引导清单 %s（synthetic，未激活，只绑定本次运行）", manifest.version_manifest_id)
    local_start = datetime.now(ZoneInfo(settings.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_start.astimezone(timezone.utc)
    context = Context(plant_id=settings.plant_id, industry_pack=settings.industry_pack, actor=actor, channel="offline",
                      run_id=None, trace_id="router-live", as_of=start, version_manifest_id=manifest.version_manifest_id)
    run = runs.create_run(context, "scenario", Interval(start=start, end=start + timedelta(days=1)))
    router.run_id = run.run_id
    try:
        router.knowledge_version = versions.knowledge_version(run.version_manifest_id)
    except PlantkgError:  # 引导清单从未激活，没有历史版本号
        router.knowledge_version = 1
    log.info("主路由 live 起始运行 %s，清单 %s%s", run.run_id, run.version_manifest_id,
             "（引导清单，未激活）" if bootstrapped else "")

