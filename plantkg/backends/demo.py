"""python -m plantkg.backends.demo --backend fake --output runtime/b24-domain.json"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from plantkg.backends import create_judge
from plantkg.core.config import JudgeConfig
from plantkg.core.contracts import OntologyCandidate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("fake", "fallback", "jev"), default="fake")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workspace-env", default="BAILIAN_WS")
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("输出文件已存在；请选择新路径，保留已有运行记录")
    definitions = {
        "energy": "储能、光伏、电价、照明及配电削峰。",
        "elevator": "人梯货梯群控、候梯与拥堵。",
        "hvac": "区域温湿度、预冷与错峰。",
        "equipment": "空压机等设备健康、故障研判与维修。",
    }
    state = {
        "content": "08:05 行政楼早高峰电梯拥堵，车间空压机启动，总用电突增逼近变压器上限，储能 SOC 20%。",
        "gate_kind": "domain", "definitions": definitions,
        "instructions": "Identify the domain most responsible for the sudden power increase.",
    }
    fixture = {"energy": 0.2, "elevator": 0.6, "hvac": 0.05, "equipment": 0.15}
    if args.backend == "fake":
        state["fake_distributions"] = [fixture] * 3
    elif args.backend == "fallback":
        state["domain_scores"] = {key: value * 4 for key, value in fixture.items()}
    judge = create_judge(JudgeConfig(backend=args.backend, jev={"workspace_env": args.workspace_env}))
    question = OntologyCandidate(candidate_id="b24-power-surge", layer="concept", subject="event:0805",
                                 options=tuple(definitions), recall_source="manual")
    result = judge.decide(state, question, tuple(definitions))
    record = {
        "run_id": "b24-domain-smoke", "event_id": question.candidate_id,
        "source_ref": "config://b24/power-surge-demo-v1", "event_nature": "synthetic",
        "result_origin": "live" if args.backend == "jev" else "synthetic",
        "real_call_status": "performed" if args.backend == "jev" else "pending_business_acceptance",
        "result": result.model_dump(mode="json"),
    }
    text = json.dumps(record, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
