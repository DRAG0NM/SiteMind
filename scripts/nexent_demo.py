"""本机演示（B23 怎么看效果）：python scripts/nexent_demo.py [--experts energy,hvac] [--ask 问题]

在本进程起 plantkg 服务（MCP + REST，Nexent 容器经 host.docker.internal 回调），然后：
1. 会诊路径：同一事件单下按 --experts 经 AgentResultSource(live) → run_expert 并行发起专家运行；
2. 问答路径（给了 --ask）：经网关问主 Agent，它调用专家子 Agent 再回答；
最后打印本次的 agent_runs 记录与落库的提案。需要本机 Nexent 已导入 Agent（import_assets）与环境变量 NEXENT_API_KEY。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plantkg.core.config import load_settings  # noqa: E402
from plantkg.core.contracts import Actor, AgentResultKind, RunBinding, Role  # noqa: E402
from plantkg.core.ports import AgentResultSource, ProposalSink  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/nexent_demo.py")
    parser.add_argument("--experts", default="energy,hvac")
    parser.add_argument("--ticket", default="ticket://tk-demo-0001")
    parser.add_argument("--ask", default=None, help="再走一次问答路径：问主 Agent 的问题")
    parser.add_argument("--port", type=int, default=8700)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    import uvicorn

    from plantkg.api import create_app
    from plantkg.bootstrap import build_services
    from plantkg.mcp_server import create_mcp_server

    # 不启用存储：演示用的 run-b23-demo 不在运行存储里，按 dev 替身绑定合成清单号；记录与提案在内存里
    settings = load_settings(profile="dev", overrides={
        "server.host": "0.0.0.0", "server.port": str(args.port), "nexent.enabled": "true"})
    services = build_services(settings)
    server = uvicorn.Server(uvicorn.Config(create_app(services, create_mcp_server(services)), host="0.0.0.0", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    gateway, store = services.nexent.gateway, services.nexent.store
    try:
        binding = RunBinding(run_id="run-b23-demo", knowledge_version=1, ticket_id=args.ticket, round=1, state_version=0,
                             trigger_at=settings.world.default_as_of)
        source = services.registry.resolve_mode(AgentResultSource, "live", "expert")
        experts = [e.strip() for e in args.experts.split(",") if e.strip()]
        print(f"== 会诊：{args.ticket} 并行发起 {experts}")
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(experts)) as pool:
            results = list(pool.map(lambda e: source.get(AgentResultKind.expert, binding, {"expert": e}), experts))
        print(f"   两路合计墙钟 {time.monotonic() - started:.1f} 秒")
        for result in results:
            print(json.dumps(result, ensure_ascii=False))
        sink = services.registry.resolve(ProposalSink)
        for result in results:
            for pid in result.get("proposal_ids", []):
                proposal = sink.get(pid)
                print("   提案", pid, proposal.expert, "轮次", proposal.binding.round, proposal.diagnosis)
        if args.ask:
            print("== 问答：", args.ask)
            actor = Actor(actor_id="dev-reviewer", roles=(Role.reviewer,))
            events = gateway.run_sync(actor, args.ask, gateway.main_agent)
            print("   回答：", "".join(e.payload["text"] for e in events if e.type == "answer_delta"))
        print("== agent_runs")
        for record in store.list(ticket_id=None):
            r = record.model_dump(mode="json")
            print(f"{r['agent_run_id']} {r['kind']:9} {r['agent_name']:26} {r['status']:9} {r['timing']['requested_at']} "
                  f"{r['timing']['latency_ms']}ms 重试{r['timing']['retries']} 提案{r['proposal_ids']}")
            for call in r["tool_calls"]:
                print(f"    {call['agent_name']} depth={call['depth']} {call['tool']} {call['business_status']} "
                      f"模型填值={call['model_params']} → 绑定值={call['bound_params']}")
            for node in r["nodes"]:
                if node["depth"]:
                    print(f"    子 Agent {node['agent_name']}：{(node['final_answer'] or '')[:120]}")
    finally:
        server.should_exit = True
        time.sleep(0.5)
        services.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
