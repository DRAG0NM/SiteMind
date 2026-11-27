"""python -m plantkg.knowledge.graph：按任务卡验收打印几组图谱查询。"""
from __future__ import annotations

import sys

from plantkg.knowledge.graph import GlobalGraph


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    g = GlobalGraph.load()
    print(f"全局图谱 {g.version}，{len(g.store)} 条三元组\n")

    r = g.trace_impact("AC-1", ("compressed_air",))
    print("1# 空压机供气到：", "、".join(a["code"] for a in r["affected"] if a["type"] == "production_line"))

    r = g.trace_impact("AIR-HDR")
    print("\n空压站主管网压力突降，影响区域：")
    for z in r["zones"]:
        print(f"  {z['label']}：{' → '.join(z['path'])}")
    for t in r["safety"]:
        print(f"  安全标记 {t['label']}（{t['clause']}）：{t['clause_text']}")

    r = g.find_sources("TX1", "load_surge")
    print("\n1# 变压器负荷突增，来源候选按领域：")
    for d, v in r["domains"].items():
        print(f"  {d}: {v['count']} 台，额定 {v['rated_kw']:.0f} kW")

    print("\n安全判断：")
    for target, action, occupied in (("FE-2", "stop", None), ("FE-2", "stop", False), ("TX2", "power_off", None),
                                     ("CR-AHU1", "setpoint_change", None), ("CH-A1", "precool_delay", None)):
        r = g.safety_tags(target, action, occupied)
        tags = "、".join(f"{t['label']}（{t['via']}）" for t in r["tags"]) or "无"
        print(f"  {target} {action} occupied={occupied}: {r['verdict']}；{tags}")

    print("\n优先级：")
    for zone, at in (("admin", "2026-07-01T08:05:00"), ("production", "2026-07-01T10:30:00"), ("production", "2026-07-01T23:00:00")):
        r = g.priority_rules(zone, at)
        print(f"  {zone} {at[11:16]} {r['window_label']}: {' > '.join(r['order'])}")

    pb = g.playbooks(event="elevator_stuck")[0]
    print(f"\n预案 {pb['label']}（操作工单：{pb['work_order']}）：")
    for s in pb["steps"]:
        print(f"  [{s['by']}] {s['do']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
