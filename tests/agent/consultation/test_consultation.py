"""B29 验收冒烟：专家并行、回答挂接与过期、预演命中与丢弃、受阻与分歧。"""

from __future__ import annotations

import json
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from plantkg.agent.consultation import (
    PREVIEW_CHECK_KEY, PREVIEW_SIGNALS_HOOK, PREVIEW_STATE_HOOK, PreviewSignal, install as install_consultation,
)
from plantkg.agent.router import PROPOSALS_READY_HOOK, REPLIES_READY_HOOK, install as install_router
from plantkg.agent.router.severity import SeverityEstimator
from plantkg.backends.judge import FakeJudge
from plantkg.core.contracts import (
    AgentResultKind, DayEvent, ExpertProposal, Interval, PolicyConfig, ProposalCost, ProposedAction, ResponseBudget, RunBinding,
)
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.integrations.nexent.proposals import wrap
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter
from tests.agent.router.helpers import AIR, PLANT, SEVERITY, T0

EXPERTS = ("energy", "hvac", "elevator", "equipment")
TZ = timezone(timedelta(hours=8))


def local(hour: int, minute: int, day: int = 1) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def event(event_id: str, at: datetime, event_type: str = "load_spike", entity: str | None = AIR, severity: int = 3) -> DayEvent:
    return DayEvent(event_id=event_id, at=at, event_type=event_type, source="script", entity_id=entity, severity=severity,
                    summary=f"{event_type} 合成事件", payload={}, nature="synthetic")


class CoverAll:
    def covers(self, event) -> bool:
        return True


def policy() -> PolicyConfig:
    return PolicyConfig(policy_version="p-demo", arbitration_weights={"cny": 1.0}, priority_profiles={"default": ("safety", "cost")},
                        response_budget=ResponseBudget(layer12_min=1, layer3_min=3), major_loss_cny=50_000)


def action(entity: str, command: str, at: datetime, minutes: int = 15, **params) -> ProposedAction:
    return ProposedAction(entity_id=entity, command=command, params=params,
                          interval=Interval(start=at, end=at + timedelta(minutes=minutes)))


def draft(expert: str, binding: RunBinding, diagnosis: str | None = None) -> ExpertProposal:
    return ExpertProposal(expert=expert, ticket_id=binding.ticket_id, cost=ProposalCost(cny=-1.0), confidence=0.8,
                          actions=(action(f"entity://{PLANT}/battery/BAT1", "discharge", binding.trigger_at),),
                          diagnosis=diagnosis or f"{expert}：合成诊断")


def reply(status="done", proposal_id=None, diagnosis=None, question=None, refs=()) -> str:
    return json.dumps({"status": status, "proposal_id": proposal_id, "diagnosis": diagnosis or "根因甲",
                       "evidence_refs": list(refs), "question": question}, ensure_ascii=False)


class ExpertSource:
    """按专家给确定性提案；可设延迟并统计并发，验证“四条泳道并行”。"""

    source_kind = "deterministic"

    def __init__(self, delay: float = 0.0, diagnosis: dict[str, str] | None = None) -> None:
        self.delay, self.diagnosis = delay, diagnosis or {}
        self.calls: list[str] = []
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def get(self, kind, binding, payload):
        expert = payload["expert"]
        with self._lock:
            self.calls.append(expert)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return {"proposal": draft(expert, binding, self.diagnosis.get(expert)).model_dump(mode="json")}


class Live:
    """live 结果来源替身：按专家回调，回调里自己落提案并返回模型回答的形状。"""

    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[tuple[str, int]] = []
        self.payloads: list[dict] = []

    def get(self, kind, binding, payload):
        self.calls.append((payload["expert"], binding.round))
        self.payloads.append(dict(payload))
        return self.fn(payload["expert"], binding)


class Env:
    def __init__(self, tmp_path, *, router_mode="evaluation", routed=EXPERTS):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.writer = SQLiteWriter(tmp_path / "consult.db")
        MigrationRunner(self.writer).apply()
        self.registry = Registry()
        self.registry.live_fallback = True
        self.router = install_router(self.registry, self.writer, PLANT, run_id="run-1", knowledge_version=3, judge=FakeJudge(),
                                     mode=router_mode, state_version=7, severity=SeverityEstimator(dict(SEVERITY)),
                                     rule_matcher=CoverAll(), policy=policy(), locator=lambda event: tuple(routed))
        self.consultation = install_consultation(self.registry, router=self.router)
        self.sink, self.store = self.router.sink, self.router.store
        self.experts = ExpertSource()
        for domain in EXPERTS:
            self.registry.mode_ports[(AgentResultSource, "deterministic", domain)] = self.experts

    def use_live(self, fn) -> Live:
        source = Live(fn)
        self.registry.mode_ports[(AgentResultSource, "live", "expert")] = source
        return source

    def propose(self, expert: str, binding: RunBinding, diagnosis: str | None = None) -> str:
        return self.sink.save(draft(expert, binding, diagnosis), binding)

    def close(self):
        self.consultation.wait_replies(5.0)  # 后台回答挂接完再关库，避免线程写已关闭的 writer
        self.writer.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    try:
        yield e
    finally:
        e.close()


# ---------------------------------------------------------------- 并行与报告


def test_experts_run_in_parallel_and_each_lands_a_report(tmp_path):
    e = Env(tmp_path)
    try:
        e.experts.delay = 0.12
        routed = e.router.handle_event(event("EV-P", T0))
        assert routed.channel == "fast" and set(routed.experts) == set(EXPERTS)
        assert e.experts.max_active >= 2  # 四条泳道真并行，不是串行等待
        assert {lane.expert for lane in e.consultation.last_lanes} == set(EXPERTS)
        assert all(len(lane.proposal_ids) == 1 and lane.diagnosis for lane in e.consultation.last_lanes)
        proposals = e.sink.list(routed.ticket_id, 1)
        assert {p.expert for p in proposals} == set(EXPERTS)
        assert all(p.timing and p.binding and p.proposal_id.startswith("proposal://") for p in proposals)
        assert e.store.ticket(routed.ticket_id).status == "resolved"
    finally:
        e.close()


# ---------------------------------------------------------------- 回答挂接、过期与分歧


def test_replies_attach_and_conflicts_are_marked(tmp_path):
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        def answer(expert, binding):
            proposal = e.propose(expert, binding)
            return {"status": "finished", "agent_run_id": f"ar-{expert}", "latency_ms": 3, "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis=f"{expert} 的判断")}

        e.use_live(answer)
        ready = []
        e.registry.register_hook(REPLIES_READY_HOOK, lambda ticket_id, round_: ready.append(round_))
        routed = e.router.handle_event(event("EV-C", T0))
        assert e.consultation.wait_replies(5.0)
        stored = {p.expert: p for p in e.sink.list(routed.ticket_id, 1)}
        assert stored["energy"].diagnosis == "energy 的判断" and e.sink.replied(stored["energy"].proposal_id)
        conflicts = json.loads(e.store.ticket_state(routed.ticket_id, "conflicts_round_1"))
        assert {c["diagnosis"] for c in conflicts} == {"energy 的判断", "hvac 的判断"}
        assert ready == [1]
    finally:
        e.close()


def test_unparsable_reply_keeps_proposal(tmp_path):
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        def answer(expert, binding):
            proposal = e.propose(expert, binding)
            return {"status": "finished", "agent_run_id": "ar-1", "proposal_ids": [proposal],
                    "final_answer": "## 结论：冷却塔堵塞，没有 JSON"}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-U", T0))
        assert e.consultation.wait_replies(5.0)
        assert e.store.ticket_state(routed.ticket_id, "reply_parse_failed_1") == "1"
        proposals = e.sink.list(routed.ticket_id, 1)
        assert len(proposals) == 2 and all("合成诊断" in p.diagnosis for p in proposals)
        assert e.store.ticket(routed.ticket_id).status == "resolved"
    finally:
        e.close()


def test_late_reply_is_history_only(tmp_path):
    """第二轮回答填了第一轮的提案编号：不属于本轮运行，不挂接，只记异常。"""
    e = Env(tmp_path, router_mode="live", routed=("energy",))
    try:
        first = {"proposal": None}

        def answer(expert, binding):
            if binding.round == 1:
                first["proposal"] = e.propose(expert, binding, "第一轮结论")
                return {"status": "finished", "agent_run_id": "ar-1", "proposal_ids": [first["proposal"]],
                        "final_answer": reply(proposal_id=first["proposal"], diagnosis="第一轮结论")}
            proposal = e.propose(expert, binding, "第二轮结论")
            return {"status": "finished", "agent_run_id": "ar-2", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=first["proposal"], diagnosis="迟到的第一轮回答")}

        e.use_live(answer)
        first_ticket = e.router.handle_event(event("EV-L1", T0, "lighting_fault")).ticket_id
        assert e.consultation.wait_replies(5.0)
        assert e.store.ticket(first_ticket).status == "resolved"
        second = e.router.handle_event(event("EV-L2", T0 + timedelta(minutes=5), "lighting_fault"))
        assert e.consultation.wait_replies(5.0)
        assert second.result == "straight_through" and e.store.ticket(first_ticket).round == 2
        state = e.store.ticket_state(first_ticket, "reply_foreign_proposal_2_energy")
        assert state and first["proposal"] in state  # 不是本轮运行的提案：只记录不挂接
        stored = e.sink.list(first_ticket, 2)
        assert [p.diagnosis for p in stored] == ["第二轮结论"]  # 迟到的回答没回写
    finally:
        e.close()


# ---------------------------------------------------------------- 先动作、后补依据


def test_proposals_reach_arbitration_before_slow_replies(tmp_path):
    """提案经 propose 工具落库后立即进入裁决；专家的慢回答在裁决之后才挂上，不挡动作。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        ready = threading.Event()
        e.registry.register_hook(PROPOSALS_READY_HOOK, lambda ticket_id, round_: ready.set())

        def answer(expert, binding):
            proposal = e.propose(expert, binding)
            assert ready.wait(5.0), "专家回答返回前，提案应已进入裁决"
            return {"status": "finished", "agent_run_id": f"ar-{expert}", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis=f"{expert} 的判断")}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-R", T0))
        assert e.consultation.wait_replies(5.0)
        assert not any(lane.error for lane in e.consultation.last_lanes)
        stored = {p.expert: p for p in e.sink.list(routed.ticket_id, 1)}
        assert set(stored) == {"energy", "hvac"} and all(e.sink.replied(p.proposal_id) for p in stored.values())
    finally:
        e.close()


def test_dispatch_returns_before_slow_replies(tmp_path):
    """提案裁决后 dispatch 立刻返回；专家回答还没回来时事件单已可推进，回答到了再挂接。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        release = threading.Event()
        adjudicated = threading.Event()
        e.registry.register_hook(PROPOSALS_READY_HOOK, lambda ticket_id, round_: adjudicated.set())

        def answer(expert, binding):
            proposal = e.propose(expert, binding)
            assert release.wait(5.0), "回答未放行前不应返回"
            return {"status": "finished", "agent_run_id": f"ar-{expert}", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis=f"{expert} 的判断")}

        e.use_live(answer)
        holder: dict = {}
        dispatched = threading.Event()

        def run():
            holder["handled"] = e.router.handle_event(event("EV-B", T0))
            dispatched.set()

        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert dispatched.wait(5.0), "dispatch 不应等专家回答"
            ticket_id = holder["handled"].ticket_id
            assert adjudicated.is_set() and e.store.ticket(ticket_id).status == "resolved"  # 已裁决
            assert not any(e.sink.replied(p.proposal_id) for p in e.sink.list(ticket_id, 1))  # 回答还没挂上
        finally:
            release.set()
        worker.join(10)
        assert e.consultation.wait_replies(5.0)
        stored = {p.expert: p for p in e.sink.list(ticket_id, 1)}
        assert all(e.sink.replied(p.proposal_id) for p in stored.values())  # 回答到达后补上
        assert stored["energy"].diagnosis == "energy 的判断"
    finally:
        e.close()


# ---------------------------------------------------------------- 预演命中后的专家运行



def test_live_mode_computes_proposals_and_compares_with_preview(tmp_path):
    """live 模式真实事件一律现场算提案：四位专家都跑一次 live，算完与预演对比并记 preview_check。"""
    e = Env(tmp_path, router_mode="live")
    try:
        preview_state(e, {"soc": 0.2, "transformer_ratio": 0.9})
        e.router.step(local(7, 50))
        assert e.consultation.wait_preview()
        assert len(e.experts.calls) == len(EXPERTS)  # 预演仍照算（确定性后台）
        ran = []

        def answer(expert, binding):
            from types import SimpleNamespace

            ran.append(expert)
            tool = wrap(expert, lambda context, bound, **params: draft(expert, bound), lambda: e.sink,
                        lambda context: (binding, None), lambda context, proposal_id: None)
            receipt = tool(SimpleNamespace(agent_run_id=f"ar-{expert}-{binding.round}"))
            proposal_id = receipt.data["proposal_id"]
            return {"status": "finished", "agent_run_id": f"ar-{expert}", "proposal_ids": [proposal_id],
                    "final_answer": reply(proposal_id=proposal_id, diagnosis=f"{expert} 的判断")}

        e.use_live(answer)
        hit = e.router.handle_event(event("EV-PF", local(8, 5)))
        assert e.consultation.wait_replies(5.0)
        assert set(ran) == set(EXPERTS)  # 真实事件发起 live 专家运行
        check = json.loads(e.store.ticket_state(hit.ticket_id, f"{PREVIEW_CHECK_KEY}_1"))
        assert check["matched"] and check["same_actions"] and check["preview_ticket"]
        assert "（与预演一致）" in e.store.ticket(hit.ticket_id).summary
        proposals = e.sink.list(hit.ticket_id, 1)
        assert len(proposals) == len(EXPERTS)  # 只有 live 提案，没有取用缓存
        assert all(e.sink.replied(p.proposal_id) for p in proposals)
        conflicts = json.loads(e.store.ticket_state(hit.ticket_id, "conflicts_round_1"))
        assert {c["diagnosis"] for c in conflicts} == {f"{expert} 的判断" for expert in EXPERTS}
    finally:
        e.close()


def test_failed_run_after_proposal_does_not_duplicate_fallback(tmp_path):
    """专家已通过 propose 落库后运行失败：不再生成确定性兜底，同轮同专家只有一份提案。"""
    e = Env(tmp_path, router_mode="live", routed=("energy",))
    try:
        def answer(expert, binding):
            e.propose(expert, binding, "已落库的提案")
            raise TimeoutError("回答超时")

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-F", T0, "lighting_fault"))
        assert e.consultation.wait_replies(5.0)
        proposals = e.sink.list(routed.ticket_id, 1)
        assert len(proposals) == 1 and proposals[0].diagnosis == "已落库的提案"  # 没有补第二份
        assert e.store.ticket(routed.ticket_id).status == "blocked"
    finally:
        e.close()


def test_live_finished_without_proposal_falls_back_and_flags_gap(tmp_path):
    """live 专家 finished 但没交提案：按确定性兜底补一份并标覆盖缺口，不静默当正常。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        def answer(expert, binding):
            if expert == "energy":
                return {"status": "finished", "agent_run_id": "ar-e", "proposal_ids": [], "final_answer": ""}
            proposal = e.propose(expert, binding)
            return {"status": "finished", "agent_run_id": "ar-h", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis="暖通判断")}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-NP", T0))
        assert e.consultation.wait_replies(5.0)
        proposals = {p.expert: p for p in e.sink.list(routed.ticket_id, 1)}
        assert set(proposals) == {"energy", "hvac"}  # energy 由确定性兜底补上
        assert "合成诊断" in proposals["energy"].diagnosis
        record = next(r for r in e.router.runs if r.kind == AgentResultKind.expert and r.agent_name == "energy")
        assert record.status == "finished" and record.coverage_gap
        assert e.store.ticket_state(routed.ticket_id, "proposal_missing_1_energy") == "1"
        assert e.store.ticket(routed.ticket_id).status == "resolved"
    finally:
        e.close()


def test_live_finished_without_proposal_and_no_fallback_marks_blocked(tmp_path):
    """没交提案且确定性兜底也没有时，事件单标受阻，不能当正常结束。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        del e.registry.mode_ports[(AgentResultSource, "deterministic", "energy")]  # 该专家没有兜底来源

        def answer(expert, binding):
            if expert == "energy":
                return {"status": "finished", "agent_run_id": "ar-e", "proposal_ids": [], "final_answer": ""}
            proposal = e.propose(expert, binding)
            return {"status": "finished", "agent_run_id": "ar-h", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis="暖通判断")}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-NB", T0))
        assert e.consultation.wait_replies(5.0)
        assert e.store.ticket(routed.ticket_id).status == "blocked"
        record = next(r for r in e.router.runs if r.kind == AgentResultKind.expert and r.agent_name == "energy")
        assert record.status == "blocked" and record.coverage_gap
    finally:
        e.close()


def test_live_mode_partial_preview_still_runs_live_experts(tmp_path):
    """预演只覆盖部分专家时，live 模式真实事件两位都现场跑；对比只记偏差，不补算、不取用。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        preview_state(e, {"soc": 0.2})
        e.registry.register_hook(PREVIEW_SIGNALS_HOOK, lambda router, now: [
            PreviewSignal(kind="plan_start", at=local(9, 45), event_type="load_spike", experts=("energy",),
                          summary="生产计划大负荷启动")])
        e.router.step(local(9, 30))
        assert e.consultation.wait_preview()
        assert len(e.experts.calls) == 1  # 预演只算了 energy
        ran = []

        def answer(expert, binding):
            ran.append(expert)
            proposal_id = e.propose(expert, binding, f"{expert} 的现场判断")
            return {"status": "finished", "agent_run_id": f"ar-{expert}", "proposal_ids": [proposal_id],
                    "final_answer": reply(proposal_id=proposal_id, diagnosis=f"{expert} 的判断")}

        e.use_live(answer)
        hit = e.router.handle_event(event("EV-PPH", local(9, 45))).ticket_id
        assert e.consultation.wait_replies(5.0)
        assert set(ran) == {"energy", "hvac"}  # 预演覆盖了 energy，也要现场跑
        check = json.loads(e.store.ticket_state(hit, f"{PREVIEW_CHECK_KEY}_1"))
        assert check["matched"] and not check["same_actions"]
        assert any("hvac" in item for item in check["diff"])  # 现场派发的专家比预演多，算偏差
        proposals = {p.expert: p for p in e.sink.list(hit, 1)}
        assert set(proposals) == {"energy", "hvac"} and proposals["energy"].diagnosis == "energy 的判断"
    finally:
        e.close()


# ---------------------------------------------------------------- live 失败兜底


def test_live_all_experts_fail_falls_back_and_blocks(tmp_path):
    """live 模式预演命中也不取用；全部 live 失败时按确定性兜底补提案，事件单标受阻。"""
    e = Env(tmp_path, router_mode="live")
    try:
        preview_state(e, {"soc": 0.2, "transformer_ratio": 0.9})
        e.router.step(local(7, 50))
        assert e.consultation.wait_preview()

        def answer(expert, binding):
            raise TimeoutError("模型连接中断")

        e.use_live(answer)
        hit = e.router.handle_event(event("EV-PF", local(8, 5)))
        assert e.consultation.wait_replies(5.0)
        assert e.store.ticket(hit.ticket_id).status == "blocked"
        assert len(e.sink.list(hit.ticket_id, 1)) == len(EXPERTS)  # 每位专家都有确定性兜底
        records = [r for r in e.router.runs if r.kind == AgentResultKind.expert and r.binding is not None
                   and r.binding.ticket_id == hit.ticket_id]
        assert len(records) == len(EXPERTS) and all(r.status == "failed" and r.coverage_gap for r in records)
    finally:
        e.close()


def test_live_partial_preview_then_failure_still_blocks(tmp_path):
    """预演只覆盖部分专家、live 模式不取用：失败专家走兜底，其余照常，事件单标受阻。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        preview_state(e, {"soc": 0.2})
        e.registry.register_hook(PREVIEW_SIGNALS_HOOK, lambda router, now: [
            PreviewSignal(kind="plan_start", at=local(9, 45), event_type="load_spike", experts=("energy",),
                          summary="生产计划大负荷启动")])
        e.router.step(local(9, 30))
        assert e.consultation.wait_preview()

        def answer(expert, binding):
            if expert == "energy":
                raise TimeoutError("模型连接中断")
            proposal = e.propose(expert, binding, "暖通判断")
            return {"status": "finished", "agent_run_id": "ar-h", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis="暖通判断")}

        e.use_live(answer)
        hit = e.router.handle_event(event("EV-PPH", local(9, 45))).ticket_id
        assert e.consultation.wait_replies(5.0)
        assert e.store.ticket(hit).status == "blocked"
        proposals = {p.expert: p for p in e.sink.list(hit, 1)}
        assert set(proposals) == {"energy", "hvac"} and "合成诊断" in proposals["energy"].diagnosis
    finally:
        e.close()


# ---------------------------------------------------------------- 迟到回答


def test_round_one_reply_after_round_two_is_history_only(tmp_path):
    """真正的延迟返回：第一轮回答在第二轮开始后才到，只入历史，不回写第一轮提案。"""
    e = Env(tmp_path, router_mode="live", routed=("energy",))
    try:
        started, proposal_ok, answer_ok, second_done = (threading.Event() for _ in range(4))

        def answer(expert, binding):
            if binding.round == 1:
                started.set()
                assert proposal_ok.wait(5.0)
                proposal_id = e.propose(expert, binding)
                assert answer_ok.wait(5.0)  # 答完之前第二轮已经开始
                return {"status": "finished", "agent_run_id": "ar-1", "proposal_ids": [proposal_id],
                        "final_answer": reply(proposal_id=proposal_id, diagnosis="第一轮的迟到回答")}
            proposal_id = e.propose(expert, binding)
            second_done.set()
            return {"status": "finished", "agent_run_id": "ar-2", "proposal_ids": [proposal_id],
                    "final_answer": reply(proposal_id=proposal_id, diagnosis="第二轮回答")}

        e.use_live(answer)
        holder = {}
        thread = threading.Thread(target=lambda: holder.setdefault(
            "handled", e.router.handle_event(event("EV-L1", T0, "lighting_fault"))))
        thread.start()
        assert started.wait(5.0)
        # 第一轮还在跑：同一事件流的事件并入，等本轮裁决完开第二轮
        assert e.router.handle_event(event("EV-L2", T0 + timedelta(minutes=5), "lighting_fault")).result == "straight_through"
        proposal_ok.set()
        assert second_done.wait(5.0)  # 第二轮已经在第一轮回答前开始
        answer_ok.set()
        thread.join(10)
        assert e.consultation.wait_replies(5.0)
        ticket_id = holder["handled"].ticket_id
        assert e.store.ticket(ticket_id).round == 2
        reason = e.store.ticket_state(ticket_id, "late_reply_1")
        assert reason and "第 2 轮" in reason
        first = e.sink.list(ticket_id, 1)
        second = e.sink.list(ticket_id, 2)
        assert len(first) == 1 and not e.sink.replied(first[0].proposal_id)  # 迟到的回答没回写
        assert len(second) == 1 and e.sink.replied(second[0].proposal_id)
    finally:
        e.close()


def test_foreign_proposal_id_is_not_attached(tmp_path):
    """专家回答填了同轮另一位专家的提案编号：不挂接、不改那位专家的诊断，记 reply_foreign_proposal。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        ids: dict[str, str] = {}
        proposed = threading.Event()

        def answer(expert, binding):
            if expert == "hvac":
                ids["hvac"] = e.propose(expert, binding, "暖通判断")
                proposed.set()
                return {"status": "finished", "agent_run_id": "ar-h", "proposal_ids": [ids["hvac"]],
                        "final_answer": reply(proposal_id=ids["hvac"], diagnosis="暖通判断")}
            assert proposed.wait(5.0)  # 等暖通的提案先落库，再拿它的编号冒充自己的
            e.propose(expert, binding, "能源判断")
            return {"status": "finished", "agent_run_id": "ar-e", "proposal_ids": [],
                    "final_answer": reply(proposal_id=ids["hvac"], diagnosis="能源替暖通下结论")}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-FP", T0))
        assert e.consultation.wait_replies(5.0)
        proposals = {p.expert: p for p in e.sink.list(routed.ticket_id, 1)}
        assert proposals["hvac"].diagnosis == "暖通判断"  # 暖通的诊断没被能源回答改写
        state = e.store.ticket_state(routed.ticket_id, "reply_foreign_proposal_1_energy")
        assert state and ids["hvac"] in state
        assert not e.sink.replied(proposals["energy"].proposal_id)
    finally:
        e.close()


# ---------------------------------------------------------------- 预演


def preview_state(e, values):
    e.registry.register_hook(PREVIEW_STATE_HOOK, lambda router, at: dict(values))


def test_preview_never_reuses_cache_and_records_check(tmp_path):
    """预演只预判：事件到达仍现场整轮算提案，算完写 preview_check（匹配、动作一致、指回预演单）。"""
    e = Env(tmp_path)
    try:
        preview_state(e, {"soc": 0.2, "transformer_ratio": 0.9})
        e.router.step(local(7, 50))
        assert e.consultation.wait_preview()
        calls_after_preview = len(e.experts.calls)
        assert calls_after_preview == len(EXPERTS)  # 预演只在后台算一次

        hit = e.router.handle_event(event("EV-H", local(8, 5)))
        assert len(e.experts.calls) == calls_after_preview * 2  # 事件到达现场整轮重算，不取用缓存
        check = json.loads(e.store.ticket_state(hit.ticket_id, f"{PREVIEW_CHECK_KEY}_1"))
        assert check["matched"] and check["same_actions"] and check["diff"] == []
        rehearsal = e.store.ticket(check["preview_ticket"])
        assert rehearsal is not None and rehearsal.rehearsal  # 预演命中开了预演单
        assert e.consultation.last_preview is not None and e.consultation.last_preview.same_actions
        assert "（与预演一致）" in e.store.ticket(hit.ticket_id).summary
        assert {p.expert for p in e.sink.list(hit.ticket_id, 1)} == set(EXPERTS)
        assert not any(e.store.ticket_state(hit.ticket_id, key)
                       for key in ("preview_hit", "preview_miss_1", "preview_partial_1"))
    finally:
        e.close()


class DifferingExpertSource:
    """预演（08:00）算 discharge、真实事件（08:05）算 shed：用来验证对比能报出动作偏差。"""

    def __init__(self, preview_at: datetime) -> None:
        self.preview_at = preview_at

    def get(self, kind, binding, payload):
        expert = payload["expert"]
        command = "discharge" if binding.trigger_at == self.preview_at else "shed"
        entity = f"entity://{PLANT}/battery/BAT1" if command == "discharge" else f"entity://{PLANT}/lighting/LT-P"
        return {"proposal": ExpertProposal(
            expert=expert, ticket_id=binding.ticket_id, cost=ProposalCost(cny=-1.0), confidence=0.8,
            actions=(action(entity, command, binding.trigger_at),), diagnosis=f"{expert}：{command}").model_dump(mode="json")}


def test_preview_check_reports_action_difference(tmp_path):
    """现场动作与预演不同：preview_check 记 same_actions=false 与偏差说明，摘要带“预演偏差”。"""
    e = Env(tmp_path)
    try:
        preview_state(e, {"soc": 0.2})
        e.registry.mode_ports[(AgentResultSource, "deterministic", "energy")] = DifferingExpertSource(local(8, 0))
        e.router.step(local(7, 50))
        assert e.consultation.wait_preview()
        hit = e.router.handle_event(event("EV-D", local(8, 5)))
        check = json.loads(e.store.ticket_state(hit.ticket_id, f"{PREVIEW_CHECK_KEY}_1"))
        assert check["matched"] and not check["same_actions"]
        assert check["diff"] and "energy" in check["diff"][0]
        assert "预演偏差" in e.store.ticket(hit.ticket_id).summary
    finally:
        e.close()


def test_preview_check_reports_no_match(tmp_path):
    """事件与预演的类型、时间窗不匹配：preview_check 记 matched=false，摘要不加对比结论。"""
    e = Env(tmp_path, routed=("energy",))
    try:
        preview_state(e, {"soc": 0.2})
        e.router.step(local(7, 50))
        assert e.consultation.wait_preview()
        hit = e.router.handle_event(event("EV-NM", local(9, 30), "lighting_fault"))
        check = json.loads(e.store.ticket_state(hit.ticket_id, f"{PREVIEW_CHECK_KEY}_1"))
        assert check["matched"] is False and check["diff"] == [] and check["preview_ticket"] is None
        assert "预演" not in e.store.ticket(hit.ticket_id).summary
    finally:
        e.close()


# ---------------------------------------------------------------- 单专家异常隔离


def test_one_expert_exception_does_not_skip_other_proposals(tmp_path):
    """A 在 _one 校验提案时抛异常、B 延迟落库：B 的提案仍要进入本轮裁决，A 走确定性兜底。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        seen: list[set[str]] = []
        e.registry.register_hook(PROPOSALS_READY_HOOK,
                                 lambda ticket_id, round_: seen.append({p.expert for p in e.sink.list(ticket_id, round_)}))

        def answer(expert, binding):
            if expert == "energy":  # 结果不是合法提案：_one 校验时抛异常
                return {"status": "finished", "agent_run_id": "ar-bad", "proposal_ids": [], "proposal": "坏提案"}
            time.sleep(0.3)  # 另一位的提案晚到
            proposal = e.propose(expert, binding)
            return {"status": "finished", "agent_run_id": "ar-h", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis="暖通判断")}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-EX", T0))
        assert e.consultation.wait_replies(5.0)
        assert seen and {"energy", "hvac"} <= seen[0]  # 裁决时两位的提案都已落库
        proposals = {p.expert: p for p in e.sink.list(routed.ticket_id, 1)}
        assert set(proposals) == {"energy", "hvac"}  # 异常专家由确定性兜底补上
        record = next(r for r in e.router.runs if r.kind == AgentResultKind.expert and r.agent_name == "energy")
        assert record.status == "failed" and record.coverage_gap
        assert e.store.ticket(routed.ticket_id).status == "blocked"
    finally:
        e.close()


# ---------------------------------------------------------------- 受阻


def test_one_expert_timeout_blocks_ticket_others_still_report(tmp_path):
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        def answer(expert, binding):
            if expert == "energy":
                raise TimeoutError("模型连接中断")
            proposal = e.propose(expert, binding, "设备正常")
            return {"status": "finished", "agent_run_id": "ar-ok", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis="设备正常")}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-T", T0))
        assert e.consultation.wait_replies(5.0)
        assert e.store.ticket(routed.ticket_id).status == "blocked"
        proposals = {p.expert: p for p in e.sink.list(routed.ticket_id, 1)}
        assert set(proposals) == {"hvac", "energy"}  # 失败的专家用确定性提案兜底，其他专家照常
        assert "合成诊断" in proposals["energy"].diagnosis
        record = next(r for r in e.router.runs if r.kind == AgentResultKind.expert and r.agent_name == "energy")
        assert record.status == "failed" and record.coverage_gap
        assert any(lane.expert == "energy" and lane.error for lane in e.consultation.last_lanes)
    finally:
        e.close()


def test_bad_result_processing_falls_back_and_keeps_other_experts(tmp_path):
    """一位专家的结果处理抛异常：该专家走确定性兜底并标覆盖缺口，其余专家提案照常。"""
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        def answer(expert, binding):
            if expert == "energy":
                return {"status": "finished", "agent_run_id": "ar-e", "proposal": {"expert": "energy"}}  # 缺必填字段
            proposal = e.propose(expert, binding, "暖通判断")
            return {"status": "finished", "agent_run_id": "ar-h", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis="暖通判断")}

        e.use_live(answer)
        routed = e.router.handle_event(event("EV-EX", T0))
        assert e.consultation.wait_replies(5.0)
        proposals = {p.expert: p for p in e.sink.list(routed.ticket_id, 1)}
        assert set(proposals) == {"energy", "hvac"} and "合成诊断" in proposals["energy"].diagnosis
        record = next(r for r in e.router.runs if r.kind == AgentResultKind.expert and r.agent_name == "energy")
        assert record.status == "failed" and record.coverage_gap
        assert e.store.ticket(routed.ticket_id).status == "blocked"
    finally:
        e.close()


# ---------------------------------------------------------------- live 会诊


def test_live_source_converts_string_as_of_to_datetime():
    """router.flush 传进来的 as_of 是 ISO 字符串；网关按 datetime 比对文档时间，不能收到字符串。"""
    from types import SimpleNamespace

    from plantkg.integrations.nexent import LiveAgentResultSource

    class FakeGateway:
        main_agent = "plantkg_main"

        def __init__(self) -> None:
            self.as_of: list[object] = []

        def run_expert(self, ticket_id, expert, *, binding, actor, brief=None, as_of=None, timing_base=None):
            self.as_of.append(as_of)
            return {"status": "finished", "agent_run_id": "ar-x", "proposal_ids": [], "final_answer": ""}

        def prepare(self, actor, query, agent_name, as_of=None, **kwargs):
            self.as_of.append(as_of)
            run = SimpleNamespace()
            run.record = SimpleNamespace(status="finished", agent_run_id="ar-1", terminal_reason=None,
                                         final_answer="ok", timing=SimpleNamespace(latency_ms=1))
            return run

        def stream(self, run):
            return iter(())

    gateway = FakeGateway()
    source = LiveAgentResultSource(gateway, SimpleNamespace(nexent=SimpleNamespace(run_retries=0)))
    binding = RunBinding(run_id="run-1", knowledge_version=3, ticket_id="ticket://TK-1", round=1, state_version=7,
                         trigger_at=T0)
    source.get(AgentResultKind.triage, binding, {"query": "q", "as_of": "2026-07-01T02:00:00Z"})
    source.get(AgentResultKind.expert, binding, {"expert": "energy", "as_of": "2026-07-01T02:00:00Z"})
    assert gateway.as_of == [T0, T0]
    assert all(isinstance(value, datetime) for value in gateway.as_of)


def test_live_payload_carries_effective_at_from_response_budget(tmp_path):
    """B29 给 live 专家的 payload 带 effective_at = trigger_at + 响应预算，提案时序符合本轮预算。"""
    e = Env(tmp_path, router_mode="live", routed=("energy",))
    try:
        def answer(expert, binding):
            proposal = e.propose(expert, binding)
            return {"status": "finished", "agent_run_id": "ar-e", "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal, diagnosis="能源判断")}

        source = e.use_live(answer)
        e.router.handle_event(event("EV-EA", T0))
        assert e.consultation.wait_replies(5.0)
        expected = (T0 + timedelta(minutes=e.router.policy.response_budget.layer12_min)).isoformat()
        assert source.payloads and all(p["effective_at"] == expected for p in source.payloads)
    finally:
        e.close()


def test_live_experts_get_current_event_and_mark_gap_without_state(tmp_path):
    """live 专家经会诊登记的钩子拿到本轮事件；主路由没有运行状态时记覆盖缺口。"""
    from types import SimpleNamespace

    class Runner:
        """只给 _payload 用的运行简报来源；没有 dispatcher，提案就绪占位不会执行动作。"""

        def __init__(self) -> None:
            self.state = SimpleNamespace(state_version=0)
            self.ids = {"air": "entity://p1/air_system/AC-2"}
            self.knowledge = {"k1"}
            self.with_state = True

        def brief(self) -> dict:
            return {"soc": 0.2, "situation": {"admin_temp": 24.0}, "at": T0.isoformat()} if self.with_state else {}

    e = Env(tmp_path, router_mode="live")
    try:
        seen: dict[str, dict] = {}

        def propose_for(domain: str):
            def propose(context, binding, focus=None):
                # 与各专家 propose 业务函数一致：从会诊登记的同名钩子取本轮事件与状态
                if domain == "elevator":
                    event, _ = e.registry.hooks_for("expert_context:elevator")[-1](context, binding, focus)
                elif domain == "hvac":
                    found = e.registry.hooks_for("hvac_situation")[-1](context, binding)
                    event = found[0] if found else {}
                elif domain == "equipment":
                    event = e.registry.hooks_for("equipment.readings")[-1](binding).get("event") or {}
                else:
                    event, _ = e.registry.hooks_for(f"energy_state:{binding.run_id}")[-1](binding)
                seen[domain] = event
                return draft(domain, binding, f"{domain}：{event.get('event_id')}")
            return propose

        for domain in EXPERTS:
            e.registry.register_hook(f"expert_propose:{domain}", propose_for(domain))

        def answer(expert, binding):
            proposal = e.registry.hooks_for(f"expert_propose:{expert}")[-1](None, binding)
            proposal_id = e.sink.save(proposal, binding)
            return {"status": "finished", "agent_run_id": f"ar-{expert}", "proposal_ids": [proposal_id],
                    "final_answer": reply(proposal_id=proposal_id, diagnosis=f"{expert} 判断")}

        e.use_live(answer)
        runner = Runner()
        e.consultation.bind(e.router, runner)
        first = e.router.handle_event(event("EV-CTX", T0))
        assert e.consultation.wait_replies(5.0)
        assert all(seen.get(domain, {}).get("event_id") == "EV-CTX" for domain in EXPERTS)
        first_runs = [r for r in e.router.runs if r.kind == AgentResultKind.expert and r.binding.ticket_id == first.ticket_id]
        assert len(first_runs) == len(EXPERTS) and not any(r.coverage_gap for r in first_runs)
        assert len(e.sink.list(first.ticket_id, 1)) == len(EXPERTS)

        runner.with_state = False  # 主路由 live：payload 只有事件、没有运行状态
        second = e.router.handle_event(event("EV-CTX2", T0 + timedelta(minutes=5), "elevator_congestion"))
        assert e.consultation.wait_replies(5.0)
        second_runs = [r for r in e.router.runs if r.kind == AgentResultKind.expert and r.binding.ticket_id == second.ticket_id]
        assert len(second_runs) == len(EXPERTS) and all(r.coverage_gap for r in second_runs)
    finally:
        e.close()


def test_main_router_live_payload_uses_preview_state_provider(tmp_path):
    e = Env(tmp_path, router_mode="live")
    try:
        e.registry.register_hook(PREVIEW_STATE_HOOK, lambda router, at: {"soc": 0.64, "transformer_ratio": 0.71})
        binding = RunBinding(run_id=e.router.run_id, knowledge_version=3, ticket_id="ticket://TK-STATE", round=1,
                             state_version=7, trigger_at=T0)

        payload = e.consultation._payload(e.router, None, event("EV-STATE", T0), binding)

        assert payload["state"] == {"soc": 0.64, "transformer_ratio": 0.71}
    finally:
        e.close()


# ---------------------------------------------------------------- 场景结束解绑（#72）


def test_pack_assembly_registers_scenario_end_hook():
    """领域包装配把场景结束钩子登记进场景注册表，B29 的解绑才会被 B17 调用。"""
    from plantkg.agent.consultation import register as register_consultation
    from plantkg.world.scenario.runner import SCENARIO_END_HOOK, SCENARIO_HOOK

    registry = Registry()
    register_consultation(registry, None)
    scenario_registry = Registry()
    for plugin in registry.hooks_for(SCENARIO_HOOK):
        plugin(scenario_registry)
    assert scenario_registry.hooks_for(SCENARIO_END_HOOK)


def test_scenario_end_unbinds_router_previews_and_energy_hook(tmp_path):
    """连跑两次场景到结束：绑定的 router 与预演缓存不累积，energy_state:<run_id> 钩子回到 0。"""
    from plantkg.agent.router.router import SCENARIO_BIND_HOOK
    from plantkg.agent.router.scenario import CoordinatedDispatcher
    from plantkg.world.scenario.__main__ import build_world, open_db
    from plantkg.world.scenario.runner import DISPATCHER_HOOK, SCENARIO_END_HOOK, ScenarioRunner
    from plantkg.world.scenario.script import Scenario

    e = Env(tmp_path)
    try:
        scenario_registry = Registry()
        scenario_registry.register_hook(DISPATCHER_HOOK, CoordinatedDispatcher)
        scenario_registry.register_hook(SCENARIO_BIND_HOOK, e.consultation.bind)
        scenario_registry.register_hook(SCENARIO_END_HOOK, e.consultation.on_scenario_end)
        base = {"scenario_id": "end-test", "title": "解绑测试", "date": "2026-07-01", "seed": 7, "initial": {"soc": 0.5},
                "events": [{"at": "09:00", "type": "lighting_fault", "entity_id": "LT-A03", "summary": "照明故障"}]}
        energy_hooks = lambda: sorted(name for name in e.registry.hooks if name.startswith("energy_state:"))
        for index in range(2):
            sc = Scenario.model_validate(base | {"scenario_id": f"end-test-{index}"})
            world = build_world(sc)
            db = open_db(tmp_path / f"scenario-{index}.db", world.site.plant_id)
            try:
                runner = ScenarioRunner(world, db, registry=scenario_registry, mode="evaluation", group="coordinated")
                runner.start()
                e.consultation._register_energy_state(runner.run_info.run_id)
                assert energy_hooks() == [f"energy_state:{runner.run_info.run_id}"]
                runner.run_until()
                assert runner.run_info.status == "finished"
                assert not [bound for _, bound in e.consultation._bindings if bound is runner]
                assert all(getattr(router, "run_id", None) != runner.run_info.run_id for router, _ in e.consultation._bindings)
                assert id(runner.dispatcher.router) not in e.consultation._previews
                assert energy_hooks() == []
            finally:
                db.close()
        assert len(e.consultation._bindings) == 1  # 只剩装配时绑定的主路由
        assert len(e.consultation._previews) == 1
    finally:
        e.close()


def test_compare_actions_reports_param_difference() -> None:
    """同一储能同一命令但放电功率不同，不能报“与预演一致”（PR #50 复审 P2）。"""
    from plantkg.agent.consultation.preview import compare_actions

    def proposal(kw: float) -> ExpertProposal:
        act = ProposedAction(entity_id=AIR, command="discharge", params={"power_kw": kw},
                             interval=Interval(start=T0, end=T0 + timedelta(minutes=15)))
        return ExpertProposal(expert="energy", ticket_id="t1", actions=(act,), cost=ProposalCost(cny=0), confidence=0.8)

    assert compare_actions([proposal(400)], [proposal(400)]) == (True, [])
    same, diff = compare_actions([proposal(400)], [proposal(200)])
    assert not same and "power_kw 400→200" in diff[0]


def test_live_round_does_not_hang_when_experts_exceed_parallel_cap(tmp_path, monkeypatch):
    """专家数超过并发上限时 live 会诊不能卡死：提案后等裁决的专家不能占住排队专家的线程（PR #50 复审）。"""
    from plantkg.agent.consultation import consilium

    monkeypatch.setattr(consilium, "MAX_PARALLEL", 1)
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        lock, running, peak = threading.Lock(), [0], [0]

        def answer(expert, binding):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.1)
            proposal = e.propose(expert, binding)
            with lock:
                running[0] -= 1
            return {"status": "finished", "agent_run_id": f"ar-{expert}", "latency_ms": 3, "proposal_ids": [proposal],
                    "final_answer": reply(proposal_id=proposal)}

        live = e.use_live(answer)
        done: list = []
        worker = threading.Thread(target=lambda: done.append(e.router.handle_event(event("EV-CAP", T0))), daemon=True)
        worker.start()
        worker.join(5.0)
        assert not worker.is_alive(), "专家数超过并发上限时会诊卡死"
        assert {expert for expert, _ in live.calls} == {"energy", "hvac"}
        assert peak[0] == 1, "并发上限 1 时两位专家同时在跑"
    finally:
        e.close()


def test_live_round_later_expert_in_slot_then_fallback_does_not_hang(tmp_path, monkeypatch):
    """后位专家先拿名额并在失败兜底时落库：不能持名额等前位、前位也不该卡在名额上（终审必修）。"""
    from plantkg.agent.consultation import consilium

    monkeypatch.setattr(consilium, "MAX_PARALLEL", 1)

    from concurrent.futures import Future

    class DaemonPool:
        """ThreadPoolExecutor 替身：线程 daemon，回归时不会因残留等待线程卡住 pytest 退出。"""

        def __init__(self, max_workers=None, thread_name_prefix=""):
            self._threads: list[threading.Thread] = []

        def submit(self, fn, *args, **kwargs):
            future = Future()

            def run():
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 — 交回 future，由会诊按失败处理
                    future.set_exception(exc)

            thread = threading.Thread(target=run, daemon=True)
            self._threads.append(thread)
            thread.start()
            return future

        def shutdown(self, wait=True):
            pass

    class LateTokenSemaphore:
        """名额替身：第一个取名额的前位专家等到后位专家释放才返回，后位专家先拿名额去运行。"""

        def __init__(self, value=1):
            self._lock = threading.Lock()
            self._calls = 0
            self._released = threading.Event()

        def acquire(self, blocking=True, timeout=None):
            with self._lock:
                self._calls += 1
                first = self._calls == 1
            if first:
                assert self._released.wait(10), "前位专家一直没拿到名额，会诊卡死"
            return True

        def release(self):
            self._released.set()

    # 只替换会诊模块看到的名额工厂，别动全局 threading.Semaphore（ThreadPoolExecutor 内部也用它）
    shim = types.SimpleNamespace(**{name: getattr(threading, name) for name in dir(threading) if not name.startswith("_")})
    shim.Semaphore = LateTokenSemaphore
    monkeypatch.setattr(consilium, "threading", shim)
    monkeypatch.setattr(consilium, "ThreadPoolExecutor", DaemonPool)
    e = Env(tmp_path, router_mode="live", routed=("energy", "hvac"))
    try:
        live = e.use_live(lambda expert, binding: {"status": "failed", "agent_run_id": f"ar-{expert}"})
        done: list = []
        worker = threading.Thread(target=lambda: done.append(e.router.handle_event(event("EV-LATE", T0))), daemon=True)
        worker.start()
        worker.join(10.0)
        assert not worker.is_alive(), "后位专家持名额等前位落库时 live 会诊卡死"
        assert done and {expert for expert, _ in live.calls} == {"energy", "hvac"}
        assert {p.expert for p in e.sink.list(done[0].ticket_id, 1)} == {"energy", "hvac"}  # 两位都走兜底落库
    finally:
        e.close()


def test_compare_actions_reports_duration_and_keeps_repeated_actions() -> None:
    """同功率不同时长要报偏差；同一对象的同名动作有多个时不能互相覆盖（PR #50 复审）。"""
    from plantkg.agent.consultation.preview import compare_actions

    def proposal(*acts: tuple[int, float]) -> ExpertProposal:
        return ExpertProposal(expert="energy", ticket_id="t1", cost=ProposalCost(cny=0), confidence=0.8,
                              actions=tuple(action(AIR, "discharge", T0, minutes, power_kw=kw) for minutes, kw in acts))

    same, diff = compare_actions([proposal((15, 400))], [proposal((60, 400))])
    assert not same and "时长 15→60 分钟" in diff[0]
    same, diff = compare_actions([proposal((15, 400), (15, 200))], [proposal((15, 400), (15, 300))])
    assert not same and "power_kw 200→300" in diff[0]
    assert compare_actions([proposal((15, 400), (15, 200))], [proposal((15, 200), (15, 400))]) == (True, [])


def test_offline_expert_fallback_is_recorded_as_deterministic_run_mode(tmp_path):
    """#243：evaluation 下专家实际取到确定性实现时，记 run_mode=deterministic、source=deterministic、覆盖不足。"""
    e = Env(tmp_path)
    try:
        e.router.handle_event(event("EV-D", T0))
        records = [r for r in e.router.runs if r.kind == AgentResultKind.expert]
        assert records and all((r.run_mode, r.source, r.coverage_gap) == ("deterministic", "deterministic", True) for r in records)
    finally:
        e.close()


def test_cached_expert_replay_keeps_evaluation_run_mode(tmp_path):
    """来源声明为 cache（真实采集的重放）时仍记 evaluation：不是确定性回退。"""
    e = Env(tmp_path)
    try:
        class Replay(ExpertSource):
            def get(self, kind, binding, payload):
                return super().get(kind, binding, payload) | {"source": "cache", "coverage_gap": False}
        e.registry.mode_ports[(AgentResultSource, "evaluation", "expert")] = Replay()
        e.router.handle_event(event("EV-C", T0))
        records = [r for r in e.router.runs if r.kind == AgentResultKind.expert]
        assert records and all((r.run_mode, r.source) == ("evaluation", "cache") for r in records)
    finally:
        e.close()


def test_evaluation_source_exception_falls_back_and_is_recorded_as_deterministic(tmp_path):
    """评测来源抛异常、落确定性兜底提案：记 deterministic/deterministic/覆盖不足，不再记 evaluation/cache。"""
    e = Env(tmp_path)
    try:
        class Boom:
            def get(self, kind, binding, payload):
                raise RuntimeError("cache backend down")
        e.registry.mode_ports[(AgentResultSource, "evaluation", "expert")] = Boom()
        e.router.handle_event(event("EV-X", T0))
        records = [r for r in e.router.runs if r.kind == AgentResultKind.expert]
        assert records and all((r.run_mode, r.source, r.coverage_gap) == ("deterministic", "deterministic", True) for r in records)
        assert all(r.proposal_ids for r in records)  # 兜底提案真的落了库
    finally:
        e.close()


def test_registered_evaluation_source_declaring_deterministic_gap_is_deterministic(tmp_path):
    """已登记的 evaluation 来源（如缓存缺失回退）返回 source=deterministic、coverage_gap=True。"""
    e = Env(tmp_path)
    try:
        class Missed(ExpertSource):
            def get(self, kind, binding, payload):
                return super().get(kind, binding, payload) | {"source": "deterministic", "coverage_gap": True}
        e.registry.mode_ports[(AgentResultSource, "evaluation", "expert")] = Missed()
        e.router.handle_event(event("EV-M", T0))
        records = [r for r in e.router.runs if r.kind == AgentResultKind.expert]
        assert records and all((r.run_mode, r.source, r.coverage_gap) == ("deterministic", "deterministic", True) for r in records)
    finally:
        e.close()
