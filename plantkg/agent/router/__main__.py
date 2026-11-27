"""看效果（B27 值守层）：

    python -m plantkg.agent.router [--fresh] [--db runtime/router.db] [--json]

走真实默认装配（build_services + configs/defaults.yaml 的 packs）：图谱、观测层、试算台与策略都经注册表接入，
只有判别（演示门控不一致）与会诊（B29 还没接）用替身。
演示：噪声过滤 → 未知类型进慢通道 → 第二幕两起故障走快通道 → 拿不准的事件走慢通道 → 订阅直通 →
未保护试算判跳闸停产立即出工单 → 值班指令 → 带安全标记的事件立即出工单。事件、专家提案与调查结论都是 synthetic。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plantkg.agent.router.router import INSTRUCTION_HOOK, ROUTER_HOOK, URGENT_HOOK
from plantkg.agent.router.standin import SyntheticConsilium
from plantkg.backends.judge import FakeJudge
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import (
    DayEvent, ExpertProposal, Interval, JudgeResult, OrderResult, ProposalCost, ProposedAction, RunBinding, TopK,
)

ROOT = Path(__file__).resolve().parents[3]
TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 7, 1, 2, 30, tzinfo=timezone.utc)  # 换算成厂区当地时间 10:30；契约要求 UTC
PLANT = "demo-electronics"
IDS = {
    "air_system": f"entity://{PLANT}/air_compressor/AC-2",
    "lighting": f"entity://{PLANT}/lighting/LT-A03",
    "zone_admin": f"entity://{PLANT}/zone/admin",
    "battery": f"entity://{PLANT}/battery/BAT1",
    "elevator": f"entity://{PLANT}/elevator_group/admin_low",
}


def _act(entity: str, command: str, start: datetime, minutes: int, **params) -> ProposedAction:
    return ProposedAction(entity_id=entity, command=command, params=params,
                          interval=Interval(start=start, end=start + timedelta(minutes=minutes)))


class DemoJudge:
    """演示用判别：FakeJudge 之外，对 voltage_sag 给一组三种顺序互相矛盾的分布（synthetic），
    用来演示门控未放行并入慢通道。"""

    backend = "fake"
    model = "demo-inconsistent-v1"

    def __init__(self) -> None:
        self.inner = FakeJudge()

    def decide(self, state, question, options):
        if "voltage_sag" not in str(state.get("content", "")):
            return self.inner.decide(state, question, options)
        energy, elevator, hvac, equipment = "energy", "elevator", "hvac", "equipment"
        orders = (
            OrderResult(order=(energy, elevator, hvac, equipment), choice=energy,
                        distribution={energy: 0.6, elevator: 0.2, hvac: 0.15, equipment: 0.05}),
            OrderResult(order=(elevator, hvac, equipment, energy), choice=elevator,
                        distribution={elevator: 0.55, hvac: 0.25, equipment: 0.15, energy: 0.05}),
            OrderResult(order=(equipment, hvac, elevator, energy), choice=equipment,
                        distribution={equipment: 0.45, hvac: 0.3, elevator: 0.2, energy: 0.05}),
        )
        distribution = {energy: 0.233, elevator: 0.284, hvac: 0.233, equipment: 0.25}
        return JudgeResult(candidate_id=question.candidate_id, choice=elevator, score=distribution[elevator],
                           coverage=1.0, backend=self.backend, model=self.model, orders=orders, order_agree=False,
                           candidate_recall_source="manual", gate_accept=False, gate_kind="domain", distribution=distribution,
                           top_k=tuple(TopK(option=o, prob=p) for o, p in sorted(distribution.items(), key=lambda kv: -kv[1])[:3]))


def _experts() -> dict:
    """四位确定性专家（synthetic）：正式实现由 B19～B22 交付，这里只为把派发与提案跑通。"""

    def equipment(event: DayEvent, b: RunBinding) -> ExpertProposal:
        if event.event_type == "compressor_pressure_drop":
            target = event.entity_id or IDS["air_system"]
            return ExpertProposal(expert="equipment", ticket_id=b.ticket_id, cost=ProposalCost(cny=-3.0), confidence=0.9,
                                  actions=(_act(target, "isolate_leak", b.trigger_at, 1),),
                                  diagnosis="压力下降速率超过阈值，按维护手册判为泄漏，隔离泄漏段并开维修工单",
                                  evidence_refs=("knowledge:compressor_leak_rule",))
        return ExpertProposal(expert="equipment", ticket_id=b.ticket_id, cost=ProposalCost(cny=0.0), confidence=0.5,
                              diagnosis="设备监测：待补充判据")

    def energy(event: DayEvent, b: RunBinding) -> ExpertProposal:
        if event.event_type == "lighting_fault":
            return ExpertProposal(expert="energy", ticket_id=b.ticket_id, cost=ProposalCost(cny=0.0), confidence=0.92,
                                  actions=(_act(IDS["lighting"], "switch_circuit", b.trigger_at, 1),),
                                  diagnosis="照明回路跳闸，切到备用回路")
        return ExpertProposal(expert="energy", ticket_id=b.ticket_id, cost=ProposalCost(cny=-120.0), confidence=0.8,
                              actions=(_act(IDS["battery"], "discharge", b.trigger_at, 30, power_kw=400.0),),
                              diagnosis="储能放电 400 kW 削峰 30 分钟")

    def elevator(event: DayEvent, b: RunBinding) -> ExpertProposal:
        return ExpertProposal(expert="elevator", ticket_id=b.ticket_id, cost=ProposalCost(cny=0.5), confidence=0.85,
                              actions=(_act(IDS["elevator"], "set_mode", b.trigger_at, 45, mode="zone"),),
                              diagnosis="早高峰上行拥堵，切分区运行")

    def hvac(event: DayEvent, b: RunBinding) -> ExpertProposal:
        return ExpertProposal(expert="hvac", ticket_id=b.ticket_id, cost=ProposalCost(cny=-5.0, comfort_min=8.0), confidence=0.75,
                              actions=(_act(IDS["zone_admin"], "set_mode", b.trigger_at, 15, mode="off"),),
                              diagnosis="行政楼推迟预冷 15 分钟让出容量")

    return {"equipment": equipment, "energy": energy, "elevator": elevator, "hvac": hvac}


def _event(at: datetime, eid: str, kind: str, entity: str | None, severity: int, summary: str, source: str = "script",
           payload: dict | None = None) -> DayEvent:
    return DayEvent(event_id=eid, at=at, event_type=kind, source=source, entity_id=entity, severity=severity,
                    summary=summary, payload=payload or {}, nature="synthetic")


def _fmt_domains(domains: dict[str, float]) -> str:
    return " / ".join(f"{k} {v:.0%}" for k, v in sorted(domains.items(), key=lambda kv: (-kv[1], kv[0])))


def _print_handled(router, handled, title: str) -> None:
    local = lambda iso: datetime.fromisoformat(iso).astimezone(TZ).strftime("%H:%M")  # noqa: E731
    if handled.result.startswith("dropped"):
        print(f"{title} [准入] 丢弃（{handled.result}）")
        return
    if handled.result == "urgent":
        print(f"{title} [严重程度] 立即出操作工单（{handled.channel}）：不等调查与裁决")
        return
    if handled.result == "instruction":
        return
    triage = router.store.triage(handled.ticket_id, 1) if handled.ticket_id else None
    if handled.result == "straight_through":
        print(f"{title} [直通] 订阅命中 → {handled.ticket_id}（不调用 Jev、不调用主 Agent）")
        if handled.experts:
            print(f"       专家不在跑且严重度过阈值 → 第 {router.store.ticket(handled.ticket_id).round} 轮重派 {', '.join(handled.experts)}")
        return
    if handled.channel == "slow":
        investigation = router.store.investigation(handled.ticket_id, 1) or {}
        print(f"{title} [慢通道] 主 Agent 调查（{investigation.get('source', '-')}，覆盖不足 {investigation.get('coverage_gap')}）："
              f"{investigation.get('diagnosis', '')}")
        print(f"       指定专家 → 第 {router.store.ticket(handled.ticket_id).round} 轮派给 {', '.join(handled.experts) or '无'}")
    else:
        if triage:
            gate = "门控一致" if triage["gate_accept"] else "门控未放行"
            print(f"{title} [研判] Jev 派域 {_fmt_domains(triage['domains'])}（{gate}）→ 图谱定位归属 {', '.join(handled.experts) or '无'}")
        print(f"{title} [开单] {handled.ticket_id}，第 {router.store.ticket(handled.ticket_id).round} 轮 → 派给 {', '.join(handled.experts) or '无'}")
    for proposal in router.sink.list(handled.ticket_id):
        actions = "，".join(f"{a.command} {a.entity_id.rsplit('/', 1)[-1]} {local(a.interval.start.isoformat())}" for a in proposal.actions)
        print(f"       [提案] {proposal.proposal_id} {proposal.expert} 置信度 {proposal.confidence:.2f}"
              f"（{actions or '无动作'}）{proposal.diagnosis or ''}")


def _store_dir(db_path: Path) -> Path:
    return db_path.parent / f"{db_path.stem}_store"


def run(db_path: Path, as_json: bool = False) -> dict:
    store = _store_dir(db_path)
    settings = load_settings(overrides={"storage.enabled": True, "storage.sqlite": str(db_path), "storage.graph": str(store / "graph"),
                                        "storage.series": str(store / "series")})
    services = build_services(settings)
    try:
        registry = services.registry
        router = registry.hooks_for(ROUTER_HOOK)[-1]  # 默认装配出来的值守程序
        router.judge = DemoJudge()
        router.standin = SyntheticConsilium(_experts())
        print(f"装配：packs={settings.packs}")
        print(f"      策略 {router.policy.policy_version}，重大损失线 {router.policy.major_loss_cny:.0f} 元；"
              f"钩子 {sorted(h for h in registry.hooks if h.startswith('router.'))}")

        def instruction_handler(event: DayEvent) -> None:
            print(f"12:30 [值班指令] 「{event.summary}」→ 临时规则处理器（B47 占位）登记 13:00～17:00 的临时规则，不用 B28")

        def urgent_handler(event: DayEvent, reason: str) -> None:
            print(f"{event.at.astimezone(TZ).strftime('%H:%M')} [工单] {event.summary} → 立即派单：{reason}")

        registry.register_hook(INSTRUCTION_HOOK, instruction_handler)
        registry.register_hook(URGENT_HOOK, urgent_handler)

        print("=== 第一幕片段：噪声过滤与从未见过的事件 ===")
        _print_handled(router, router.handle_event(_event(T0 - timedelta(minutes=5), "EV-NOISE", "sensor_noise", None, 0,
                                                         "传感器抖动")), "10:25")
        _print_handled(router, router.handle_event(_event(T0 - timedelta(minutes=4), "EV-EXT", "vendor_alert", None, 4,
                                                         "厂商门户推送的未知告警类型")), "10:26")  # D53：从未见过的事件走慢通道

        print("\n=== 第二幕：两起互不相关的故障（10:30）===")
        leak = _event(T0, "EV-1001", "compressor_pressure_drop", IDS["air_system"], 3, "空压站压力异常下降")
        light = _event(T0, "EV-1002", "lighting_fault", IDS["lighting"], 2, "行政楼 3 层照明回路故障")
        first = router.handle_event(leak, flush=False)
        second = router.handle_event(light, flush=False)
        _print_handled(router, first, "10:30")
        _print_handled(router, second, "10:30")
        batch = router.flush()
        if batch:
            print(f"\n[主 Agent] 一批 {len(batch.ticket_ids)} 张事件单的研判与汇报合成一次运行（{batch.source}，{batch.agent_run_id}）")
            print(f"           {batch.final_answer}")

        print("\n=== 慢通道（10:31）：无规则覆盖的电压暂降 ===")
        sag = _event(T0 + timedelta(minutes=1), "EV-1004", "voltage_sag", IDS["air_system"], 3,
                     "车间电压暂降，空压机同时异常，领域拿不准")
        _print_handled(router, router.handle_event(sag), "10:31")

        print("\n=== 同一设备的后续事件（10:36）：订阅直通 ===")
        follow = _event(T0 + timedelta(minutes=6), "EV-1003", "compressor_pressure_drop", IDS["air_system"], 3,
                        "空压站压力继续下降")
        _print_handled(router, router.handle_event(follow), "10:36")

        print("\n=== 慢通道（10:40）：无匹配规则的怪事件，主 Agent 调查后指定专家 ===")
        weird = _event(T0 + timedelta(minutes=10), "EV-1005", "unknown_combo", IDS["lighting"], 3,
                       "车间照明闪烁与 1# 空压机异常同时出现")
        _print_handled(router, router.handle_event(weird), "10:40")

        print("\n=== 传感器说谎（10:45）：压力读数可疑，命中规则也走慢通道 ===")
        suspicious = _event(T0 + timedelta(minutes=15), "EV-1007", "compressor_pressure_drop",
                            f"entity://{PLANT}/air_compressor/AC-1", 3,
                            "1# 空压机压力读数异常下降（残差报警，读数质量可疑）")
        suspicious = suspicious.model_copy(update={"payload": {"reading": {"sensor_id": "pressure", "quality": "suspect",
                                                                          "residual_alarm": True, "value": 0.31}}})
        handled = router.handle_event(suspicious)
        _print_handled(router, handled, "10:45")
        estimate = router.store.severity_of(handled.ticket_id) if handled.ticket_id else None
        if estimate:
            print(f"       [严重程度] 读数可疑 → 不按停产派单；basis：{estimate['basis']}")

        print("\n=== 未保护试算（11:00）：突增 800 kW，TX1 连续超容判跳闸，图谱下游产线与空压站停产 ===")
        surge = _event(T0 + timedelta(minutes=30), "EV-1008", "load_spike", f"entity://{PLANT}/production_line/LINE-1", 4,
                       "冲压线与空压站同时启动，总用电突增",
                       payload={"effects": [{"kind": "extra_load", "kw": 800, "minutes": 40}]})
        handled = router.handle_event(surge)
        _print_handled(router, handled, "11:00")
        print(f"       [严重程度] {router.store.severity_of(handled.ticket_id)['basis']}")

        print("\n=== 值班人员指令（12:30）===")
        instruction = _event(T0 + timedelta(hours=2), "EV-2001", "operator_instruction", None, 2,
                             "下午有领导参观，行政楼保持 24 ℃", source="operator_message")
        _print_handled(router, router.handle_event(instruction), "12:30")

        print("\n=== 图谱安全标记命中（13:40）：危化品仓库防爆排风机，事件类型本身不在安全清单 ===")
        fan = _event(T0 + timedelta(hours=3, minutes=10), "EV-1009", "equipment_fault", f"entity://{PLANT}/exhaust_fan/HZ-EXF1", 3,
                     "危化品仓库防爆排风机振动异常")
        _print_handled(router, router.handle_event(fan), "13:40")

        print("\n=== 带安全标记的事件（14:20）：不经 Jev 与主 Agent，立即出工单 ===")
        trapped = _event(T0 + timedelta(hours=3, minutes=50), "EV-1006", "elevator_trapped",
                         f"entity://{PLANT}/freight_elevator/FE-2", 5, "2 号货梯停在两层之间，轿厢内有人")
        _print_handled(router, router.handle_event(trapped), "14:20")

        print("\n=== 召回（plantkg_recall）===")
        fast_ticket = first.ticket_id
        recall = router.recall(_context(router), fast_ticket)
        print(json.dumps(recall.data, ensure_ascii=False, indent=1))
        proposal_id = router.sink.list(fast_ticket)[0].proposal_id
        expanded = router.recall(_context(router), proposal_id)
        print(f"{proposal_id} 展开：{expanded.data['summary']}（下一跳 {expanded.data['items'][0]['next']}）")

        return {"events": len(router.store.events_of(fast_ticket)) + 1,
                "tickets": [t.ticket_id for t in router.store.open_tickets()],
                "batches": [run.agent_run_id for run in router.runs]}
    finally:
        services.close()


def _context(router):
    from plantkg.core.contracts import Actor, Context

    return Context(plant_id=router.plant_id, industry_pack="manufacturing", actor=Actor(actor_id="duty-officer", roles=("operator",)),
                   channel="offline", trace_id="router-demo", as_of=T0, run_id=router.run_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.agent.router")
    parser.add_argument("--db", default=str(ROOT / "runtime" / "router.db"))
    parser.add_argument("--fresh", action="store_true", help="先删掉本命令自己的演示库再跑（只删 --db 指定的文件与同名 _store 目录）")
    parser.add_argument("--json", action="store_true", help="只打印汇总 JSON")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and db.is_file():
        db.unlink()
        shutil.rmtree(_store_dir(db), ignore_errors=True)
    report = run(db, as_json=args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
