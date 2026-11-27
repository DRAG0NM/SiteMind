"""B27 值守层冒烟测试：事件洪峰、事件流订阅、直通、轮次、分叉与值班指令。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from plantkg.agent.router import (
    EXPERTS_HOOK, INSTRUCTION_HOOK, PROPOSALS_READY_HOOK, REPLIES_READY_HOOK, RouterConfig, RouterStore,
)
from plantkg.core.contracts import ExpertReply
from plantkg.core.ports import AgentResultSource
from tests.agent.router.helpers import (
    AIR, LIGHT, T0, TRANSFORMER, CountingTriage, binding, make_event, make_proposal, run_info,
)


def open_ticket(env, event):
    handled = env.router.handle_event(event)
    assert handled.result == "opened" and handled.ticket_id
    return handled


def test_event_flood_drops_noise_and_opens_real_tickets(env):
    """事件洪峰：噪声与低于阈值的事件被过滤，真实异常开出事件单。"""
    for index in range(12):
        handled = env.router.handle_event(make_event(f"EV-N{index}", index, "sensor_noise", None, severity=0))
        assert handled.result == "dropped_severity"
    assert env.router.handle_event(make_event("EV-LOW", 13, "load_spike", None, severity=1)).result == "dropped_severity"
    assert env.router.handle_event(make_event("EV-AGAIN", 15, "sensor_noise", None, severity=0)).result == "dropped_severity"

    real = open_ticket(env, make_event("EV-R1", 20, "compressor_pressure_drop", AIR))
    assert env.store.ticket(real.ticket_id).status == "open"
    assert [t.ticket_id for t in env.store.open_tickets()] == [real.ticket_id]
    # 噪声与真实异常都留在准入记录里，能看出谁被过滤了
    assert env.store.event("EV-LOW")["result"] == "dropped_severity"
    assert env.store.event("EV-R1")["ticket_id"] == real.ticket_id


def test_follow_up_event_merges_into_existing_ticket(env):
    """同一设备的后续同类事件：订阅直通，并入已有事件单并开下一轮，不重新研判。"""
    first = open_ticket(env, make_event("EV-1", 0, "compressor_pressure_drop", AIR))
    judge_calls = env.judge.calls
    second = env.router.handle_event(make_event("EV-2", 5, "compressor_pressure_drop", AIR))

    assert second.result == "straight_through" and second.ticket_id == first.ticket_id
    ticket = env.store.ticket(first.ticket_id)
    assert ticket.event_ids == ("EV-1", "EV-2") and ticket.round == 2
    assert env.judge.calls == judge_calls


def test_subscription_filters_other_event_types_on_same_entity(env):
    """订阅了变压器功率事件的事件单，不接收同一变压器下其他类型的事件。"""
    power = open_ticket(env, make_event("EV-P", 0, "load_spike", TRANSFORMER))
    assert env.store.matches(TRANSFORMER, "voltage_sag") == []
    other = env.router.handle_event(make_event("EV-V", 3, "voltage_sag", TRANSFORMER))

    assert other.result == "opened" and other.ticket_id != power.ticket_id
    assert power.ticket_id not in env.store.matches(TRANSFORMER, "voltage_sag")
    assert env.store.matches(TRANSFORMER, "load_spike") == [power.ticket_id]


def test_straight_through_event_does_not_call_jev_or_main_agent(env):
    """直通的事件不调用 Jev 与主 Agent。"""
    triage = CountingTriage()
    env.registry.mode_ports[(AgentResultSource, "deterministic", "triage")] = triage
    open_ticket(env, make_event("EV-1", 0, "compressor_pressure_drop", AIR))
    jev_calls, runs = env.judge.calls, triage.calls

    env.router.handle_event(make_event("EV-2", 4, "compressor_pressure_drop", AIR))
    assert env.judge.calls == jev_calls and triage.calls == runs


def test_round_two_proposals_do_not_mix_with_round_one(env):
    """连续两轮：第一轮派发后并入新事件，第二轮的提案就绪只含第二轮提案。"""
    dispatched: list[tuple[str, int]] = []
    ready: list[tuple[str, int, list[str]]] = []
    env.registry.register_hook(EXPERTS_HOOK, lambda ticket, b, experts: dispatched.append((ticket.ticket_id, b.round)))
    env.registry.register_hook(PROPOSALS_READY_HOOK,
                               lambda tid, round_: ready.append((tid, round_, [p.proposal_id for p in env.sink.list(tid, round_)])))

    first = open_ticket(env, make_event("EV-1", 0, "compressor_pressure_drop", AIR))
    ticket_id = first.ticket_id
    assert env.store.is_running(ticket_id)  # 第一轮专家还在跑
    round_one = env.sink.save(make_proposal("equipment", ticket_id, diagnosis="第一轮：泄漏"), binding(ticket_id, round_=1))

    merged = env.router.handle_event(make_event("EV-2", 6, "compressor_pressure_drop", AIR))
    assert merged.result == "straight_through" and env.store.ticket(ticket_id).round == 1  # 排到下一次，没有重派

    env.router.complete_round(ticket_id, 1)
    assert dispatched == [(ticket_id, 1), (ticket_id, 2)]
    round_two = env.sink.save(make_proposal("equipment", ticket_id, diagnosis="第二轮：已复现"), binding(ticket_id, round_=2))
    env.router.complete_round(ticket_id, 2)

    assert [entry[1] for entry in ready] == [1, 2]
    assert ready[1][2] == [round_two]  # 第二轮只收到第二轮的提案
    assert round_one not in ready[1][2]
    assert [p.proposal_id for p in env.sink.list(ticket_id, 2)] == [round_two]


def test_fork_hands_open_tickets_and_subscriptions_to_child_run(env):
    """知识发布分叉后，未关闭事件单与订阅在子运行里继续有效，并按新知识版本重派。"""
    first = open_ticket(env, make_event("EV-1", 0, "compressor_pressure_drop", AIR))
    ticket_id = first.ticket_id
    bindings = []
    env.registry.register_hook(EXPERTS_HOOK, lambda ticket, b, experts: bindings.append(b))

    forked_at = T0 + timedelta(minutes=8)
    env.router.on_fork(run_info("run-1"), run_info("run-2"), forked_at)
    assert env.store.run_of_ticket(ticket_id) == "run-2"
    assert env.store.matches(AIR, "compressor_pressure_drop") == [ticket_id]

    env.router.resume_after_fork(knowledge_version=4)
    assert env.store.ticket(ticket_id).round == 2
    assert bindings[-1].run_id == "run-2" and bindings[-1].knowledge_version == 4
    assert bindings[-1].trigger_at == forked_at  # 重派的新一轮 trigger_at 取 forked_at


def test_operator_instruction_goes_to_registered_handler_without_ticket(env):
    """值班人员一句话指令：识别为指令类事件，交给登记的处理器，不开事件单。"""
    seen = []
    env.registry.register_hook(INSTRUCTION_HOOK, seen.append)
    handled = env.router.handle_event(make_event("EV-I", 0, "operator_instruction", None, severity=2,
                                                 source="operator_message", summary="下午有领导参观，行政楼保持 24 ℃"))
    assert handled.result == "instruction" and handled.ticket_id is None
    assert [(e.event_id, e.payload["run_id"]) for e in seen] == [("EV-I", env.router.run_id)]
    assert env.store.event("EV-I")["event"].payload == {}
    assert env.store.open_tickets() == []


def test_instruction_handler_exception_is_isolated_and_loop_continues(env):
    """单个指令处理器抛异常：记 unhandled 而不穿出 handle_event，后续指令照常处理。"""
    def boom(event):
        raise ValueError("坏指令")

    env.registry.register_hook(INSTRUCTION_HOOK, boom)
    broken = env.router.handle_event(make_event("EV-I-BOOM", 0, "operator_instruction", None, severity=2,
                                                source="operator_message", summary="坏指令"))
    assert broken.result == "instruction_unhandled"
    assert env.store.event("EV-I-BOOM")["result"] == "instruction_unhandled"

    seen = []
    env.registry.register_hook(INSTRUCTION_HOOK, seen.append)
    later = env.router.handle_event(make_event("EV-I-LATER", 2, "operator_instruction", None, severity=2,
                                               source="operator_message", summary="后续指令"))
    assert later.result == "instruction" and [e.event_id for e in seen] == ["EV-I-LATER"]


def test_straight_through_switch_off_routes_to_jev(env):
    """直通开关关闭（B33 编排消融用）：订阅命中的事件也重新研判，不开直通。"""
    env.router.config = RouterConfig(straight_through=False)
    first = open_ticket(env, make_event("EV-1", 0, "compressor_pressure_drop", AIR))
    second = env.router.handle_event(make_event("EV-2", 3, "compressor_pressure_drop", AIR))
    assert second.result == "opened" and second.ticket_id != first.ticket_id


def test_replies_ready_fires_on_unquantifiable_disagreement(env):
    """两位专家对根因判断不同（量化不了的分歧）时，本轮结束后调用第 3 层钩子。"""
    fired = []
    env.registry.register_hook(REPLIES_READY_HOOK, lambda tid, round_: fired.append((tid, round_)))
    first = open_ticket(env, make_event("EV-1", 0, "compressor_pressure_drop", AIR))
    ticket_id = first.ticket_id
    for expert, view in (("equipment", "判为泄漏"), ("energy", "判为供电侧")):
        proposal_id = env.sink.save(make_proposal(expert, ticket_id, diagnosis=view), binding(ticket_id, round_=1))
        env.sink.attach_reply(proposal_id, ExpertReply(status="done", diagnosis=view))
    env.router.complete_round(ticket_id, 1)
    assert fired == [(ticket_id, 1)]


def test_lighting_fault_is_routed_to_energy_not_to_air_equipment(env):
    """第二幕：两起互不相关的故障分别派给专家，互不打扰。"""
    def dispatch(ticket, b, experts):
        for expert in experts:
            env.sink.save(make_proposal(expert, ticket.ticket_id), b)
        env.router.complete_round(ticket.ticket_id, b.round)

    env.registry.register_hook(EXPERTS_HOOK, dispatch)
    leak = open_ticket(env, make_event("EV-1", 0, "compressor_pressure_drop", AIR))
    light = open_ticket(env, make_event("EV-2", 0, "lighting_fault", LIGHT, severity=2))
    assert leak.experts == ("equipment",) and light.experts == ("energy",)
    assert [p.expert for p in env.sink.list(leak.ticket_id)] == ["equipment"]
    assert [p.expert for p in env.sink.list(light.ticket_id)] == ["energy"]



def test_ticket_revision_is_shared_per_writer_without_writer_attributes(env):
    """事件单水位按 writer 共享：同库另建的 RouterStore 推进同一计数，且不往 writer 上挂属性（#207）。"""
    other = RouterStore(env.writer, "plant")
    revision = env.store.ticket_revision
    other.set_running("ticket://TK-none", False)
    assert env.store.ticket_revision == revision + 1
    assert not hasattr(env.writer, "_ticket_revision") and not hasattr(env.writer, "_ticket_revision_lock")


def test_readings_hook_internal_type_error_is_not_retried_as_legacy(env):
    """#201-6：四参读数实现内部的 TypeError 原样抛出，不再回退三参重调把它掩盖掉。"""
    calls = []

    def readings(entity, start, end, run_id):
        calls.append(run_id)
        raise TypeError("读数实现内部错误")

    env.registry.register_hook("router.readings", readings)
    with pytest.raises(TypeError, match="读数实现内部错误"):
        env.router.sensor_readings(AIR, T0 - timedelta(minutes=5), T0)
    assert calls == ["run-1"]


def test_legacy_three_argument_readings_hook_still_works(env):
    """旧的 `(entity, start, end)` 读数实现照旧兼容（#186）。"""
    def readings(entity, start, end):
        return [{"at": T0.isoformat(), "value": 1.0, "quality": "ok"}]

    env.registry.register_hook("router.readings", readings)
    rows = env.router.sensor_readings(AIR, T0 - timedelta(minutes=5), T0)
    assert [(row["value"], row["quality"]) for row in rows] == [(1.0, "ok")]


def test_unhashable_readings_hook_is_not_blocked_by_signature_cache(env):
    """#201-6：签名缓存不能因为 callable 不可哈希而让读数钩子崩溃。"""
    class Readings:
        __hash__ = None

        def __call__(self, entity, start, end, run_id):
            return [{"at": T0.isoformat(), "value": 2.0, "quality": "ok"}]

    env.registry.register_hook("router.readings", Readings())
    rows = env.router.sensor_readings(AIR, T0 - timedelta(minutes=5), T0)
    assert [(row["value"], row["quality"]) for row in rows] == [(2.0, "ok")]
