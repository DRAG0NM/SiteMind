"""场景协同组接入 live：python scripts/nexent_live_scenario.py configs/scenarios/factory_demo_day.yaml --until 09:00

在本进程起 plantkg 服务（MCP + REST，Nexent 容器经 host.docker.internal 回调），再：
1. 用服务的 writer 建场景库，场景运行、版本清单与运行记录都在服务的 RunStore 里，网关能取到；
2. 把服务注册表里的 live 结果源接进场景注册表，协同组专家由 B29 会诊真调 Nexent；
3. 专家提案按运行分发到场景路由的提案库，进 complete_round 与直用兜底；
4. 预演照常开启：预演只算确定性提案供对比，真实事件一律由 live 专家现场算提案，
   算完把对比结果写成事件单状态 preview_check_<轮>。
5. 每次真实会诊前先在本批次调用账本（--budget-ledger，默认 runtime/issue244-batch-budget.json）预留一次；
   超预算或超调用上限直接拒绝，不再发请求。账本与 B33/D54 共用，预留不是账单。
需要本机 Nexent 已导入 Agent 与环境变量 NEXENT_API_KEY。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plantkg.agent.consultation import PREVIEW_CHECK_KEY  # noqa: E402
from plantkg.core.contracts import AgentResultKind, ErrorCode, PlantkgError  # noqa: E402
from plantkg.core.ports import AgentResultSource, ProposalSink  # noqa: E402
from plantkg.core.registry import Registry  # noqa: E402
from plantkg.evaluation.ledger import (  # noqa: E402
    DEFAULT_BUDGET_CNY,
    DEFAULT_MAX_CALLS,
    DEFAULT_PER_CALL_CNY,
    CallLedger,
    LedgeredSource,
    resolve_ledger_default,
)
from plantkg.integrations.nexent.scenario import RunSinkRouter  # noqa: E402
from plantkg.world.scenario.__main__ import build_world  # noqa: E402
from plantkg.world.scenario.runner import ScenarioDB, ScenarioRunner, default_registry  # noqa: E402
from plantkg.world.scenario.script import Scenario, load_scenario  # noqa: E402

STARTUP_TIMEOUT_S = 30.0
REPLY_MARGIN_S = 30.0  # 后台回答在运行截止之后还要挂接和落库
DONE_TICKETS = ("resolved", "closed")
DEFAULT_LEDGER = resolve_ledger_default(Path(__file__).resolve().parents[1] / "runtime" / "issue244-batch-budget.json")  # 锚在仓库根；ISSUE244_LEDGER 优先


def wrap_live_with_ledger(runner: ScenarioRunner, ledger: CallLedger) -> list[str]:
    """把 runner 注册表里的 live 结果源逐个包上账本；返回被包住的 kind 列表。

    会诊是异步的（回答在后台挂接），包装必须在 start() 之前完成，否则第一批会诊会绕过预算。
    """
    wrapped = []
    for slot, impl in list(runner.registry.mode_ports.items()):
        port, mode, key = slot
        if port is AgentResultSource and mode == "live" and key:
            runner.registry.mode_ports[slot] = LedgeredSource(ledger, impl, kind=key)
            wrapped.append(key)
    return sorted(wrapped)


DECISIONS = ("sampling", "to_human", "rejected", "conflict", "skipped_duplicate", "history")


def build_runner(scenario: Scenario, writer: Any, plugins: Registry, *, live_sources: Mapping[str, Any] | None = None,
                 original_sink: Any = None) -> ScenarioRunner:
    world = build_world(scenario)
    db = ScenarioDB.from_writer(writer, world.site.plant_id)
    registry = default_registry(plugins=plugins)
    for key, impl in (live_sources or {}).items():
        registry.mode_ports[(AgentResultSource, "live", key)] = impl
    runner = ScenarioRunner(world, db, registry=registry, mode="live", group="coordinated")
    sink = original_sink if original_sink is not None else plugins.ports.get(ProposalSink)
    if sink is None:
        raise PlantkgError(ErrorCode.PRECONDITION, "缺少原 ProposalSink（需要 B27 的提案库）")
    plugins.ports[ProposalSink] = RunSinkRouter(sink, registry, lambda: runner.run_info.run_id if runner.run_info else None)
    return runner


def _preview_check(router: Any, ticket_id: str, round_: Any) -> dict[str, Any]:
    """事件单本轮与预演的对比（B29 写的事件单状态 preview_check_<轮>）；还没写过时为空。"""
    if not isinstance(round_, int):
        return {}
    raw = router.store.ticket_state(ticket_id, f"{PREVIEW_CHECK_KEY}_{round_}")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def collect_report(runner: ScenarioRunner, trace: Any, scenario: Scenario) -> dict[str, Any]:
    router = runner.dispatcher.router
    tickets = []
    for ticket_id, view in trace.tickets.items():
        stored = router.store.ticket(ticket_id)
        proposals = router.sink.list(ticket_id)
        runs = [r for r in router.runs if r.kind == AgentResultKind.expert and r.binding is not None
                and r.binding.ticket_id == ticket_id]
        experts = []
        for record in runs:
            mine = [p for p in proposals if p.proposal_id in set(record.proposal_ids)]
            experts.append({"expert": record.agent_name, "source": record.source, "coverage_gap": record.coverage_gap,
                            "status": record.status, "proposals": len(mine), "actions": sum(len(p.actions) for p in mine)})
        applied = [a for a in trace.actions if a.get("source", {}).get("ticket_id") == ticket_id]
        proposal_ids = {p.proposal_id for p in proposals}
        notes = [n for n in trace.notes
                 if n.get("ticket_id") == ticket_id or n.get("proposal_id") in proposal_ids]
        decisions = [f"{n['kind']}（{n['reason']}）" if n.get("reason") else n["kind"]
                     for n in notes if n.get("kind") in DECISIONS]
        tickets.append({"ticket_id": ticket_id, "status": stored.status if stored is not None else view.get("status"),
                        "round": view.get("round"), "summary": view.get("summary", ""), "experts": experts,
                        "preview_check": _preview_check(router, ticket_id, view.get("round")),
                        "proposals": len(proposals), "applied_actions": len(applied),
                        "decisions": decisions})
    return {"scenario": scenario.scenario_id, "mode": "live", "group": "coordinated",
            "run_id": trace.runs[-1].run_id if trace.runs else None, "tickets": tickets,
            "expert_runs": sum(len(t["experts"]) for t in tickets), "proposals": len(trace.proposals),
            "applied_actions": len(trace.actions)}


def format_report(report: Mapping[str, Any]) -> str:
    lines = [f"# {report['scenario']}（{report['mode']} / {report['group']}），运行 {report['run_id']}"]
    for ticket in report["tickets"]:
        lines.append(f"{ticket['ticket_id']} {ticket['status']} 第 {ticket['round']} 轮：{ticket['summary']}")
        check = ticket.get("preview_check") or {}
        if check.get("matched") and check.get("same_actions"):
            lines.append("  [预演对比] 与预演一致")
        elif check.get("matched"):  # 预演只供对比，偏差照实报告
            lines.append(f"  [预演对比] 预演偏差：{'、'.join(check.get('diff') or ())}")
        for expert in ticket["experts"]:
            lines.append(f"  {expert['expert']:<9} 来源 {expert['source']:<12} coverage_gap={expert['coverage_gap']} "
                         f"状态 {expert['status']:<9} 提案 {expert['proposals']} 份 动作 {expert['actions']} 个")
        decision = "、".join(ticket["decisions"]) or "直用兜底"
        lines.append(f"  裁决：{decision}；执行 {ticket['applied_actions']} 个动作；提案 {ticket['proposals']} 份")
    lines.append(f"合计：{len(report['tickets'])} 张事件单，{report['expert_runs']} 次专家运行，"
                 f"{report['proposals']} 份提案，{report['applied_actions']} 个动作")
    if report.get("replies_pending"):
        lines.append("后台回答没有等齐，本次判失败")
    budget = report.get("budget")
    if budget:
        lines.append(f"预算账本：{budget['path']}，已预留 {budget['reserved_cny']:.2f} 元 / 上限 "
                     f"{budget['budget_cny']:.2f} 元，调用 {budget['calls']}/{budget['max_calls']} 次"
                     "（预留是客户端门禁，不是账单）")
    return "\n".join(lines)


def passed(report: Mapping[str, Any]) -> bool:
    expert_runs = [e for t in report["tickets"] for e in t["experts"]]
    matched = [t for t in report["tickets"] if (t.get("preview_check") or {}).get("matched")]
    return (bool(report["tickets"]) and not report.get("replies_pending")
            and all(t["status"] in DONE_TICKETS for t in report["tickets"])
            and all(e["status"] == "finished" for e in expert_runs)
            and any(e["source"] == "nexent" for e in expert_runs)
            and any(e["source"] == "nexent" and not e["coverage_gap"] and e["actions"] > 0 for e in expert_runs)
            and all(t["proposals"] <= len(t["experts"]) for t in report["tickets"])  # 同轮同专家只有一份提案
            and all(any(e["source"] == "nexent" for e in t["experts"]) for t in matched))  # 匹配到预演的也是 live 现算


def start_server(server: Any, timeout: float = STARTUP_TIMEOUT_S) -> threading.Thread:
    """后台起服务；线程提前退出（如端口被占）或超时都报错，不无限等待。"""
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive():
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务启动失败，端口可能被占用")
        if time.monotonic() >= deadline:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"服务 {timeout:.0f} 秒内没有启动")
        time.sleep(0.1)
    return thread


def wait_replies(consultations: Iterable[Any], timeout: float) -> bool:
    """等后台回答；任何一个会诊没等齐就返回 False。"""
    return all([consultation.wait_replies(timeout) for consultation in consultations])


def _tenant_of_admin_jwt() -> dict[str, Any]:
    """用管理员 JWT 回读当前租户，供 --preflight-only 打印证据（只读 GET，不发推理）。"""
    jwt = os.environ.get("NEXENT_ADMIN_JWT")
    if not jwt:
        return {"note": "未设置 NEXENT_ADMIN_JWT，无法回读租户"}
    import httpx

    try:
        with httpx.Client(timeout=10, trust_env=False, headers={"Authorization": "Bearer " + jwt}) as client:
            response = client.get("http://127.0.0.1:5010/user/current_user_info")
            response.raise_for_status()
            body = response.json()
    except Exception as exc:  # noqa: BLE001 - 预检要能把"连不上/过期"直接报出来
        return {"error": f"{type(exc).__name__}: {exc}"}
    data = body.get("data") or body
    data = data.get("user") or data
    return {key: data.get(key) for key in ("tenant_id", "user_id", "user_email", "user_role")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/nexent_live_scenario.py")
    parser.add_argument("scenario")
    parser.add_argument("--until", default="09:00", help="剧本时刻，如 09:00")
    parser.add_argument("--port", type=int, default=8710)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--json", action="store_true")
    # Issue #244 隔离实验：默认沿用配置里的共享 Agent，显式传参才切到独立 Agent
    parser.add_argument("--agents-dir", default=None, help="Agent 资产目录（默认取配置 nexent.agents_dir）")
    parser.add_argument("--main-agent", default=None, help="主 Agent 名（默认取配置 nexent.main_agent）")
    # Issue #244 统一预算：B23 一次运行可能调多次专家，默认就已开启账本门禁
    parser.add_argument("--budget-ledger", default=str(DEFAULT_LEDGER), help="调用账本（与 B33/D54 共用同一份）")
    parser.add_argument("--budget-cny", type=float, default=DEFAULT_BUDGET_CNY)
    parser.add_argument("--per-call-cny", type=float, default=DEFAULT_PER_CALL_CNY)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--budget-model", default="qwen4-max", help="账本里记录的模型名")
    parser.add_argument("--preflight-only", action="store_true",
                        help="只校验 Agent 资产、live 结果源、账本与租户，不发任何模型请求")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    import uvicorn

    from plantkg.agent.consultation import SERVICE_HOOK
    from plantkg.api import create_app
    from plantkg.bootstrap import build_services
    from plantkg.core.config import load_settings
    from plantkg.mcp_server import create_mcp_server

    overrides = {"server.host": "0.0.0.0", "server.port": str(args.port), "nexent.enabled": "true", "storage.enabled": "true",
                 # 一次账本预留必须对应一次 Nexent 运行：live 结果源的重试在内部循环，
                 # 若开重试，一次 get() 可能触发两轮专家调用而账本只预留一次，属于未覆盖的支出。
                 "nexent.run_retries": "0"}
    if args.db:
        overrides["storage.sqlite"] = args.db
    if args.agents_dir:
        overrides["nexent.agents_dir"] = args.agents_dir
    if args.main_agent:
        overrides["nexent.main_agent"] = args.main_agent
    settings = load_settings(profile="dev", overrides=overrides)
    services = build_services(settings)
    ledger: CallLedger | None = None
    server = uvicorn.Server(uvicorn.Config(create_app(services, create_mcp_server(services)), host="0.0.0.0",
                                           port=args.port, log_level="warning"))
    try:
        scenario = load_scenario(args.scenario)
        if not args.preflight_only:
            start_server(server)
        runner = build_runner(scenario, services.storage.writer, services.registry,
                              original_sink=services.registry.resolve(ProposalSink))
        ledger = CallLedger(args.budget_ledger, model=args.budget_model, budget_cny=args.budget_cny,
                            per_call_cny=args.per_call_cny, max_calls=args.max_calls, source="b23-nexent-live")
        wrapped = wrap_live_with_ledger(runner, ledger)
        if not wrapped:
            raise PlantkgError(ErrorCode.PRECONDITION, "没有可加账本的 live 结果源：B23 必须在 nexent.enabled=true 下运行")
        if args.preflight_only:
            # 真实运行前把"会打到哪个租户、哪些 Agent、哪本账"确认清楚：只读本地配置与 Nexent 管理接口，
            # 不调用 /nb/v1/chat/run，也不调模型。租户靠管理员 JWT 回读，避免看错环境。
            tenant = _tenant_of_admin_jwt()
            summary = {
                "preflight": True, "requests_sent": 0,
                "tenant_id": tenant.get("tenant_id"), "tenant_user": tenant.get("user_email"),
                "agents_dir": str(settings.nexent.agents_dir), "main_agent": settings.nexent.main_agent,
                "live_sources": wrapped, "nexent_enabled": settings.nexent.enabled,
                "run_retries": settings.nexent.run_retries, "run_deadline_s": settings.nexent.run_deadline_s,
                "reply_timeout_s": settings.nexent.run_deadline_s * (settings.nexent.run_retries + 1) + REPLY_MARGIN_S,
                "scenario_db": args.db or str(getattr(settings.storage, "sqlite", "")),
                "budget": ledger.snapshot(),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=1))
            return 0
        runner.start()
        from plantkg.experts.energy.minute import install_minute_layer  # live 与 CLI 同为权威入口（#188）
        install_minute_layer(runner)
        trace = runner.run_until(scenario.end if args.until == "end" else scenario.instant(args.until))
        # 回答在后台挂接：等够一次运行的全部重试，等不齐就判失败
        reply_timeout = settings.nexent.run_deadline_s * (settings.nexent.run_retries + 1) + REPLY_MARGIN_S
        replied = wait_replies(services.registry.hooks_for(SERVICE_HOOK), reply_timeout)
        report = collect_report(runner, trace, scenario) | {"replies_pending": not replied,
                                                           "budget": ledger.snapshot()}
        if args.out:
            from plantkg.world.scenario.__main__ import trace_json

            out = Path(args.out)
            # tenant_env 的 ISSUE244_B23_OUT 是目录；旧行为直接 write_text 会在目录上抛
            # PermissionError，把整轮证据丢掉。目录（含以分隔符结尾的新目录）解释为 day.json。
            if out.is_dir() or str(args.out).endswith(("/", "\\")):
                out = out / "day.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(trace_json(trace, scenario), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else format_report(report))
        return 0 if passed(report) else 1
    except PlantkgError as exc:
        print(f"场景 live 失败：{exc.message}", file=sys.stderr)
        return 1
    except ValueError as exc:  # 预算门禁 / 账本格式问题：拒绝发请求，并保留账本证据
        print(f"场景 live 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        server.should_exit = True
        time.sleep(0.5)
        services.close()
        if ledger is not None:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
