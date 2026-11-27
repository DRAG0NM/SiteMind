"""D54 真实平台演示：确定性聚类后让设备监测专家解读，生成候选规则并落库。

    python scripts/nexent_workorder_mining.py --import-agents   # 首次：先导入 Agent（主 Agent 绑 workorder_search）
    python scripts/nexent_workorder_mining.py                   # 之后只跑挖掘

前提：本机 Nexent 在跑、环境变量 NEXENT_API_KEY 有北向 Key、设备监测专家已导入知识库绑定。
脚本不改 Nexent 里的资料；候选规则没有 KnowledgePublisher 时落成 B06 的 rule_candidate 资产（derived）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 北向与本机 Nexent 都走本地地址；本机 no_proxy 里带 [::1] 会让 httpx 解析代理表时报 InvalidURL
for _key in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(_key, None)

from plantkg.bootstrap import build_services  # noqa: E402
from plantkg.core.config import load_settings  # noqa: E402
from plantkg.core.contracts import Actor, Context, Role  # noqa: E402
from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog  # noqa: E402
from plantkg.knowledge.documents import AgentInterpreter, AssetCandidateStore, load_library, mine, publish  # noqa: E402
from plantkg.storage.migration import MigrationRunner  # noqa: E402
from plantkg.storage.sqlite import SQLiteWriter  # noqa: E402

PLANT = "demo-electronics"
AS_OF = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/nexent_workorder_mining.py")
    parser.add_argument("--import-agents", action="store_true", help="先跑 import_assets（需要管理端凭据）")
    parser.add_argument("--credentials", default=None, help="管理端凭据 json（仓库外）")
    parser.add_argument("--agent", default="plantkg_expert_equipment")
    parser.add_argument("--as-of", default=AS_OF.isoformat())
    parser.add_argument("--out", default=str(ROOT / "runtime" / "b26" / "workorder_mining.json"))
    parser.add_argument("--port", type=int, default=8700)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    as_of = datetime.fromisoformat(args.as_of)

    import uvicorn

    from plantkg.api import create_app
    from plantkg.mcp_server import create_mcp_server

    settings = load_settings(profile="dev", overrides={"server.host": "0.0.0.0", "server.port": str(args.port), "nexent.enabled": "true"})
    services = build_services(settings)
    server = uvicorn.Server(uvicorn.Config(create_app(services, create_mcp_server(services)), host="0.0.0.0", port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    actor = Actor(actor_id="dev-reviewer", roles=(Role.reviewer,))
    gateway, store = services.nexent.gateway, services.nexent.store
    runs: list[dict] = []
    try:
        if args.import_agents:
            from plantkg.integrations.nexent.assets import load_assets
            from plantkg.integrations.nexent.client import AdminClient
            from plantkg.integrations.nexent.import_assets import sync
            credentials = args.credentials or (Path(os.environ["USERPROFILE"]) / ".plantkg-probe" / "tenant-dev.json")
            creds = json.loads(Path(credentials).read_text(encoding="utf-8"))
            admin = AdminClient.signin("http://127.0.0.1:5010", creds["email"], creds["password"])
            report = sync(admin, load_assets(ROOT / "nexent_assets" / "agents"), "http://host.docker.internal:8700/mcp",
                          ROOT / "nexent_assets" / "agents")
            print("import problems:", report["problems"])
            print("main tools:", report["bindings"].get("plantkg_main", {}).get("tools"))

        library = load_library(ROOT)

        def ask(prompt: str) -> str:
            events = gateway.run_sync(actor, prompt, args.agent, as_of)
            answer = "".join(event.payload["text"] for event in events if event.type == "answer_delta")
            record = store.list()[-1]
            runs.append({"agent": args.agent, "agent_run_id": record.agent_run_id, "status": record.status,
                         "terminal_reason": record.terminal_reason, "answer": answer})
            print(f"  [{args.agent}] {record.status}/{record.terminal_reason}：{answer}")
            return answer

        result = mine(library, as_of=as_of, interpreter=AgentInterpreter(ask), created_at=as_of)
        with tempfile.TemporaryDirectory(prefix="plantkg-b26-mine-") as temp:
            writer = SQLiteWriter(Path(temp) / "catalog.db")
            MigrationRunner(writer).apply()
            catalog = SQLiteAssetCatalog(writer, PLANT, Path(temp) / "blobs", base_dir=ROOT)
            context = Context(plant_id=PLANT, industry_pack="manufacturing",
                              actor=Actor(actor_id="dev-reviewer", roles=(Role.operator,)), channel="offline",
                              trace_id="t-b26-mining", as_of=as_of)
            result = publish(result, store=AssetCandidateStore(catalog, context, Path(temp) / "candidates"))
            writer.close()

        print(f"\n工单 {len(result.orders)} 张，聚成 {result.cluster_count} 类，候选规则 {result.candidate_count} 条，"
              f"落库 {result.adopted_count} 条")
        for record in result.records:
            print(f"候选 {record.candidate.candidate_id}（{record.reading.kind}，置信度 {record.reading.confidence}，"
                  f"解读 {record.reading.interpreter}）：")
            print(f"  规则：{record.reading.rule_text}")
            print(f"  根因：{record.reading.root_cause}")
            print(f"  引用工单：{'、'.join(order.order_id for order in record.citations)}")
            print(f"  候选资产：{record.candidate_asset_version_id}")
        evidence = {"created_at": datetime.now(timezone.utc).isoformat(), "as_of": as_of.isoformat(),
                    "platform": "Nexent v2.8.2 本机 Docker", "agent": args.agent, "runs": runs,
                    "result": result.summary()}
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果写入 {out}")
    finally:
        server.should_exit = True
        time.sleep(0.5)
        services.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

