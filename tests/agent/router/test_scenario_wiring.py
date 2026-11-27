"""验收冒烟：协同组的场景运行经值守层，动作来源与 B28 裁决记录一致。"""

from __future__ import annotations

import threading
from datetime import timedelta

from plantkg.agent.router import EXPERTS_HOOK, PROPOSALS_READY_HOOK, STEP_HOOK, URGENT_HOOK
from plantkg.agent.router.proposals import SqliteProposalSink
from plantkg.agent.router.router import SCENARIO_BIND_HOOK
from plantkg.agent.router.scenario import CoordinatedDispatcher
from plantkg.agent.watchdog import Watchdog
from plantkg.core.config import load_settings
from plantkg.core.contracts import (
    Actor, AgentResultKind, AgentRunRecord, AgentTiming, AgentToolCall, Context, ExpertProposal, ProposalCost, RunBinding, RunMode,
)
from plantkg.core.ports import ProposalSink
from plantkg.core.versions import read
from plantkg.integrations.nexent.proposals import wrap
from plantkg.world.scenario import ScenarioRunner, load_scenario, runner as scenario_runner
from plantkg.world.scenario.__main__ import ROOT, build_world, open_db, trace_json

DEMO = ROOT / "configs" / "scenarios" / "factory_demo_day.yaml"


def run_day(tmp_path, group):
    scenario = load_scenario(DEMO)
    world = build_world(scenario)
    db = open_db(tmp_path / f"{group}.db", world.site.plant_id)
    runner = ScenarioRunner(world, db, group=group)
    return runner, runner.run(), db


def test_demo_day_runs_through_router(tmp_path):
    runner, trace, db = run_day(tmp_path, "coordinated")
    direct, baseline, direct_db = run_day(tmp_path, "independent")
    try:
        assert isinstance(runner.dispatcher, CoordinatedDispatcher)
        store = runner.dispatcher.router.store
        channels = {t: store.channel_of(t) for t in trace.tickets}
        assert set(channels.values()) == {"fast", "slow", "urgent"}
        assert all(store.ticket(t).status == rec["status"] for t, rec in trace.tickets.items())
        assert read(db.writer, lambda con: con.execute("SELECT COUNT(*) FROM b27_proposals").fetchone())[0] == len(trace.proposals)
        assert any(r.kind == "investigate" for r in trace.agent_runs)  # 慢通道经主 Agent 调查
        assert len(trace.runs) == len(baseline.runs) > 1  # 知识发布分叉后值守层跟到子运行
        assert runner.dispatcher.router.run_id == trace.run_id
        selected = {pid for decision in trace.decisions for pid in decision.result.selected_proposal_ids}
        applied = {a["source"]["proposal_id"] for a in trace.actions if "proposal_id" in a["source"]}
        assert trace.decisions and applied <= selected
        stored = {(p.binding.run_id, SqliteProposalSink(db.writer, f"{db.plant_id}#{p.binding.run_id}").get(p.proposal_id).proposal_id)
                  for p in trace.proposals}  # 输出的提案编号都能在所属运行的库里查到
        assert stored and {(r.binding.run_id, i) for r in trace.agent_runs for i in r.proposal_ids} <= stored
        assert {a["source"]["proposal_id"] for a in trace.actions if "proposal_id" in a["source"]} <= {pid for _, pid in stored}
        summary = trace.ledger_summary()
        assert summary["minutes_balanced"] == summary["minutes"] and not trace.slot_problems
    finally:
        db.close()
        direct_db.close()


def test_fork_rebuilds_proposal_store_for_child_run(tmp_path):
    """知识发布分叉后提案库按子运行重建：live 提案按当前 run_id 落库、路由读得回，父运行库不再写入。"""
    scenario = load_scenario(DEMO)
    world = build_world(scenario)
    db = open_db(tmp_path / "fork.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, group="coordinated")
        trace = runner.run(until=scenario.instant("08:06"))  # 07:30 知识发布分叉后的第一个事件
        parent_run, child_run = trace.runs[0].run_id, runner.run_info.run_id
        router = runner.dispatcher.router
        assert trace.runs[-1].forked_from == parent_run and runner.now > scenario.instant("07:30")
        assert trace.run_id == child_run == router.run_id
        sink = runner.registry.ports[ProposalSink]
        assert sink.plant_id == f"{db.plant_id}#{child_run}"  # 域包（live 专家）拿到的是子运行提案库
        assert router.sink is sink and router.recall.sink is sink  # 路由与召回跟着换
        scopes = read(db.writer, lambda con: con.execute("SELECT DISTINCT plant_id FROM b27_proposals").fetchall())
        assert {row[0] for row in scopes} == {f"{db.plant_id}#{child_run}"}  # 分叉后的提案都进子运行库
        ticket_id = next(iter(trace.tickets))
        binding = RunBinding(run_id=child_run, knowledge_version=runner.knowledge_version, ticket_id=ticket_id,
                             round=trace.tickets[ticket_id]["round"], state_version=runner.state.state_version, trigger_at=runner.now)
        proposal_id = sink.save(ExpertProposal(expert="energy", ticket_id=ticket_id, cost=ProposalCost(cny=0.0), confidence=0.9), binding)
        assert proposal_id in {p.proposal_id for p in router.sink.list(ticket_id, binding.round)}
        assert proposal_id in {p.proposal_id for p in router.recall.sink.list(ticket_id, binding.round)}
        parent_sink = SqliteProposalSink(db.writer, f"{db.plant_id}#{parent_run}")
        assert parent_sink.list(ticket_id, binding.round) == []  # 父运行库不动
    finally:
        db.close()


def test_fork_keeps_parent_run_proposals_recallable(tmp_path):
    """分叉前父运行的提案在子运行仍按原 ID 召回；子运行新提案另给编号，同一事件单历史能看到两份。"""
    scenario = load_scenario(DEMO)
    world = build_world(scenario)
    db = open_db(tmp_path / "fork_history.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, group="coordinated")
        runner.run(until=scenario.instant("07:29"))  # 分叉发生在 07:30 知识发布
        router, parent_run = runner.dispatcher.router, runner.run_info.run_id
        ticket_id = "TK-900"  # 分叉前父运行已有提案的事件单
        old_id = router.sink.save(
            ExpertProposal(expert="equipment", ticket_id=ticket_id, cost=ProposalCost(cny=0.0), confidence=0.8,
                           diagnosis="分叉前：空压站泄漏"),
            RunBinding(run_id=parent_run, knowledge_version=runner.knowledge_version, ticket_id=ticket_id, round=1,
                       state_version=runner.state.state_version, trigger_at=runner.now))
        assert old_id == "proposal://P-0001"

        runner.run(until=scenario.instant("08:06"))
        child_run, chain = runner.run_info.run_id, router.recall.sink
        assert child_run != parent_run and chain.plant_id == f"{db.plant_id}#{child_run}"
        recalled = router.recall(Context(plant_id=db.plant_id, industry_pack="manufacturing",
                                         actor=Actor(actor_id="duty", roles=("operator",)), channel="offline",
                                         trace_id="t", as_of=runner.now), old_id)
        assert recalled.data["kind"] == "proposal" and "分叉前：空压站泄漏" in recalled.data["summary"]

        new_id = chain.save(
            ExpertProposal(expert="equipment", ticket_id=ticket_id, cost=ProposalCost(cny=0.0), confidence=0.8,
                           diagnosis="分叉后：按新手册复现"),
            RunBinding(run_id=child_run, knowledge_version=runner.knowledge_version, ticket_id=ticket_id, round=2,
                       state_version=runner.state.state_version, trigger_at=runner.now))
        assert new_id != old_id and int(new_id.split("-")[-1]) > int(old_id.split("-")[-1])
        assert chain.get(new_id).diagnosis == "分叉后：按新手册复现"
        assert [p.proposal_id for p in chain.list(ticket_id)] == [old_id, new_id]  # 事件单历史：父库 + 子库
        parent_only = SqliteProposalSink(db.writer, f"{db.plant_id}#{parent_run}")
        assert [p.proposal_id for p in parent_only.list(ticket_id)] == [old_id]
    finally:
        db.close()


def test_in_flight_live_proposal_across_fork_stays_in_own_run(tmp_path):
    """分叉时在途的 live 专家调用跨分叉返回：提案按 binding.run_id 写回父运行库，父子各自历史查得到。"""
    scenario = load_scenario(DEMO)
    world = build_world(scenario)
    db = open_db(tmp_path / "fork_in_flight.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, group="coordinated")
        runner.run(until=scenario.instant("07:29"))  # 知识发布分叉发生在 07:30
        parent_run = runner.run_info.run_id
        ticket_id = "TK-901"  # 分叉前父运行已有提案的事件单
        binding = RunBinding(run_id=parent_run, knowledge_version=runner.knowledge_version, ticket_id=ticket_id, round=1,
                             state_version=runner.state.state_version, trigger_at=runner.now)
        old_id = runner.registry.ports[ProposalSink].save(
            ExpertProposal(expert="equipment", ticket_id=ticket_id, cost=ProposalCost(cny=0.0), confidence=0.8,
                           diagnosis="分叉前：父运行提案"), binding)
        assert old_id == "proposal://P-0001"

        started, release = threading.Event(), threading.Event()

        def inner(ctx: Context, binding: RunBinding, **params) -> ExpertProposal:
            started.set()
            assert release.wait(30), "分叉后没放开在途调用"
            return ExpertProposal(expert="hvac", ticket_id=binding.ticket_id, cost=ProposalCost(cny=0.0), confidence=0.9,
                                  diagnosis="分叉前发出、分叉后返回的在途提案")

        def propose(ctx: Context) -> object:
            # 网关先取得父库引用：业务函数在后台线程跨过分叉才返回并保存。
            return wrap("hvac", inner, lambda: runner.registry.resolve(ProposalSink), lambda c: (binding, None),
                        lambda c, pid: None)(ctx)

        results = []
        thread = threading.Thread(target=lambda: results.append(propose(Context(
            plant_id=db.plant_id, industry_pack="manufacturing", actor=Actor(actor_id="duty", roles=("operator",)),
            channel="offline", trace_id="t", as_of=runner.now))))
        thread.start()
        assert started.wait(30), "在途调用没开始"
        runner.run(until=scenario.instant("08:06"))  # 跑过 07:30 分叉
        child_run, chain = runner.run_info.run_id, runner.registry.ports[ProposalSink]
        assert child_run != parent_run and chain.plant_id == f"{db.plant_id}#{child_run}"
        release.set()
        thread.join(30)
        assert not thread.is_alive()

        new_id = results[0].data["proposal_id"]
        parent_only = SqliteProposalSink(db.writer, f"{db.plant_id}#{parent_run}")
        assert [p.proposal_id for p in parent_only.list(ticket_id)] == [old_id, new_id]  # 在途提案回到父运行历史
        assert parent_only.get(new_id).diagnosis == "分叉前发出、分叉后返回的在途提案"
        scope_of = read(db.writer, lambda con: con.execute(
            "SELECT plant_id, run_id FROM b27_proposals WHERE proposal_id=?", (new_id,)).fetchone())
        assert scope_of == (f"{db.plant_id}#{parent_run}", parent_run)

        child_id = chain.save(
            ExpertProposal(expert="hvac", ticket_id=ticket_id, cost=ProposalCost(cny=0.0), confidence=0.8,
                           diagnosis="分叉后：子运行提案"),
            RunBinding(run_id=child_run, knowledge_version=runner.knowledge_version, ticket_id=ticket_id, round=2,
                       state_version=runner.state.state_version, trigger_at=runner.now))
        assert int(child_id.split("-")[-1]) > int(new_id.split("-")[-1])  # 编号仍跨运行链唯一
        assert [p.proposal_id for p in chain.list(ticket_id)] == [old_id, new_id, child_id]
        assert [p.proposal_id for p in parent_only.list(ticket_id)] == [old_id, new_id]  # 子运行提案不混进父库
    finally:
        db.close()


def test_pack_hooks_run_in_scenario_and_async_round_syncs_view(tmp_path):
    """领域包登记的业务钩子在协同组里被调用；异步完成一轮后，视图与 trace 跟上 RouterStore。"""
    source = scenario_runner._load_packs.__wrapped__(load_settings().packs)
    held, ready, urgent, steps, bound = [], [], [], [], []
    source.register_hook(EXPERTS_HOOK, lambda ticket, binding, experts: held.append((ticket, binding)))
    source.register_hook(PROPOSALS_READY_HOOK, lambda ticket_id, round_: ready.append((ticket_id, round_)))
    source.register_hook(URGENT_HOOK, lambda event, reason: urgent.append(event.event_id))
    source.register_hook(STEP_HOOK, lambda run_id, now: steps.append(now))
    source.register_hook(SCENARIO_BIND_HOOK, lambda router, runner: bound.append(router))

    scenario = load_scenario(DEMO)
    world = build_world(scenario)
    db = open_db(tmp_path / "hooks.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, scenario_runner.default_registry(source), group="coordinated")
        trace = runner.run(until=scenario.instant("14:25"))
        router = runner.dispatcher.router
        assert held and steps and urgent == ["act3-trapped"] and bound == [router]
        assert not trace.proposals  # 专家钩子接管后，同步替身不再出提案

        ticket, binding = held[-1]  # 分叉前的绑定已作废，取当前运行的一轮
        proposal_id = runner.registry.ports[ProposalSink].save(
            ExpertProposal(expert="energy", ticket_id=ticket.ticket_id, cost=ProposalCost(cny=0.0), confidence=0.9), binding)
        router.complete_round(ticket.ticket_id, binding.round)
        assert ready == [(ticket.ticket_id, binding.round)] and proposal_id.startswith("proposal://")
        assert router.store.ticket(ticket.ticket_id).status == "resolved"

        runner.dispatcher.step(runner.now)
        view, record = runner.tickets.tickets[ticket.ticket_id].ticket, trace.tickets[ticket.ticket_id]
        assert (view.status, view.round) == (record["status"], record["round"]) == ("resolved", binding.round)
    finally:
        db.close()


def test_watchdog_blocked_ticket_syncs_view_next_minute(tmp_path):
    """Watchdog 的受阻回写推进事件单水位：下一分钟场景视图就同步，不用等 15 分钟兜底（#perf）。"""
    scenario = load_scenario(DEMO)
    world = build_world(scenario)
    db = open_db(tmp_path / "watchdog_watermark.db", world.site.plant_id)
    try:
        runner = ScenarioRunner(world, db, group="coordinated")
        trace = runner.run(until=scenario.instant("14:25"))
        store, ticket_id = runner.dispatcher.router.store, "ticket://TK-0004"
        ticket = store.ticket(ticket_id)
        # 守卫条件：仍是本轮打开、无运行、非 urgent、无待办提问/试探/任务
        store.save_ticket(ticket.model_copy(update={"status": "open"}), run_id=store.run_of_ticket(ticket_id), running=False)
        store.set_channel(ticket_id, "fast")
        assert store.probe_of(ticket_id) is None
        assert not any(q["status"] == "pending" for q in store.questions(ticket_id))

        runs = store.agent_runs(ticket_id)  # 运行记录按墙上时间排序：把巡检要读的失败记录排在最后
        at = (max(r.timing.requested_at for r in runs) if runs else runner.now) + timedelta(seconds=1)
        binding = RunBinding(run_id=store.run_of_ticket(ticket_id), knowledge_version=runner.knowledge_version,
                             ticket_id=ticket_id, round=ticket.round, state_version=0, trigger_at=at)
        timing = AgentTiming(origin_event_at=at, trigger_at=at, effective_at=at, requested_at=at, returned_at=at)
        call = AgentToolCall(call_id="call-1", agent_name="plantkg_main", tool="x", started_at=at, duration_ms=1,
                             business_status="failed", summary="PLANTKG_E_FORBIDDEN: 越权")
        store.save_agent_run(AgentRunRecord(agent_run_id="ar-watchdog", kind=AgentResultKind.investigate,
                                            agent_name="plantkg_main", run_mode=RunMode.live, source="deterministic",
                                            binding=binding, timing=timing, status="failed", tool_calls=(call,)))
        Watchdog(store, db.runs, db.writer, store.plant_id, {}).check()
        assert store.ticket(ticket_id).status == "blocked"
        assert runner.tickets.tickets[ticket_id].ticket.status == "resolved"  # 视图还停在巡检前

        next_minute = runner.now + timedelta(minutes=1)
        assert next_minute.minute % 15 != 0  # 不靠 15 分钟兜底
        runner.dispatcher.step(next_minute)
        assert runner.tickets.tickets[ticket_id].ticket.status == "blocked"
        assert trace.tickets[ticket_id]["status"] == "blocked"
    finally:
        db.close()


def test_demo_day_proposal_numbering_is_reproducible(tmp_path):
    """同一剧本连跑 3 次：提案编号、选中提案、冲突摘要逐项一致（并行专家按固定顺序落库，#207 P2）。"""
    exports = []
    for i in range(3):
        scenario = load_scenario(DEMO)
        world = build_world(scenario)
        db = open_db(tmp_path / f"repro{i}.db", world.site.plant_id)
        runner = ScenarioRunner(world, db, group="coordinated")
        trace = runner.run()
        try:
            exported = trace_json(trace, runner.world.scenario)
            proposals = read(db.writer, lambda con: con.execute(
                "SELECT proposal_id, ticket_id, round, expert FROM b27_proposals ORDER BY seq").fetchall())
            exports.append((proposals, [(d["ticket_id"], d["round"], d["conflict"]["summary"], d["conflict"]["proposal_ids"],
                                         d["result"]["selected_proposal_ids"], d["result"]["summary"])
                                        for d in exported["decisions"]]))
        finally:
            db.close()
    assert exports[0] == exports[1] == exports[2]
