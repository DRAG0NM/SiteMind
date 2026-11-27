"""B23 Nexent 接入：网关、专家提案包装、运行记录与 AgentResultSource 的 live 版本。

装配（bootstrap 在领域包加载之后调用 install）：
- 四个专家的 `<域>_propose` 工具总是经统一包装登记（无 Nexent 也可用：确定性提案照常落库）；
- nexent.enabled 时再装网关：读 nexent_assets/agents/ 配置并自检、登记 Gateway 端口与 /agent/run、/agent/stop、
  登记 AgentResultSource 的 live 实现（expert 经 run_expert，triage / arbitration / follow_up 经主 Agent）。
ProposalSink：B27 的实现登记后直接使用；dev/mock 未登记时用内存替身；prod 缺失即启动失败。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from plantkg.core.actions import WORKBENCH_LOGIN_PENDING
from plantkg.core.contracts import (
    Actor,
    AgentResultKind,
    AgentRunAccepted,
    AgentRunRequest,
    AgentRunStopped,
    Context,
    ErrorCode,
    PlantkgError,
    RunBinding,
    Role,
)
from plantkg.core.ports import RUN_BINDING_HOOK, AgentResultSource, AgentRunReader, EvidenceSink, Gateway, ProposalSink, RunSessionStore, RunStore
from plantkg.core.registry import Registry, RegistryError
from plantkg.integrations.nexent import proposals
from plantkg.integrations.nexent.assets import check_assets, load_assets
from plantkg.integrations.nexent.gateway import NexentGateway, timing_from_payload
from plantkg.integrations.nexent.records import MemoryAgentRunStore, SqliteAgentRunStore

ROOT = Path(__file__).resolve().parents[3]
DOCUMENT_PATHS_HOOK = "nexent.document_paths"  # fn(asset, as_of) -> 可见文档路径 | None（B26 登记）
SYNTHETIC_MANIFEST = "vm-synthetic-no-storage"


@dataclass
class NexentIntegration:
    gateway: NexentGateway | None
    store: Any


class _BindingRelay:
    """propose 包装在网关之前装配：先拿一个转发器，网关装好后接上。"""

    def __init__(self) -> None:
        self.gateway: NexentGateway | None = None

    def binding_for(self, context: Context):
        return self.gateway.binding_for(context) if self.gateway else None

    def saved(self, context: Context, proposal_id: str) -> None:
        if self.gateway:
            self.gateway.proposal_saved(context, proposal_id)


def install(registry: Registry, settings: Any, guard: Any, *, writer: Any = None, operator: Any = None) -> NexentIntegration:
    relay = _BindingRelay()
    dev = settings.profile in ("mock", "dev")
    if ProposalSink not in registry.ports:
        if settings.profile == "prod":
            raise RegistryError("prod profile 缺少 ProposalSink（B27 提供）；专家提案无法落库，拒绝启动")
        if dev:
            registry.register(ProposalSink, proposals.MemoryProposalSink(), fallback=True)
    if ProposalSink in registry.ports:  # evaluation 没有提案存取时不包装（评测走确定性路径，不经 Nexent）
        proposals.install(registry, lambda: registry.resolve(ProposalSink), relay.binding_for, relay.saved, placeholders=dev)
    registry.register_hook(RUN_BINDING_HOOK, relay.binding_for)
    store = SqliteAgentRunStore(writer, settings.plant_id) if writer is not None else MemoryAgentRunStore()
    registry.register(AgentRunReader, store)
    if not settings.nexent.enabled:
        return NexentIntegration(None, store)

    nexent = settings.nexent
    assets = load_assets(_path(nexent.agents_dir))
    check_assets(assets, registry.tools, nexent.main_agent)
    from plantkg.integrations.nexent.client import NorthboundClient

    hooks = registry.hooks_for(DOCUMENT_PATHS_HOOK)
    runs = registry.ports.get(RunStore)
    if runs is not None:
        run_manifest = lambda run_id: runs.get_run(run_id).version_manifest_id  # noqa: E731
    elif dev:  # 替身（synthetic）：未启用存储的 mock/dev 没有运行存储，绑定一个标明合成的清单号
        run_manifest = lambda run_id: SYNTHETIC_MANIFEST  # noqa: E731
    else:
        run_manifest = None  # 带 run_id 的运行会被拒绝
    gateway = NexentGateway(
        assets=assets, tool_specs={name: b.spec for name, b in registry.tools.items()}, sessions=registry.resolve(RunSessionStore),
        guard=guard, store=store, client=NorthboundClient(nexent.northbound_url, os.environ[nexent.api_key_env]),
        plant_id=settings.plant_id, default_as_of=settings.world.default_as_of, timezone=settings.timezone, main_agent=nexent.main_agent,
        session_ttl_s=settings.gateway.session_ttl_s, keepalive_s=settings.gateway.keepalive_s, run_deadline_s=nexent.run_deadline_s,
        run_retries=nexent.run_retries, max_parallel_runs=nexent.max_parallel_runs, evidence=registry.ports.get(EvidenceSink),
        document_paths=hooks[-1] if hooks else None, run_manifest=run_manifest)
    relay.gateway = gateway
    registry.register(Gateway, gateway)
    live = LiveAgentResultSource(gateway, settings)
    for kind in AgentResultKind:  # 按结果类型登记（键与 bootstrap.MODE_REQUIREMENTS 一致）
        registry.register_mode(AgentResultSource, "live", live, key=kind.value)
    from plantkg.integrations.nexent.scenario import SCENARIO_PLUGIN_HOOK, register_live_sources

    registry.register_hook(SCENARIO_PLUGIN_HOOK, register_live_sources(registry))
    for (method, path), provider in rest_providers(gateway, settings, operator).items():
        registry.register_rest(method, path, provider)
    return NexentIntegration(gateway, store)


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _as_of(value: Any, default: datetime) -> datetime:
    """payload["as_of"] 可能是 ISO 字符串（路由与会诊这么传）；网关按 datetime 比对文档时间，这里统一转换。"""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    return value if isinstance(value, datetime) else default


class LiveAgentResultSource:
    """AgentResultSource 的 live 版本：B27/B28/B29 只经这个端口要模型结果，不直接调网关。"""

    def __init__(self, gateway: NexentGateway, settings: Any) -> None:
        self._gateway = gateway
        self._actor = Actor(actor_id="plantkg-router", roles=(Role.viewer,))  # 值守循环发起的运行：服务身份，只能提议
        self._settings = settings

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        kind = AgentResultKind(kind)
        timing = timing_from_payload(payload, binding)
        as_of = _as_of(payload.get("as_of"), binding.trigger_at)
        if kind == AgentResultKind.expert:
            return self._gateway.run_expert(binding.ticket_id, str(payload["expert"]), binding=binding, actor=self._actor,
                                            brief=payload.get("brief"), as_of=as_of, timing_base=timing)
        if not payload.get("query"):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"{kind.value} 需要 payload.query")
        result = {}
        for attempt in range(self._settings.nexent.run_retries + 1):
            run = self._gateway.prepare(self._actor, str(payload["query"]), self._gateway.main_agent, as_of, kind=kind, binding=binding,
                                        timing_base=timing, retries=attempt)
            for _ in self._gateway.stream(run):
                pass
            record = run.record
            result = {"status": record.status, "agent_run_id": record.agent_run_id, "terminal_reason": record.terminal_reason,
                      "final_answer": record.final_answer, "retries": attempt, "latency_ms": record.timing.latency_ms}
            if record.terminal_reason not in ("timeout", "error", "stream_lost"):
                break
        return result


def rest_providers(gateway: NexentGateway, settings: Any, operator: Any) -> dict[tuple[str, str], Any]:
    def authenticate(headers: Mapping[str, str]) -> Actor:
        if operator is not None:
            return operator.authenticate(headers)
        default = settings.security.default_ctx
        if default.enabled and settings.profile in ("mock", "dev"):
            return Actor(actor_id=default.actor_id, roles=default.roles)
        raise PlantkgError(ErrorCode.FORBIDDEN, f"{WORKBENCH_LOGIN_PENDING}；本机请开启存储后用运维令牌")

    def run(context: Context, path: Mapping[str, str], query: Mapping[str, str], *, body: Any, headers: Mapping[str, str]):
        actor = authenticate(headers)
        request = AgentRunRequest.model_validate(body)
        prepared = gateway.prepare(actor, request.query, request.agent_name, request.as_of, conversation_id=request.conversation_id,
                                   run_id=request.run_id)
        accepted = AgentRunAccepted(agent_run_id=prepared.agent_run_id, trace_id=prepared.trace_id)
        return StreamResult(gateway.stream_async(prepared), headers={"X-Plantkg-Agent-Run": accepted.agent_run_id})

    def stop(context: Context, path: Mapping[str, str], query: Mapping[str, str], *, body: Any, headers: Mapping[str, str]) -> AgentRunStopped:
        actor = authenticate(headers)
        return AgentRunStopped(agent_run_id=path["agent_run_id"], status=gateway.stop_sync(path["agent_run_id"], actor))

    return {("POST", "/agent/run"): run, ("POST", "/agent/stop/{agent_run_id}"): stop}


@dataclass
class StreamResult:
    """流式 REST 结果：api.py 把 events（UIEvent 异步迭代器）按 text/event-stream 发出。"""

    events: Any
    headers: dict[str, str]
