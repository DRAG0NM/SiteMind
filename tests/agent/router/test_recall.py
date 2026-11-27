"""B27 提案存取与通用召回：ProposalSink 落库、plantkg_recall 按前缀展开一跳、8KB 预算。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from plantkg.agent.router.proposals import SqliteProposalSink
from plantkg.core.contracts import TOOL_TEXT_MAX_BYTES
from plantkg.core.contracts import ErrorCode, ExpertReply, PlantkgError
from tests.agent.router.helpers import AIR, PLANT, binding, context, make_event, make_proposal


def test_proposal_sink_assigns_id_and_attaches_reply(env):
    ticket_id = env.router.handle_event(make_event("EV-1", 0, "compressor_pressure_drop", AIR)).ticket_id
    proposal_id = env.sink.save(make_proposal("equipment", ticket_id, diagnosis="泄漏"), binding(ticket_id, round_=1))
    assert proposal_id.startswith("proposal://P-")
    stored = env.sink.get(proposal_id)
    assert stored.proposal_id == proposal_id and stored.ticket_id == ticket_id
    assert stored.binding is not None and stored.binding.round == 1

    updated = env.sink.attach_reply(proposal_id, ExpertReply(status="done", diagnosis="按手册判为泄漏", evidence_refs=("knowledge:leak",)))
    assert updated.diagnosis == "按手册判为泄漏" and updated.evidence_refs == ("knowledge:leak",)
    assert env.sink.replied(proposal_id)
    assert env.sink.get(proposal_id).evidence_refs == ("knowledge:leak",)


def test_recall_expands_proposal_to_ticket_and_evidence(env):
    ticket_id = env.router.handle_event(make_event("EV-1", 0, "compressor_pressure_drop", AIR)).ticket_id
    proposal_id = env.sink.save(make_proposal("equipment", ticket_id, evidence=("knowledge:compressor_leak_rule",)),
                                binding(ticket_id, round_=1))
    handler = env.registry.tools["recall"].handler
    result = handler(context(), proposal_id)

    assert result.ok and result.data["kind"] == "proposal"
    assert result.data["ticket_id"] == ticket_id
    next_ids = [item["id"] for item in result.data["items"]]
    assert ticket_id in next_ids and "knowledge:compressor_leak_rule" in next_ids
    assert "equipment" in result.data["summary"]


def test_recall_expands_ticket_to_proposals_and_events(env):
    ticket_id = env.router.handle_event(make_event("EV-1", 0, "compressor_pressure_drop", AIR)).ticket_id
    proposal_id = env.sink.save(make_proposal("equipment", ticket_id), binding(ticket_id, round_=1))
    data = env.registry.tools["recall"].handler(context(), ticket_id).data
    ids = [item["id"] for item in data["items"]]
    assert proposal_id in ids and "event://EV-1" in ids
    assert data["summary"].startswith("open")


def test_recall_is_a_registered_tool_and_respects_plant_scope(env):
    spec = env.registry.tools["recall"].spec
    assert spec.name == "recall" and spec.nexent_name == "plantkg_recall"
    with_other_plant = context("p2")
    try:
        env.registry.tools["recall"].handler(with_other_plant, "ticket://TK-0001")
    except PlantkgError as exc:
        assert exc.code == ErrorCode.CROSS_PLANT
    else:
        raise AssertionError("跨厂区召回必须拒绝")


def test_recall_rejects_unknown_prefix(env):
    try:
        env.registry.tools["recall"].handler(context(), "user://42")
    except PlantkgError as exc:
        assert exc.code == ErrorCode.INVALID_REQUEST
    else:
        raise AssertionError("未知前缀必须拒绝")


def test_recall_truncates_long_expansion_within_8kb(env):
    ticket_id = env.router.handle_event(make_event("EV-1", 0, "compressor_pressure_drop", AIR)).ticket_id
    evidence = tuple(f"knowledge:clause-{i:03d}-" + "x" * 40 for i in range(200))
    proposal_id = env.sink.save(make_proposal("equipment", ticket_id, evidence=evidence), binding(ticket_id, round_=1))
    result = env.registry.tools["recall"].handler(context(), proposal_id)
    text = result.to_agent_text()
    assert len(text.encode("utf-8")) <= TOOL_TEXT_MAX_BYTES
    assert result.data["truncated"] is True and result.data["next"]


def test_forked_sink_chains_to_parent_run(env):
    """分叉后的子运行提案库接父库：链内编号不碰撞，父运行提案按原 ID 可读、可回复，父库只留自己的提案。"""
    ticket_id = env.router.handle_event(make_event("EV-1", 0, "compressor_pressure_drop", AIR)).ticket_id
    parent_id = env.sink.save(make_proposal("equipment", ticket_id, diagnosis="父运行：泄漏"), binding(ticket_id, round_=1))
    child = SqliteProposalSink(env.writer, f"{PLANT}#run-2", parent=env.sink)

    child_id = child.save(make_proposal("equipment", ticket_id, diagnosis="子运行：按新手册复现"), binding(ticket_id, round_=2))
    assert (parent_id, child_id) == ("proposal://P-0001", "proposal://P-0002")
    assert child.get(parent_id).diagnosis == "父运行：泄漏"
    assert [p.proposal_id for p in child.list(ticket_id)] == [parent_id, child_id]
    assert [p.proposal_id for p in child.list(ticket_id, 1)] == [parent_id]
    assert child.attach_reply(parent_id, ExpertReply(status="done", diagnosis="父运行：判为泄漏")).diagnosis == "父运行：判为泄漏"
    assert child.replied(parent_id) and env.sink.replied(parent_id)
    assert [p.proposal_id for p in env.sink.list(ticket_id)] == [parent_id]  # 父库内容不被改


def test_in_flight_parent_sink_allocates_after_child_save(env):
    """旧父库引用延迟保存时，编号避开子库已用的 ID，召回和回答仍指向各自提案。"""
    ticket_id = "TK-901"
    started, release = threading.Event(), threading.Event()

    def delayed_save():
        sink = env.sink
        started.set()
        assert release.wait(10)
        return sink.save(make_proposal("equipment", ticket_id, diagnosis="父运行迟到提案"), binding(ticket_id))

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(delayed_save)
        try:
            assert started.wait(10)
            child = SqliteProposalSink(env.writer, f"{PLANT}#run-2", parent=env.sink, run_id="run-2")
            child_id = child.save(make_proposal("equipment", ticket_id, diagnosis="子运行提案"),
                                  binding(ticket_id, run_id="run-2"))
        finally:
            release.set()
        parent_id = pending.result(timeout=10)

    assert parent_id != child_id
    assert child.get(child_id).diagnosis == "子运行提案"
    assert child.get(parent_id).diagnosis == "父运行迟到提案"
    assert [p.proposal_id for p in child.list(ticket_id)] == [child_id, parent_id]
    assert [p.proposal_id for p in env.sink.list(ticket_id)] == [parent_id]
    child.attach_reply(child_id, ExpertReply(status="done", diagnosis="子运行回答"))
    assert child.replied(child_id) and not child.replied(parent_id)
    assert env.sink.get(parent_id).diagnosis == "父运行迟到提案"
    child.attach_reply(parent_id, ExpertReply(status="done", diagnosis="父运行回答"))
    assert env.sink.replied(parent_id) and child.replied(parent_id)
    assert env.sink.get(parent_id).diagnosis == "父运行回答"
    assert child.get(child_id).diagnosis == "子运行回答"


def test_recall_unknown_object_is_not_found(env):
    try:
        env.registry.tools["recall"].handler(context(), "proposal://P-9999")
    except PlantkgError as exc:
        assert exc.code == ErrorCode.NOT_FOUND
    else:
        raise AssertionError("不存在的提案要报 NOT_FOUND")
