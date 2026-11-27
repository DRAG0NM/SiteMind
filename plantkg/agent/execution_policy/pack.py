"""B47 领域包入口：把分级执行接进注册表与场景注册表。

有存储时在主注册表装完整服务（工单、队列、临时规则都落库）；场景注册表另有一份装配：
主装配在时只补上 B28 执行钩子、场景确认钩子与 ForkHook；纯 packs 场景（没有存储）在第一次绑定时
用运行自己的库建执行表。
"""

from __future__ import annotations

from typing import Any, Callable

from plantkg.core.contracts import (
    ActionCancelRequest, ActionRevokeRequest, AppliedAction, AutonomyPauseRequest, AutonomyResumeRequest, Page,
)
from plantkg.core.ports import Repository

from .service import (
    EXECUTION_HOOK, FORK_HOOK, INSTRUCTION_HOOK, PROBE_HOOK, SCENARIO_BIND_HOOK, SCENARIO_CONFIRM_HOOK, SCENARIO_HOOK,
    STEP_HOOK, URGENT_HOOK, ExecutionPolicyService, PendingActionHandler,
)
from .store import ExecutionStore

_TASK_KINDS = ("work_order", "human_decision", "correction", "sampling", "review")
_GRAPH_HOOKS = ("router.playbooks", "router.action_safety", "router.action_consequence")


def install(registry: Any, store: ExecutionStore, plant_id: str) -> ExecutionPolicyService:
    service = ExecutionPolicyService(registry, store, plant_id=plant_id)
    for name, fn in ((URGENT_HOOK, service.on_urgent), (INSTRUCTION_HOOK, service.on_instruction),
                     (PROBE_HOOK, service.on_probe), (STEP_HOOK, service.step), (SCENARIO_BIND_HOOK, service.bind)):
        registry.register_hook(name, fn)
    registry.register_hook('evolution.authorization_published', service.authorization_published)
    for kind in _TASK_KINDS:
        registry.register_action_handler(kind, PendingActionHandler(service, kind))
    return service


def register(registry: Any, settings: Any = None) -> None:
    repository = registry.ports.get(Repository)
    writer = getattr(repository, "writer", None)
    plant_id = str(getattr(settings, "plant_id", "") or getattr(repository, "plant_id", "") or "")
    service = install(registry, ExecutionStore(writer, plant_id), plant_id) if writer is not None else None
    registry.register_hook(SCENARIO_HOOK, _scenario_plugin(service, registry))


def _scenario_plugin(main: ExecutionPolicyService | None, source: Any) -> Callable[[Any], None]:
    def plugin(scenario_registry: Any) -> None:
        # 图谱的 playbooks / 动作安全 / 后果档位只在装配注册表上：转给场景注册表，B47 的判定才用得上
        for name in _GRAPH_HOOKS:
            for hook in source.hooks_for(name):
                scenario_registry.register_hook(name, hook)
        if main is not None:  # 主装配已在：场景注册表只补 B27 不转发的三个钩子
            scenario_registry.register_hook(EXECUTION_HOOK, main.execute)
            scenario_registry.register_hook(FORK_HOOK, main.on_fork)
            scenario_registry.register_hook(SCENARIO_CONFIRM_HOOK, main.confirm_from_scenario)
            return
        service = ExecutionPolicyService(scenario_registry, None)
        for name, fn in ((URGENT_HOOK, service.on_urgent), (INSTRUCTION_HOOK, service.on_instruction),
                         (PROBE_HOOK, service.on_probe), (STEP_HOOK, service.step),
                         (SCENARIO_BIND_HOOK, service.bind), (EXECUTION_HOOK, service.execute),
                         (FORK_HOOK, service.on_fork), (SCENARIO_CONFIRM_HOOK, service.confirm_from_scenario)):
            scenario_registry.register_hook(name, fn)
    return plugin


def rest_providers(service: ExecutionPolicyService, operator: Any) -> dict[tuple[str, str], Callable[..., Any]]:
    """#245 人工干预的 REST 提供者：写入与 actions 端点一样只认本机运维令牌（operator.authenticate）。"""
    override = service.override

    def run_of(query: Any) -> str | None:
        return query.get("run_id") or None

    def status(context, path, query):
        return override.status(run_of(query))

    def applied(context, path, query):
        return Page[AppliedAction](items=tuple(override.applied_actions(run_of(query))), as_of=context.as_of)

    def preview(context, path, query):
        return override.revoke_preview(path["action_id"], run_of(query))

    def pause(context, path, query, *, body, headers):
        actor = operator.authenticate(headers)
        request = AutonomyPauseRequest.model_validate(body)
        return override.pause(actor, request.reason, request.run_id)

    def resume(context, path, query, *, body, headers):
        actor = operator.authenticate(headers)
        return override.resume(actor, AutonomyResumeRequest.model_validate(body).run_id)

    def cancel(context, path, query, *, body, headers):
        actor = operator.authenticate(headers)
        request = ActionCancelRequest.model_validate(body)
        return override.cancel(path["action_id"], actor, request.reason, request.run_id, request.confirm_protection)

    def revoke(context, path, query, *, body, headers):
        actor = operator.authenticate(headers)
        request = ActionRevokeRequest.model_validate(body)
        return override.revoke(path["action_id"], actor, request.reason_kind, request.note, request.run_id,
                               request.confirm_protection)

    return {("POST", "/autonomy/pause"): pause, ("POST", "/autonomy/resume"): resume,
            ("GET", "/autonomy/status"): status, ("GET", "/autonomy/actions"): applied,
            ("POST", "/actions/{action_id}/cancel"): cancel, ("POST", "/actions/{action_id}/revoke"): revoke,
            ("GET", "/actions/{action_id}/revoke-preview"): preview}


def register_rest(registry: Any, operator: Any) -> None:
    """装配层调用：主注册表有执行服务（需要存储）时登记人工干预的 REST 路由。"""
    service = next((owner for hook in registry.hooks_for(URGENT_HOOK)
                    if isinstance(owner := getattr(hook, "__self__", None), ExecutionPolicyService)), None)
    if service is None:
        return
    for (method, path), provider in rest_providers(service, operator).items():
        registry.register_rest(method, path, provider)
