"""B23 网关冒烟测试：用假 Nexent 北向（按注入表回调 plantkg 工具，模拟平台按 Agent 名投递 tool_params）。"""

from __future__ import annotations

import json
import threading
import time

import pytest

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Actor, AgentResultKind, ErrorCode, PlantkgError, Role, RunBinding, ToolResult
from plantkg.core.ports import AgentResultSource, ProposalSink
from plantkg.core.registry import Registry, RegistryError
from plantkg.integrations import nexent

ACTOR = Actor(actor_id="dev-reviewer", roles=(Role.reviewer,))


class FakeNexent:
    """script(fake, payload) 产出 SSE 事件；fake.call(agent, tool, **params) 用该 Agent 桶里的 ctx 调 plantkg 工具。"""

    def __init__(self, services, script):
        self.services, self.script, self.payloads, self.stopped = services, script, [], []

    def run(self, payload, request_id):
        self.payloads.append(payload)
        yield {"type": "conversation_created", "content": {"conversation_id": str(100 + len(self.payloads))}}
        yield from self.script(self, payload)

    def call(self, payload, agent, tool, **params):
        ctx = payload.tool_params["agents"][agent]["tools"]["plantkg_" + tool]["ctx"]
        binding = self.services.registry.tools[tool]
        return self.services.guard.call(binding.spec, binding.handler, ctx, params)

    def stop(self, conversation_id):
        self.stopped.append(conversation_id)

    def last_answer(self, conversation_id):
        return None


def answer(text, depth=0, invocation=None):
    extra = {"depth": depth, "invocation_id": invocation} if depth else {}
    return [{"type": "final_answer", "answer_phase": "delta", "content": text, **extra},
            {"type": "final_answer", "answer_phase": "complete", "content": "", **extra}]


@pytest.fixture
def services(monkeypatch):
    monkeypatch.setenv("NEXENT_API_KEY", "nb-test-key-not-real")
    services = build_services(load_settings(profile="dev", overrides={"nexent.enabled": "true"}))
    yield services
    services.close()


def binding(ticket="ticket://tk-t1", round_=1):
    return RunBinding(run_id="run-t", knowledge_version=1, ticket_id=ticket, round=round_, state_version=0, trigger_at="2026-07-01T00:05:00Z")


def test_main_agent_calls_expert_subagent_and_only_main_speaks(services):
    def script(fake, payload):
        fake.call(payload, "plantkg_main", "list_assets")
        yield {"type": "subagent_start", "depth": 1, "invocation_id": "inv-1",
               "content": json.dumps({"invocation_name": "plantkg_expert_hvac", "invocation_id": "inv-1"})}
        fake.call(payload, "plantkg_expert_hvac", "list_assets")
        yield from answer("暖通子 Agent 的原文", depth=1, invocation="inv-1")
        yield {"type": "subagent_end", "depth": 1, "invocation_id": "inv-1", "content": "{}"}
        yield from answer("主 Agent：暖通目前无异常。")

    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, script)
    events = gateway.run_sync(ACTOR, "暖通怎么样？", "plantkg_main")

    types = [e.type for e in events]
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert "".join(e.payload["text"] for e in events if e.type == "answer_delta") == "主 Agent：暖通目前无异常。"
    finished = [e.payload for e in events if e.type == "tool_finished"]
    assert [(p["agent_name"], p["depth"]) for p in finished] == [("plantkg_main", 0), ("plantkg_expert_hvac", 1)]
    # 每个 Agent 的桶各有自己的 ctx；绑知识库的能源专家另有 document_paths
    buckets = fake.payloads[0].tool_params["agents"]
    assert set(buckets) == {"plantkg_main", "plantkg_expert_energy", "plantkg_expert_elevator", "plantkg_expert_hvac", "plantkg_expert_equipment"}
    ctxs = {b["tools"]["plantkg_list_assets"]["ctx"] for b in buckets.values()}
    assert len(ctxs) == 5
    assert buckets["plantkg_expert_energy"]["tools"]["knowledge_base_search"]["document_paths"]
    record = services.nexent.store.list()[-1]
    assert record.kind == "follow_up" and record.status == "finished" and record.terminal_reason == "completed_from_deltas"
    assert {(n.agent_name, n.depth) for n in record.nodes} == {("plantkg_main", 0), ("plantkg_expert_hvac", 1)}
    assert [c.agent_name for c in record.tool_calls] == ["plantkg_main", "plantkg_expert_hvac"]
    # 运行结束即失效
    assert all(gateway._sessions.resolve(ctx) is None for ctx in ctxs)


def test_expert_with_knowledge_but_no_document_paths_is_refused(services):
    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, lambda f, p: iter(()))
    gateway._document_paths = lambda asset, as_of: ()
    with pytest.raises(PlantkgError) as exc:
        gateway.prepare(ACTOR, "能源怎么样？", "plantkg_main")
    assert exc.value.code == ErrorCode.PRECONDITION and "plantkg_expert_energy" in exc.value.message
    result = gateway.run_expert("ticket://tk-t1", "energy", binding=binding(), actor=ACTOR)
    assert result["status"] == "blocked" and fake.payloads == []


def test_mimicked_platform_context_is_not_an_answer():
    from datetime import datetime, timezone

    from plantkg.integrations.nexent.sse import RunState

    now, state = datetime.now(timezone.utc), RunState("plantkg_main")
    state.feed({"type": "model_output_thinking", "content": "<completed_action_history read_only=\"true\">", "step_id": "s", "attempt_id": "a"}, now)
    state.feed({"type": "final_answer", "content": "", "answer_phase": "complete", "reclassify_step": True, "step_id": "s"}, now)
    assert state.finished and state.root.final_answer is None


def test_reclassified_subagent_step_is_read_as_its_answer():
    """子 Agent 的最后一步被改判时，回答同样在它自己的思考流里，不串到主 Agent。"""
    from datetime import datetime, timezone

    from plantkg.integrations.nexent.sse import RunState

    now, state = datetime.now(timezone.utc), RunState("plantkg_main")
    sub = {"depth": 1, "invocation_id": "i1", "step_id": "s1"}
    state.feed({"type": "subagent_start", "content": "{}", "agent_name": "设备", **sub}, now)
    state.feed({"type": "model_output_thinking", "content": "主", "step_id": "s0", "attempt_id": "a"}, now)
    state.feed({"type": "model_output_thinking", "content": "专家回答", "attempt_id": "a", **sub}, now)
    delta = state.feed({"type": "final_answer", "content": "", "answer_phase": "complete", "reclassify_step": True, **sub}, now)
    assert delta is None and state.nodes["i1"].final_answer == "专家回答" and state.root.final_answer is None


def test_repeated_propose_in_one_run_saves_one_proposal_and_query_carries_sim_time(services):
    def script(fake, payload):
        replies = [json.loads(fake.call(payload, payload.agent_name, "hvac_propose", focus=f)) for f in ("a", "b")]
        assert replies[0]["data"]["proposal_id"] == replies[1]["data"]["proposal_id"]
        yield from answer(json.dumps({"status": "done", "diagnosis": "ok"}))

    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, script)
    source = services.registry.resolve_mode(AgentResultSource, "live", "expert")
    result = source.get(AgentResultKind.expert, binding(), {"expert": "hvac"})
    assert result["status"] == "finished" and len(result["proposal_ids"]) == 1
    assert fake.payloads[0].query.startswith("事件单 ") and "\n当前时刻：" in fake.payloads[0].query


def test_two_experts_run_in_parallel_with_their_own_ctx(services):
    barrier = threading.Barrier(2, timeout=10)  # 两路不并行就会在这里超时

    def script(fake, payload):
        agent = payload.agent_name
        barrier.wait()
        text = fake.call(payload, agent, agent.removeprefix("plantkg_expert_") + "_propose", focus="早高峰")
        proposal_id = json.loads(text)["data"]["proposal_id"]
        yield from answer(json.dumps({"status": "done", "proposal_id": proposal_id, "diagnosis": "ok"}))

    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, script)
    source = services.registry.resolve_mode(AgentResultSource, "live", "expert")
    threads, results = [], {}
    for expert in ("energy", "hvac"):
        t = threading.Thread(target=lambda e=expert: results.__setitem__(e, source.get(AgentResultKind.expert, binding(), {"expert": e})))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(30)

    assert {r["status"] for r in results.values()} == {"finished"}
    for payload in fake.payloads:  # 每路只注入该专家自己的桶
        assert set(payload.tool_params["agents"]) == {payload.agent_name}
    sink = services.registry.resolve(ProposalSink)
    for expert, result in results.items():
        (pid,) = result["proposal_ids"]
        proposal = sink.get(pid)
        assert proposal.expert == expert and proposal.binding.ticket_id == "ticket://tk-t1" and proposal.binding.round == 1
    records = services.nexent.store.list(ticket_id="ticket://tk-t1")
    assert sorted(r.agent_name for r in records) == ["plantkg_expert_energy", "plantkg_expert_hvac"]
    assert all(r.tool_calls[0].bound_params["ticket_id"] == "ticket://tk-t1" for r in records)
    assert all(r.tool_calls[0].bound_params["run_id"] == "run-t" for r in records)


def test_expert_timeout_is_retried_once_then_returned_as_failure(services):
    def script(fake, payload):
        time.sleep(1.2)
        yield from ()

    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, script)
    gateway._deadline = 0.5
    result = gateway.run_expert("ticket://tk-t2", "hvac", binding=binding("ticket://tk-t2"), actor=ACTOR)
    assert result["status"] == "timeout" and result["terminal_reason"] == "timeout" and result["retries"] == 1
    assert len(fake.payloads) == 2 and fake.stopped


def test_propose_outside_ticket_run_is_refused(services):
    def script(fake, payload):
        text = fake.call(payload, "plantkg_expert_hvac", "hvac_propose")
        assert text.startswith("PLANTKG_E_PRECONDITION")
        yield from answer("没有事件单，不能提案")

    services.nexent.gateway._client = FakeNexent(services, script)
    events = services.nexent.gateway.run_sync(ACTOR, "暖通提个方案", "plantkg_main")
    assert events[-1].type == "run_finished"


def test_client_disconnect_still_stops_remote_and_saves_record(services):
    def script(fake, payload):
        yield from answer("第一段", depth=0)[:1]
        while not fake.stopped:  # 远端一直不结束，直到收到中止请求
            time.sleep(0.05)
        yield from ()

    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, script)
    gateway._deadline = 1.0

    async def consume():
        stream = gateway.stream_async(gateway.prepare(ACTOR, "暖通怎么样？", "plantkg_main"))
        async for event in stream:
            if event.type == "answer_delta":
                break  # 工作台断开
        await stream.aclose()

    import asyncio
    asyncio.run(consume())
    for _ in range(60):
        if services.nexent.store.list():
            break
        time.sleep(0.1)
    (record,) = services.nexent.store.list()
    assert record.status == "cancelled" and fake.stopped == ["101"]
    assert not gateway._runs and gateway._slots.acquire(blocking=False)


@pytest.mark.parametrize("padding", [0, 9000], ids=["normal", "over_8kb"])
def test_blocked_tool_result_does_not_leak_into_sse_or_record(services, padding):
    leaky = lambda context, **params: ToolResult(ok=True, business_status="done",  # noqa: E731
                                                 data={"summary": "key=nb-test-key-not-real" + "x" * padding})

    def script(fake, payload):
        ctx = payload.tool_params["agents"]["plantkg_main"]["tools"]["plantkg_list_assets"]["ctx"]
        text = services.guard.call(services.registry.tools["list_assets"].spec, leaky, ctx, {})
        assert "nb-test-key" not in text
        yield from answer("好")

    services.nexent.gateway._client = FakeNexent(services, script)
    events = services.nexent.gateway.run_sync(ACTOR, "资产有哪些？", "plantkg_main")
    assert "nb-test-key" not in json.dumps([e.model_dump(mode="json") for e in events], ensure_ascii=False)
    assert "nb-test-key" not in services.nexent.store.list()[-1].model_dump_json()


def test_run_binding_carries_run_id_and_manifest_and_refuses_mismatch(services):
    gateway = services.nexent.gateway
    gateway._run_manifest = lambda run_id: "vm-fixed"
    run = gateway.prepare(ACTOR, "q", "plantkg_expert_hvac", binding=binding(), standalone=True)
    context = services.guard.context_for(services.registry.tools["list_assets"].spec, next(iter(run.ctxs)))
    assert (context.run_id, context.version_manifest_id) == ("run-t", "vm-fixed")
    gateway._release(run)
    with pytest.raises(PlantkgError, match="不一致"):
        gateway.prepare(ACTOR, "q", "plantkg_main", run_id="run-t", version_manifest_id="vm-other")
    gateway._run_manifest = None  # 没有运行存储：不静默降为空，直接拒绝
    assert gateway.run_expert("ticket://tk-t1", "hvac", binding=binding(), actor=ACTOR)["status"] == "blocked"


def test_cancel_before_conversation_id_still_stops_remote(services):
    class LateId(FakeNexent):
        def run(self, payload, request_id):
            time.sleep(0.7)
            yield {"type": "conversation_created", "content": {"conversation_id": "777"}}
            while not self.stopped:
                time.sleep(0.05)

    gateway = services.nexent.gateway
    gateway._client = fake = LateId(services, None)
    run = gateway.prepare(ACTOR, "q", "plantkg_main")
    threading.Timer(0.2, run.cancel.set).start()  # 远端已发起、会话编号还没到时取消
    assert [e.type for e in gateway.stream(run)][-1] == "run_cancelled"
    assert fake.stopped == ["777"]


def test_cancelled_while_queued_never_reaches_remote(services):
    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, lambda f, p: iter(()))
    run = gateway.prepare(ACTOR, "q", "plantkg_main")
    run.cancel.set()
    assert [e.type for e in gateway.stream(run)] == ["run_started", "run_cancelled"] and fake.payloads == []


def test_production_without_proposal_sink_fails_to_start(monkeypatch):
    settings = load_settings(profile="dev").model_copy(update={"profile": "prod"})
    with pytest.raises(RegistryError, match="ProposalSink"):
        nexent.install(Registry(), settings, guard=None)


def test_reclassified_last_step_is_read_as_final_answer():
    """平台把最后一步改判时 complete 为空，正文在同一步的 model_output_thinking 里；旧 attempt 的内容不算。"""
    from datetime import datetime, timezone

    from plantkg.integrations.nexent.sse import RunState

    now, state = datetime.now(timezone.utc), RunState("plantkg_main")
    for attempt, chunk in (("a1", "旧"), ("a2", '{"diagnosis":'), ("a2", ' "x"}')):
        state.feed({"type": "model_output_thinking", "content": chunk, "step_id": "s9", "attempt_id": attempt}, now)
    state.feed({"type": "final_answer", "content": "", "answer_phase": "complete", "reclassify_step": True, "step_id": "s9"}, now)
    assert state.finished and state.root.final_answer == '{"diagnosis": "x"}'


def test_complete_then_stream_error_keeps_finished_and_does_not_retry(services):
    """depth 0 complete 到后平台断流：按完成收尾、提案只落一份、不再整次重试。"""

    def script(fake, payload):
        text = fake.call(payload, payload.agent_name, "hvac_propose", focus="complete-then-raise")
        proposal_id = json.loads(text)["data"]["proposal_id"]
        yield {"type": "final_answer", "answer_phase": "complete",
               "content": json.dumps({"status": "done", "proposal_id": proposal_id, "diagnosis": "已给出结论"}, ensure_ascii=False)}
        raise RuntimeError("complete 之后平台断流")

    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, script)
    result = gateway.run_expert("ticket://tk-t1", "hvac", binding=binding(), actor=ACTOR)
    assert result["status"] == "finished" and result["terminal_reason"] == "completed"
    assert len(result["proposal_ids"]) == 1 and len(fake.payloads) == 1


def test_complete_past_deadline_is_still_finished(services):
    """complete 已到后越过运行截止：不再判 timeout，也不重试。"""

    def script(fake, payload):
        yield {"type": "final_answer", "answer_phase": "complete", "content": "截止前已收完整回答"}
        time.sleep(0.8)  # 越过下面的截止时间
        raise RuntimeError("断流")

    gateway = services.nexent.gateway
    gateway._client = fake = FakeNexent(services, script)
    gateway._deadline = 0.1
    result = gateway.run_expert("ticket://tk-t1", "hvac", binding=binding(), actor=ACTOR)
    assert result["status"] == "finished" and result["terminal_reason"] == "completed"
    assert len(fake.payloads) == 1


def test_stream_async_reports_run_failed_when_pipeline_raises(services):
    """合流生成器抛异常时也要给出 run_failed 终态并结束事件流，消费端不永久等待。"""
    import asyncio

    gateway = services.nexent.gateway
    gateway._client = FakeNexent(services, lambda f, p: iter(()))

    def boom(run):
        raise RuntimeError("合流线程炸了")
        yield  # pragma: no cover

    gateway._pump = boom
    run = gateway.prepare(ACTOR, "q", "plantkg_main")

    async def consume():
        async def collect():
            return [event async for event in gateway.stream_async(run)]

        return await asyncio.wait_for(collect(), timeout=2)

    events = asyncio.run(consume())
    assert [e.type for e in events] == ["run_started", "run_failed"]
    assert events[-1].payload["terminal_reason"] == "error"


def test_finish_clamps_clock_rollback(services):
    """时钟回拨时不构造返回早于发起的 AgentTiming：returned_at 取发起时刻，latency_ms 为 0。"""
    from datetime import datetime, timezone

    gateway = services.nexent.gateway
    gateway._client = FakeNexent(services, lambda f, p: iter(()))
    gateway._clock = lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)
    run = gateway.prepare(ACTOR, "q", "plantkg_main")
    events = list(gateway.stream(run))
    assert events[-1].type == "run_failed"
    assert run.record.timing.returned_at == run.requested_at and run.record.timing.latency_ms == 0


def test_tool_calls_are_written_as_evidence_and_queryable_by_agent_run_id(tmp_path, monkeypatch):
    """#214：假事件流跑一次问答后，按 agent_run_id 查到每次调用的 tool_called/tool_result。"""
    monkeypatch.setenv("NEXENT_API_KEY", "nb-test-key-not-real")
    services = build_services(load_settings(profile="dev", overrides={
        "nexent.enabled": True, "storage.enabled": True, "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"), "storage.series": str(tmp_path / "series")}))
    try:
        def script(fake, payload):
            fake.call(payload, "plantkg_main", "list_assets")
            yield {"type": "subagent_start", "depth": 1, "invocation_id": "inv-1",
                   "content": json.dumps({"invocation_name": "plantkg_expert_hvac", "invocation_id": "inv-1"})}
            fake.call(payload, "plantkg_expert_hvac", "list_assets")
            yield from answer("暖通目前无异常。")

        gateway = services.nexent.gateway
        gateway._client = FakeNexent(services, script)
        gateway._run_manifest = lambda run_id: "vm-214"
        run = gateway.prepare(ACTOR, "暖通怎么样？", "plantkg_main", run_id="run-214")
        list(gateway.stream(run))
        record = run.record
        assert record.status == "finished" and len(record.tool_calls) == 2

        provider = services.registry.rest[("GET", "/evidence")]
        page = provider(services.guard.anonymous_read_context(), {}, {"agent_run_id": record.agent_run_id, "page_size": "200"})
        tool_events = [e for e in page.items if e.event_type in ("tool_called", "tool_result")]
        assert sum(e.event_type == "tool_called" for e in tool_events) == 2
        assert sum(e.event_type == "tool_result" for e in tool_events) == 2
        assert {e.agent_run_id for e in tool_events} == {record.agent_run_id}
        assert {e.run_id for e in tool_events} == {"run-214"}
        assert {e.call_id for e in tool_events} == {c.call_id for c in record.tool_calls}
    finally:
        services.close()
