"""B26 真实平台演示：逐个问专家同一个问题，看它是否只引用自己库里的资料。

    python scripts/nexent_kb_demo.py --experts elevator,hvac --query "从你的知识库里查一下电梯困人救援的规程条款，引用原文回答。"
    python scripts/nexent_kb_demo.py --ask "今天峰段储能怎么放？"   # 再问一次主 Agent（会派给专家）

前提：本机 Nexent 在跑、`import_assets` 已导入带知识库绑定的 Agent、环境变量 NEXENT_API_KEY 有北向 Key。
脚本只读，不改 Nexent 里的 Agent 或知识库；每次运行打印回答与 plantkg 工具调用。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 北向与本机 Nexent 都走本地地址；本机 no_proxy 里带 [::1] 会让 httpx 解析代理表时报 InvalidURL
for _key in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(_key, None)

from plantkg.bootstrap import build_services  # noqa: E402
from plantkg.core.config import load_settings  # noqa: E402
from plantkg.core.contracts import Actor, Role  # noqa: E402

DEFAULT_QUERY = "从你的知识库里查一下电梯困人救援的规程条款，引用原文回答；一句话即可。"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/nexent_kb_demo.py")
    parser.add_argument("--experts", default="elevator,hvac")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--ask", default=None, help="额外问一次主 Agent 的问题（会派给专家）")
    parser.add_argument("--as-of", default=None, help="查看时点（ISO 时间）；缺省用配置的 default_as_of，可用来验证按 as_of 注入")
    parser.add_argument("--port", type=int, default=8700)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None

    import uvicorn

    from plantkg.api import create_app
    from plantkg.mcp_server import create_mcp_server

    settings = load_settings(profile="dev", overrides={"server.host": "0.0.0.0", "server.port": str(args.port), "nexent.enabled": "true"})
    services = build_services(settings)
    server = uvicorn.Server(uvicorn.Config(create_app(services, create_mcp_server(services)), host="0.0.0.0", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    gateway, store = services.nexent.gateway, services.nexent.store
    actor = Actor(actor_id="dev-reviewer", roles=(Role.reviewer,))
    try:
        for expert in [name.strip() for name in args.experts.split(",") if name.strip()]:
            agent = f"plantkg_expert_{expert}"
            print(f"\n== 问 {agent}：{args.query}")
            events = gateway.run_sync(actor, args.query, agent, as_of)
            answer = "".join(e.payload["text"] for e in events if e.type == "answer_delta")
            record = store.list()[-1]
            print(f"   状态 {record.status}/{record.terminal_reason}")
            print(f"   回答：{answer}")
            print(f"   plantkg 工具调用：{[(call.tool, call.business_status) for call in record.tool_calls]}")
            print(f"   run_id {record.agent_run_id}")
        if args.ask:
            print(f"\n== 问主 Agent：{args.ask}")
            events = gateway.run_sync(actor, args.ask, gateway.main_agent)
            print("   回答：" + "".join(e.payload["text"] for e in events if e.type == "answer_delta"))
            record = store.list()[-1]
            print(f"   节点：{[(node.agent_name, node.depth) for node in record.nodes]}")
        print("\n== agent_runs")
        for record in store.list():
            r = record.model_dump(mode="json")
            print(f"{r['agent_run_id']} {r['kind']:9} {r['agent_name']:26} {r['status']:9} {r['timing']['latency_ms']}ms")
    finally:
        server.should_exit = True
        time.sleep(0.5)
        services.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
