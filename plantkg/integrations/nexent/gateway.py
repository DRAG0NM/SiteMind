"""plantkg 网关（03 §6.1、§7）：登记运行会话 → 按 Agent 名分桶注入 ctx 与 document_paths → 调 Nexent 北向 → 合流事件 → 落库运行记录。

一次运行里每个参与的 Agent（发起的 Agent 与它的子 Agent）各登记一个会话、各拿一个 ctx：
Nexent 按 Agent 名精确投递 tool_params，子 Agent 只用自己的桶（P21a/b），所以专家之间、主 Agent 与专家之间互不串用身份。
工具调用以 plantkg 自己的调用记录为准（ToolGuard 观察者，按 ctx 归属到 Agent）；SSE 只取回答文本与子 Agent 节点。
"""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.core.contracts import (
    Actor,
    AgentResultKind,
    AgentRunNode,
    AgentRunRecord,
    AgentTiming,
    AgentToolCall,
    Context,
    ContextChannel,
    ErrorCode,
    EvidenceEvent,
    NexentRunPayload,
    PlantkgError,
    RunBinding,
    RunMode,
    UIEvent,
)
from plantkg.core.security import new_trace_id
from plantkg.integrations.nexent.assets import AgentAsset, experts_by_domain, participants, tool_params
from plantkg.integrations.nexent.sse import RunState, terminal

log = logging.getLogger("plantkg.gateway")
POLL_S = 0.5
RETRYABLE = frozenset({"timeout", "error", "stream_lost"})  # 超时或模型连接中断重试一次（03 §3.0 运行截止）

# Nexent 的 CoreAgent 只执行 <code>…</code> 容器里的动作（code_block_tags 被置空，markdown 代码块不会被提取，
# 会被直接当成最终答案结束本次运行）。模型侧提示词改不动平台自带的系统段时，把格式要求放进最后一条用户消息，
# 是唯一贴近生成点、又不改共享平台的位置。弱模型（本机 4B）只有在这里看到示例才会稳定输出 <code>。
OUTPUT_PROTOCOL_HINT = (
    "\n【执行格式（最高优先级；违反则本轮工具不会执行，等于没做）】\n"
    "1. 第一步必须真正执行工具：把要执行的代码放进 <code></code>，例如：\n"
    "<code>result = plantkg_list_assets(page_size=1)\nprint(result)</code>\n"
    "2. 禁止使用 markdown 代码块（三个反引号）；工具函数已直接可用，不要写 import / from / def。\n"
    "3. 禁止用“等待工具返回”“等待提案结果”之类的话结束本轮；本轮没拿到工具 Observation 就不许下结论。\n"
    "4. 拿到工具返回后，才输出 <final_answer>结论</final_answer>。"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Run:
    agent_run_id: str
    trace_id: str
    asset: AgentAsset
    actor: Actor
    query: str
    kind: AgentResultKind
    as_of: datetime
    binding: RunBinding | None
    timing_base: tuple[datetime, datetime, datetime]  # origin_event_at, trigger_at, effective_at（仿真时间）
    payload: NexentRunPayload
    ctxs: dict[str, tuple[str, int]]  # ctx → (Agent 名, depth)
    run_id: str | None = None  # 请求或事件单绑定的运行号（#214：工具证据带上它）
    retries: int = 0
    events: queue.Queue = field(default_factory=queue.Queue)
    cancel: threading.Event = field(default_factory=threading.Event)
    seq: int = 0
    tool_calls: list[AgentToolCall] = field(default_factory=list)
    proposal_ids: list[str] = field(default_factory=list)
    requested_at: datetime = field(default_factory=_utcnow)
    record: AgentRunRecord | None = None

    @property
    def timing_for_proposals(self) -> AgentTiming:
        origin, trigger, effective = self.timing_base
        return AgentTiming(origin_event_at=origin, trigger_at=trigger, effective_at=effective, requested_at=self.requested_at,
                           retries=self.retries)


class NexentGateway:
    """Gateway 端口实现。run/stop 是异步接口（工作台 SSE）；run_sync / run_expert 供后台与会诊（B29）在线程里调用。"""

    def __init__(self, *, assets: Mapping[str, AgentAsset], tool_specs: Mapping[str, Any], sessions: Any, guard: Any, store: Any,
                 client: Any, plant_id: str, default_as_of: datetime, timezone: str, main_agent: str, session_ttl_s: int, keepalive_s: float,
                 run_deadline_s: float, run_retries: int, max_parallel_runs: int, evidence: Any = None,
                 document_paths: Callable[[AgentAsset, datetime], tuple[str, ...] | None] | None = None,
                 run_manifest: Callable[[str], str] | None = None, clock: Callable[[], datetime] = _utcnow) -> None:
        self.assets, self._specs, self._sessions, self._store, self._client = assets, tool_specs, sessions, store, client
        self._run_manifest = run_manifest  # run_id → 该运行固定的版本清单（RunStore）
        self._plant_id, self._default_as_of, self.main_agent = plant_id, default_as_of, main_agent
        self._timezone = ZoneInfo(timezone)
        self._ttl, self._keepalive, self._deadline, self._retries = session_ttl_s, keepalive_s, run_deadline_s, run_retries
        self._evidence, self._document_paths, self._clock = evidence, document_paths, clock
        self._slots = threading.BoundedSemaphore(max_parallel_runs)
        self._runs: dict[str, _Run] = {}
        self._by_ctx: dict[str, _Run] = {}
        self._lock = threading.Lock()
        guard.observers.append(self._observe)

    # ------------------------------------------------------------ 端口：异步接口
    async def run(self, actor: Actor, query: str, agent_name: str, as_of: datetime | None = None, **options: Any) -> AsyncIterator[UIEvent]:
        from starlette.concurrency import run_in_threadpool

        run = await run_in_threadpool(lambda: self.prepare(actor, query, agent_name, as_of, **options))
        async for event in self.stream_async(run):
            yield event

    async def stream_async(self, run: _Run) -> AsyncIterator[UIEvent]:
        """合流在独立线程里跑完：工作台断开只置 cancel，线程照常停远端、落记录、还槽位，也不占 Starlette 线程池。"""
        import asyncio

        loop, events = asyncio.get_running_loop(), asyncio.Queue()

        def pump() -> None:
            try:
                for event in self.stream(run):
                    try:
                        loop.call_soon_threadsafe(events.put_nowait, event)
                    except RuntimeError:  # 事件循环已关闭：继续跑完收尾
                        pass
            except Exception as exc:  # noqa: BLE001 — 合流自身出错也要给消费端一个终态
                log.exception("运行 %s 的事件流异常中止", run.agent_run_id)
                try:
                    loop.call_soon_threadsafe(events.put_nowait, self._event(
                        run, "run_failed", terminal_reason="error", status="failed", message=f"运行异常中止：{type(exc).__name__}"))
                except RuntimeError:
                    pass
            finally:
                try:
                    loop.call_soon_threadsafe(events.put_nowait, None)
                except RuntimeError:
                    pass

        threading.Thread(target=pump, name=f"gateway-{run.agent_run_id}", daemon=True).start()
        try:
            while (event := await events.get()) is not None:
                yield event
        finally:
            run.cancel.set()  # 工作台断开连接即中止这次运行

    async def stop(self, agent_run_id: str, actor: Actor) -> None:
        self.stop_sync(agent_run_id, actor)

    def stop_sync(self, agent_run_id: str, actor: Actor) -> str:
        run = self._runs.get(agent_run_id)
        if run is None:
            return "already_finished"
        if run.actor.actor_id != actor.actor_id and "reviewer" not in actor.roles:
            raise PlantkgError(ErrorCode.FORBIDDEN, "只能中止自己发起的运行")
        run.cancel.set()
        return "stopping"

    # ------------------------------------------------------------ 同步接口
    def run_sync(self, actor: Actor, query: str, agent_name: str, as_of: datetime | None = None, **options: Any) -> list[UIEvent]:
        run = self.prepare(actor, query, agent_name, as_of, **options)
        return list(self.stream(run))

    def run_expert(self, ticket_id: str, expert: str, *, binding: RunBinding, actor: Actor, brief: str | None = None,
                   as_of: datetime | None = None, timing_base: tuple[datetime, datetime, datetime] | None = None) -> dict[str, Any]:
        """会诊入口：直接发起该专家 Agent 的北向运行，只注入该专家的 ctx 与 document_paths；最多同时 4 路。
        超时或模型连接中断重试一次，仍失败把失败结果交回调用方，不抛异常（不中断值守循环）。"""
        if binding.ticket_id != ticket_id:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "binding.ticket_id 必须等于 ticket_id")
        asset = experts_by_domain(self.assets).get(expert) or self.assets.get(expert)
        if asset is None or asset.role != "expert":
            raise PlantkgError(ErrorCode.NOT_FOUND, f"没有专家 {expert}")
        query = brief or expert_brief(asset, binding)
        record = None
        for attempt in range(self._retries + 1):
            try:
                run = self.prepare(actor, query, asset.name, as_of, kind=AgentResultKind.expert, binding=binding, timing_base=timing_base,
                                   retries=attempt, standalone=True)
            except PlantkgError as exc:
                return {"status": "blocked", "expert": expert, "message": exc.message, "agent_run_id": None, "proposal_ids": []}
            for _ in self.stream(run):
                pass
            record = run.record
            if record.terminal_reason is None or record.terminal_reason not in RETRYABLE:
                break
            if record.proposal_ids or record.final_answer:  # 本轮已有提案或回答：重试只会重复计费
                log.info("专家运行 %s 已落库 %d 份提案/已收回答，不再重试", record.agent_run_id, len(record.proposal_ids))
                break
        return expert_result(expert, record)

    # ------------------------------------------------------------ 准备：登记会话与注入表
    def prepare(self, actor: Actor, query: str, agent_name: str, as_of: datetime | None = None, *,
                kind: AgentResultKind = AgentResultKind.follow_up, binding: RunBinding | None = None,
                timing_base: tuple[datetime, datetime, datetime] | None = None, conversation_id: str | None = None,
                run_id: str | None = None, version_manifest_id: str | None = None, retries: int = 0, standalone: bool = False) -> _Run:
        asset = self.assets.get(agent_name)
        if asset is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"Nexent Agent {agent_name} 没有配置（nexent_assets/agents/）")
        as_of = as_of or self._default_as_of
        members = [(asset, 0)] if standalone else participants(self.assets, agent_name)
        documents = {}
        for member, _ in members:  # 先全部检查，再登记：任何一位缺 document_paths 都不发起运行
            docs = self._documents(member, as_of)
            if member.knowledge is not None and not docs:
                raise PlantkgError(ErrorCode.PRECONDITION, f"{member.name} 绑定了知识库检索，但没有可见文档清单 document_paths，拒绝运行"
                                                         "（漏注入会让检索读到全部版本，P21c）")
            documents[member.name] = docs
        if binding is not None:
            run_id = run_id or binding.run_id
        manifest = self._manifest_for(run_id, version_manifest_id) if run_id else None
        agent_run_id, trace_id = "ar-" + secrets.token_hex(8), new_trace_id()
        ctxs, buckets = {}, []
        for member, depth in members:
            session = self._sessions.register(actor, self._plant_id, as_of, documents[member.name], self._ttl, agent_run_id, trace_id,
                                              run_id=run_id, version_manifest_id=manifest)
            ctxs[session.ctx] = (member.name, depth)
            buckets.append((member, session.ctx, documents[member.name]))
        local = as_of.astimezone(self._timezone)
        stamped = (f"{query}{OUTPUT_PROTOCOL_HINT}\n当前时刻：{local:%Y-%m-%d %H:%M}。判断日期、季节和峰谷都以此为准，不用系统日期。"
                   )  # 放末尾：专家按任务开头区分会诊与问答
        payload = NexentRunPayload(agent_name=agent_name, query=stamped, conversation_id=conversation_id,
                                   tool_params=tool_params(buckets, self._specs))
        trigger = binding.trigger_at if binding else as_of
        run = _Run(agent_run_id, trace_id, asset, actor, query, kind, as_of, binding, timing_base or (trigger, trigger, trigger), payload,
                   ctxs, run_id=run_id, retries=retries)
        with self._lock:
            self._runs[agent_run_id] = run
            for ctx in ctxs:
                self._by_ctx[ctx] = run
        return run

    def _manifest_for(self, run_id: str, claimed: str | None) -> str:
        """运行绑定只认运行存储里固定的清单；查不到或与请求声明的不一致就拒绝，不降为空。"""
        if self._run_manifest is None:
            raise PlantkgError(ErrorCode.PRECONDITION, f"运行 {run_id} 需要运行存储解析版本清单，但 RunStore 未登记")
        manifest = self._run_manifest(run_id)
        if claimed and claimed != manifest:
            raise PlantkgError(ErrorCode.PRECONDITION, f"运行 {run_id} 固定的清单是 {manifest}，与请求的 {claimed} 不一致")
        return manifest

    def _documents(self, asset: AgentAsset, as_of: datetime) -> tuple[str, ...]:
        if asset.knowledge is None:
            return ()
        if self._document_paths is not None:
            docs = self._document_paths(asset, as_of)
            if docs is not None:
                return tuple(docs)
        return asset.knowledge.document_paths

    # ------------------------------------------------------------ 合流
    def stream(self, run: _Run) -> Iterator[UIEvent]:
        acquired = self._slots.acquire(timeout=self._deadline)
        try:
            yield self._event(run, "run_started", agent_name=run.asset.name, kind=run.kind.value)
            if not acquired or run.cancel.is_set():  # 排队期间已取消：不再发起远端运行
                yield from self._finish(run, RunState(run.asset.name), cancelled=run.cancel.is_set(), deadline_hit=not acquired,
                                        recovered=None)
                return
            yield from self._pump(run)
        finally:
            if acquired:
                self._slots.release()
            self._release(run)

    def _pump(self, run: _Run) -> Iterator[UIEvent]:
        state = RunState(run.asset.name)
        reader = threading.Thread(target=self._read, args=(run,), name=f"nexent-{run.agent_run_id}", daemon=True)
        reader.start()
        start = time.monotonic()
        last_sent, evidence_cursor = start, None
        cancelled = deadline_hit = eof = stop_sent = False
        while not eof:
            try:
                item = run.events.get(timeout=POLL_S)
            except queue.Empty:
                item = None
            now = self._clock()
            if item is not None:
                tag = item[0]
                if tag == "eof":
                    eof = True
                    if item[1] is not None and not (cancelled or deadline_hit or state.finished):
                        state.error = state.error or item[1]
                elif tag == "sse":
                    text = state.feed(item[1], now)
                    if text:
                        last_sent = time.monotonic()
                        yield self._event(run, "answer_delta", text=text)
                elif tag == "tool":
                    last_sent = time.monotonic()
                    yield self._tool_event(run, item[1], item[2])
            if self._evidence is not None:
                for event in self._poll_evidence(run, evidence_cursor):
                    evidence_cursor = str(event.seq)
                    if event.event_type in ("tool_called", "tool_result"):
                        continue
                    last_sent = time.monotonic()
                    yield self._event(run, "evidence_added", evidence_id=event.event_id, event_type=event.event_type,
                                      tool=event.tool, summary=event.summary)
            if not (cancelled or deadline_hit or state.finished):
                if run.cancel.is_set():
                    cancelled = True
                elif time.monotonic() - start > self._deadline:
                    deadline_hit = True
            if (cancelled or deadline_hit) and not stop_sent and state.conversation_id:  # 会话编号晚到也补发中止
                stop_sent = True
                self._stop_remote(state)
            if (cancelled or deadline_hit) and time.monotonic() - start > self._deadline + 10:
                break  # 中止后最多再等 10 秒收尾，不被卡住
            if not eof and time.monotonic() - last_sent >= self._keepalive:
                last_sent = time.monotonic()
                yield self._event(run, "keepalive")
        recovered = None
        if not (state.finished or cancelled or deadline_hit or state.error) and state.conversation_id:
            recovered = self._client.last_answer(state.conversation_id)
            if recovered:
                state.root.complete = recovered
        yield from self._finish(run, state, cancelled=cancelled, deadline_hit=deadline_hit, recovered=recovered)

    def _read(self, run: _Run) -> None:
        error, started = None, False
        try:
            for event in self._client.run(run.payload, run.agent_run_id):
                run.events.put(("sse", event))
                started = started or event.get("type") == "conversation_created"
                if run.cancel.is_set() and started:  # 拿到会话编号前继续读，合流才能据此中止远端
                    break
        except PlantkgError as exc:
            error = exc.message
        except Exception as exc:  # noqa: BLE001 — 连接中断等，由终态判定处理
            error = f"Nexent 连接中断：{type(exc).__name__}"
        run.events.put(("eof", error))

    def _stop_remote(self, state: RunState) -> None:
        if state.conversation_id:
            try:
                self._client.stop(state.conversation_id)
            except Exception:  # noqa: BLE001
                log.warning("Nexent 中止请求失败（会话 %s）", state.conversation_id)

    def _poll_evidence(self, run: _Run, cursor: str | None) -> list[Any]:
        context = Context(plant_id=self._plant_id, industry_pack="-", actor=run.actor, channel=ContextChannel.gateway,
                          agent_run_id=run.agent_run_id, trace_id=run.trace_id, as_of=run.as_of)
        try:
            return list(self._evidence.list(None, cursor, context, agent_run_id=run.agent_run_id).items)
        except Exception:  # noqa: BLE001 — 证据合流失败不影响运行
            log.exception("证据事件读取失败")
            return []

    # ------------------------------------------------------------ 工具调用（ToolGuard 观察者，运行在 MCP 工具线程里）
    def _observe(self, phase: str, call: dict[str, Any]) -> None:
        run = self._by_ctx.get(call.get("ctx") or "")
        if run is not None:
            agent_name, depth = run.ctxs[call["ctx"]]
            run.events.put(("tool", phase, {**call, "agent_name": agent_name, "depth": depth}))
            self._record_tool_evidence(run, phase, call)

    def _record_tool_evidence(self, run: _Run, phase: str, call: dict[str, Any]) -> None:
        """#214：工具调用写成证据事件，供 /api/v1/evidence?agent_run_id= 查；写入失败只记日志，不影响问答。"""
        if self._evidence is None:
            log.debug("未装配证据端口，跳过工具调用证据（%s）", run.agent_run_id)
            return
        try:
            context: Context = call["context"]
            finished = phase == "finished"
            result = call["result"] if finished else None
            self._evidence.append(EvidenceEvent(
                event_id=f"b23-tool-{run.agent_run_id}-{call['call_id']}-{phase}", seq=0, occurred_at=context.as_of,
                agent_run_id=run.agent_run_id, run_id=run.run_id, trace_id=run.trace_id, call_id=call["call_id"],
                event_type="tool_result" if finished else "tool_called", tool=call["tool"],
                result_status=_result_status(result) if finished else "pending",
                params=_bound_params(run, context), model_params=dict(call["params"]),
                summary=_summary(result) if finished else None))
        except Exception:  # noqa: BLE001 — 证据写失败不影响工具调用与问答
            log.exception("工具调用证据写入失败（%s，%s）", run.agent_run_id, call["call_id"])

    def _tool_event(self, run: _Run, phase: str, call: dict[str, Any]) -> UIEvent:
        common = {"tool": call["tool"], "agent_name": call["agent_name"], "depth": call["depth"]}
        if phase == "started":
            return self._event(run, "tool_started", call_id=call["call_id"], model_params=call["params"], **common)
        result = call["result"]
        duration = int((call["finished_at"] - call["started_at"]).total_seconds() * 1000)
        context: Context = call["context"]
        bound = _bound_params(run, context)
        summary = _summary(result)
        run.tool_calls.append(AgentToolCall(call_id=call["call_id"], agent_name=call["agent_name"], depth=call["depth"], tool=call["tool"],
                                            model_params=call["params"], bound_params=bound,
                                            started_at=call["started_at"], duration_ms=max(duration, 0),
                                            business_status=result.business_status, summary=summary))
        return self._event(run, "tool_finished", call_id=call["call_id"], business_status=result.business_status,
                           duration_ms=duration, bound_params=bound, summary=summary, **common)

    def binding_for(self, context: Context) -> tuple[RunBinding, AgentTiming] | None:
        run = self._runs.get(context.agent_run_id or "")
        if run is None or run.binding is None:
            return None
        return run.binding, run.timing_for_proposals

    def proposal_saved(self, context: Context, proposal_id: str) -> None:
        run = self._runs.get(context.agent_run_id or "")
        if run is not None:
            run.proposal_ids.append(proposal_id)

    # ------------------------------------------------------------ 收尾
    def _finish(self, run: _Run, state: RunState, *, cancelled: bool, deadline_hit: bool, recovered: str | None) -> Iterator[UIEvent]:
        kind, reason, message = terminal(state, cancelled=cancelled, deadline_hit=deadline_hit, recovered=recovered)
        returned = max(self._clock(), run.requested_at)  # 时钟回拨时不能早于发起时刻
        status = {"run_finished": "finished", "run_cancelled": "cancelled"}.get(kind, "timeout" if reason == "timeout" else "failed")
        origin, trigger, effective = run.timing_base
        timing = AgentTiming(origin_event_at=origin, trigger_at=trigger, effective_at=effective, requested_at=run.requested_at,
                             returned_at=returned, latency_ms=max(0, int((returned - run.requested_at).total_seconds() * 1000)),
                             retries=run.retries)
        nodes = tuple(AgentRunNode(node_id=n.node_id, agent_name=n.agent_name, depth=n.depth, parent_id=n.parent_id, started_at=n.started_at,
                                   finished_at=n.finished_at, final_answer=n.final_answer) for n in state.nodes.values())
        run.record = AgentRunRecord(
            agent_run_id=run.agent_run_id, kind=run.kind, agent_name=run.asset.name, run_mode=RunMode.live, source="nexent",
            binding=run.binding, timing=timing, status=status, proposal_ids=tuple(run.proposal_ids), model=run.asset.model,
            conversation_id=state.conversation_id, query=run.query, terminal_reason=reason, nodes=nodes,
            tool_calls=tuple(run.tool_calls), final_answer=state.root.final_answer)
        try:
            self._store.save(run.record)
        except Exception:  # noqa: BLE001 — 记录失败不改变运行结果
            log.exception("agent_runs 落库失败（%s）", run.agent_run_id)
        payload: dict[str, Any] = {"terminal_reason": reason, "status": status}
        if message:
            payload["message"] = message
        if run.proposal_ids:
            payload["proposal_ids"] = list(run.proposal_ids)
        yield self._event(run, kind, **payload)

    def _release(self, run: _Run) -> None:
        with self._lock:
            self._runs.pop(run.agent_run_id, None)
            for ctx in run.ctxs:
                self._by_ctx.pop(ctx, None)
        for ctx in run.ctxs:  # 运行结束即失效
            self._sessions.expire(ctx)

    def _event(self, run: _Run, event_type: str, *, call_id: str | None = None, **payload: Any) -> UIEvent:
        event = UIEvent(type=event_type, agent_run_id=run.agent_run_id, trace_id=run.trace_id, seq=run.seq, ts=self._clock(), call_id=call_id,
                        payload={k: v for k, v in payload.items() if v is not None})
        run.seq += 1
        return event


def _summary(result: Any) -> str | None:
    if result.error is not None:
        return f"{result.error.code}: {result.error.message}"[:500]
    data = result.data or {}
    text = data.get("summary") if isinstance(data.get("summary"), str) else None
    return (text or result.notice or result.business_status)[:500]


def _bound_params(run: _Run, context: Context) -> dict[str, Any]:
    """工具调用里系统绑定的值（会话给出的厂区、身份、as_of、运行）；去掉空值，注入参数不在其中。"""
    bound = {"plant_id": context.plant_id, "actor": context.actor.actor_id if context.actor else None,
             "as_of": context.as_of.isoformat(), "run_id": context.run_id}
    if run.binding is not None:
        bound |= {"ticket_id": run.binding.ticket_id, "round": run.binding.round}
    return {k: v for k, v in bound.items() if v is not None}


def _result_status(result: Any) -> str:
    """ToolResult.business_status → EvidenceEvent.result_status（done 记 ok，其余同名）。"""
    return {"done": "ok"}.get(result.business_status, result.business_status)


def expert_brief(asset: AgentAsset, binding: RunBinding) -> str:
    tool = f"plantkg_{asset.domain}_propose"
    return (f"事件单 {binding.ticket_id} 第 {binding.round} 轮会诊（触发时刻 {binding.trigger_at.isoformat()}）。"
            f"【必须执行】第一步先调用一次工具 {tool}，且只传它自己的参数（没有把握就只传 focus），例如："
            f'<code>result = {tool}(focus="事件单 {binding.ticket_id} 第 {binding.round} 轮会诊")\nprint(result)</code>；'
            "拿到工具返回的 proposal_id 之前，不要用“等待工具返回”“等待提案结果”这类话结束本轮。"
            "拿到工具返回之后，只用一行 JSON 回答："
            '{"status":"done|waiting|blocked|needs_clarification","proposal_id":"<工具返回的 proposal_id>",'
            '"diagnosis":"<一句话诊断>","evidence_refs":[],"question":null}')


def expert_result(expert: str, record: AgentRunRecord | None) -> dict[str, Any]:
    if record is None:
        return {"status": "failed", "expert": expert, "agent_run_id": None, "proposal_ids": []}
    return {"status": record.status, "expert": expert, "agent_run_id": record.agent_run_id, "terminal_reason": record.terminal_reason,
            "final_answer": record.final_answer, "proposal_ids": list(record.proposal_ids), "retries": record.timing.retries,
            "latency_ms": record.timing.latency_ms, "coverage_gap": record.coverage_gap}


def timing_from_payload(payload: Mapping[str, Any], binding: RunBinding) -> tuple[datetime, datetime, datetime]:
    """调用方可在 payload 里给 origin_event_at / effective_at（仿真时间）；缺省按 trigger_at。"""
    def parse(key: str, default: datetime) -> datetime:
        value = payload.get(key)
        return datetime.fromisoformat(value) if isinstance(value, str) else value if isinstance(value, datetime) else default
    trigger = binding.trigger_at
    return parse("origin_event_at", trigger), trigger, parse("effective_at", trigger + timedelta(0))
