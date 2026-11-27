"""看效果（B29）：第一幕早高峰——四位专家并行研判、预演对比与一个声音。

    python -m plantkg.agent.consultation [--fresh] [--db runtime/consultation.db]

走真实默认组合：B27 值守程序 + 本卡登记的会诊钩子 + 确定性专家结果源（演示替身，synthetic），
不联网也能跑：07:50 预演在后台把专家确定性提案算好并缓存（只预判，不取用）→ 08:05 突增到达，
四路专家泳道并行现场算提案（约 0.2 s）→ 与匹配到的预演对比，打印“与预演一致” → 主 Agent 一句话汇总。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plantkg.agent.consultation import PREVIEW_STATE_HOOK, install as install_consultation
from plantkg.agent.router import install as install_router
from plantkg.agent.router.severity import SeverityEstimator
from plantkg.backends.judge import FakeJudge
from plantkg.core.contracts import (
    AgentResultKind, DayEvent, ExpertProposal, Interval, PolicyConfig, ProposalCost, ProposedAction, ResponseBudget,
)
from plantkg.core.ports import AgentResultSource
from plantkg.core.registry import Registry
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter

ROOT = Path(__file__).resolve().parents[3]
TZ = timezone(timedelta(hours=8))
PLANT = "demo-plant"
EXPERTS = ("elevator", "energy", "hvac", "equipment")
VERTICAL = {"energy": "储能放电与照明削峰", "elevator": "人梯分区分流", "hvac": "行政楼推迟预冷", "equipment": "空压机健康监测"}
SEVERITY = {"horizon_min": 60, "major_loss_cny": 50_000, "line_value_cny_per_min": 0, "stop_minutes": 30,
            "production_lines": ["LINE-1"], "stop_event_types": [], "safety_event_types": [], "equipment_damage_cny": {"default": 0}}


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 1, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def _act(entity: str, command: str, start: datetime, minutes: int, **params) -> ProposedAction:
    return ProposedAction(entity_id=f"entity://{PLANT}/{entity}", command=command, params=params,
                          interval=Interval(start=start, end=start + timedelta(minutes=minutes)))


class DemoExpert:
    """演示用确定性专家（synthetic）：睡一小段模拟模型耗时，便于看到泳道并行。"""

    source_kind = "deterministic"

    def __init__(self, domain: str, diagnosis: str, actions: list[ProposedAction], cny: float, confidence: float,
                 delay: float = 0.15) -> None:
        self.domain, self.diagnosis, self.actions = domain, diagnosis, actions
        self.cny, self.confidence, self.delay = cny, confidence, delay

    def get(self, kind, binding, payload):
        time.sleep(self.delay)
        proposal = ExpertProposal(expert=self.domain, ticket_id=binding.ticket_id, actions=tuple(self.actions),
                                  cost=ProposalCost(cny=self.cny), confidence=self.confidence, diagnosis=self.diagnosis,
                                  evidence_refs=(f"knowledge:{self.domain}_sop",))
        if kind != AgentResultKind.expert:
            raise ValueError(f"演示专家只实现 expert，不实现 {kind}")
        return {"proposal": proposal.model_dump(mode="json")}


def demo_experts() -> dict[str, DemoExpert]:
    t = at(8, 5)
    return {
        "energy": DemoExpert("energy", "总用电突增、SOC 只剩 20%：储能放电 400 kW 削峰 30 分钟，生产照明降 20%",
                             [_act("battery/BAT1", "discharge", t, 30, power_kw=400.0),
                              _act("lighting/LT-P", "shed", t, 15, ratio=0.8)], -126.0, 0.82, 0.18),
        "elevator": DemoExpert("elevator", "行政楼低区上行候梯 4.2 分钟：切分区运行，保留 2 台低区专梯",
                               [_act("elevator_group/admin_low", "set_mode", t, 45, mode="zone")], 1.6, 0.86, 0.12),
        "hvac": DemoExpert("hvac", "行政楼预冷会再抬 120 kW：推迟预冷 15 分钟，温度上浮 0.8 ℃ 仍在舒适区",
                           [_act("zone/admin", "set_mode", t, 15, mode="off")], -18.0, 0.78, 0.15),
        "equipment": DemoExpert("equipment", "空压站压力与电流同步上升，判为正常启动而非泄漏：建议观察，不开维修工单",
                                [], 0.0, 0.7, 0.10),
    }


def spike(event_id: str, when: datetime) -> DayEvent:
    return DayEvent(event_id=event_id, at=when, event_type="load_spike", source="script",
                    entity_id=f"entity://{PLANT}/production_line/LINE-1", severity=4,
                    summary="冲压线与空压站同时启动，总用电突增逼近 TX1 上限", payload={}, nature="synthetic")


def _lane_lines(lanes) -> list[str]:
    lines = []
    for lane in lanes:
        lines.append(f"  {lane.expert:<9} 泳道 ▕ {'█' * max(1, int(lane.elapsed_ms // 40) or 1):<8}▏ "
                     f"耗时 {lane.elapsed_ms:>4} ms  来源 {lane.source:<12} {lane.status}")
    return lines


def run(db_path: Path) -> dict:
    writer = SQLiteWriter(db_path)
    MigrationRunner(writer).apply()
    registry = Registry()
    registry.live_fallback = True
    try:
        policy = PolicyConfig(policy_version="demo-policy", arbitration_weights={"cny": 1.0, "wait_min": 0.5, "comfort_min": 0.8},
                              priority_profiles={"default": ("safety", "production", "comfort", "cost")},
                              response_budget=ResponseBudget(layer12_min=1, layer3_min=3), major_loss_cny=50_000)
        router = install_router(registry, writer, PLANT, run_id="run-consult-demo", knowledge_version=1, judge=FakeJudge(),
                                mode="evaluation", severity=SeverityEstimator(dict(SEVERITY)), policy=policy,
                                locator=lambda event: EXPERTS)
        consultation = install_consultation(registry, router=router)
        for domain, source in demo_experts().items():
            registry.mode_ports[(AgentResultSource, "deterministic", domain)] = source
        state = {"soc": 0.2, "transformer_ratio": 0.92}
        registry.register_hook(PREVIEW_STATE_HOOK, lambda r, at: dict(state))

        print("装配：B27 值守程序 + B29 会诊；专家为演示用确定性替身（synthetic），不联网、不跑 LLM")
        print(f"      策略 {policy.policy_version}：预演窗口 {policy.preview_horizon_min} 分钟，"
              f"SOC<{policy.preview_soc_low:.0%} 或 TX1 负载>{policy.preview_transformer_high:.0%} 才预演\n")

        print("=== 07:50 预演（值守循环的每步钩子，后台算、不裁决、不执行、不对人说话）===")
        router.step(at(7, 50))
        ready = consultation.wait_preview()
        print(f"  预见 08:00 早高峰 load_spike；储能 SOC {state['soc']:.0%}、TX1 负载 {state['transformer_ratio']:.0%}"
              f" → 命中规则，后台{'已算好专家确定性提案' if ready else '还在算'}")
        print("  预演单已建并按状态指纹缓存（SOC/TX1），等真实事件；演示里不打扰人\n")

        print("=== 08:05 突增到达：值守程序派给四位专家，四条泳道并行现场计算 ===")
        first = router.handle_event(spike("EV-0805", at(8, 5)))
        lanes = consultation.last_lanes
        check = json.loads(router.store.ticket_state(first.ticket_id, "preview_check_1") or "{}")
        if check.get("matched") and check.get("same_actions"):
            print("  [预演对比] 与预演一致")
        elif check.get("matched"):
            print(f"  [预演对比] 预演偏差：{'、'.join(check.get('diff') or ())}")
        else:
            print("  [预演对比] 没有匹配的预演")
        for line in _lane_lines(lanes):
            print(line)
        proposals = router.sink.list(first.ticket_id, 1)
        print("  结构化报告：")
        for proposal in proposals:
            actions = "，".join(f"{a.command} {a.entity_id.rsplit('/', 1)[-1]}" for a in proposal.actions) or "无动作"
            print(f"    {proposal.expert:<9} {proposal.diagnosis[:60]}｜{actions}｜置信度 {proposal.confidence:.2f}｜"
                  f"代价 {proposal.cost.cny:+.1f} 元")
        print(f"  事件单 {first.ticket_id} 第 1 轮：{len(proposals)} 份提案已落库，等待裁决（B28 未接入，演示只到提案就绪）")

        batch = first.batch or router.flush()
        print("\n=== 主 Agent 一句话（全系统只有它对人说话）===")
        print(f"  {batch.final_answer if batch else '（没有待汇报结论）'}")
        return {"tickets": [first.ticket_id],
                "preview_check": {"matched": bool(check.get("matched")),
                                  "same_actions": bool(check.get("same_actions"))},
                "proposals": len(proposals)}
    finally:
        writer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.agent.consultation")
    parser.add_argument("--db", default=str(ROOT / "runtime" / "consultation.db"))
    parser.add_argument("--fresh", action="store_true", help="先删掉本命令自己的演示库再跑")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and db.is_file():
        db.unlink()
    run(db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
