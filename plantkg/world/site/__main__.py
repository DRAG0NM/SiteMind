"""看效果：python -m plantkg.world.site [--d05 小样本或星型表目录] [--days 7]

打印设备清单与配电路径、首日逐小时人流与分区负荷、变压器峰值对容量上限、D05 底座取用统计。
"""
import argparse
from pathlib import Path
from zoneinfo import ZoneInfo

from .loads import synthesize
from .model import SiteModel
from .sources import d05

ROOT = Path(__file__).resolve().parents[3]


def _bar(v, top, width=24):
    return "█" * round(width * v / top) if top else ""


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--site", default=ROOT / "configs/sites/factory/site.yaml")
    p.add_argument("--d05", default=ROOT / "fixtures/sites/factory/d05_week.parquet")
    p.add_argument("--days", type=int, default=7)
    a = p.parse_args(argv)
    site = SiteModel.load(a.site)
    loads = synthesize(site, d05.load_d05(a.d05), a.days)

    print(f"# {site.site_name}：{len(site.equipment)} 台设备\n")
    print("编号 | 设备 | 区域 | 楼层 | 额定 kW | 负荷来源 | 配电路径")
    for r in site.table():
        print(f"{r['code']} | {r['label']} | {r['zone']} | {r['floor'] or '-'} | {r['rated_kw']:g} | {r['load']} | {r['path']}")

    tz = ZoneInfo(site.plant_tz)
    day = [i for i, t in enumerate(loads.slots) if t.astimezone(tz).date() == loads.slots[0].astimezone(tz).date()]
    people = loads.series["occupancy:admin"]
    top = max(people[i] for i in day)
    print(f"\n# 首日 {loads.slots[0].astimezone(tz):%Y-%m-%d %a} 逐小时（厂区当地时间）")
    print(f"{'时':>3} {'行政楼人数':>8}  {'':<24} {'人梯次/刻':>8} {'全厂kW':>8} {'生产区':>7} {'行政楼':>7} {'特殊区':>7}")
    for h in range(24):
        i = day[h * 4]
        special = sum(loads.series[f"zone_total:{z}"][i] for z in ("cleanroom", "hazmat", "datacenter"))
        print(f"{h:>3} {people[i]:>8.0f}  {_bar(people[i], top):<24} {loads.series['trips:passenger_elevator'][i]:>8.1f} "
              f"{loads.total(i):>8.0f} {loads.series['zone_total:production'][i]:>7.0f} "
              f"{loads.series['zone_total:admin'][i]:>7.0f} {special:>7.0f}")

    print("\n# 变压器峰值 / 容量上限（kW）")
    for tx in (n for n, node in site.nodes.items() if node.type == "transformer"):
        vals = [v for v in loads.series[f"transformer_load:{tx}"] if v is not None]
        print(f"{tx}: {max(vals):.0f} / {site.transformer_limit_kw(tx):.0f}")

    used = [x["decomposition"] for x in loads.base_slots if x["decomposition"] and x["decomposition"]["total"] is not None]
    lifted = sum(1 for d in used if d["basis"] == "leaf_sum")
    print(f"\n# D05 底座：{len(used)}/{len(loads.slots)} 个时段可用；{lifted} 个时段叶子合计高于 mi_a 净购电（对方厂光伏），取叶子合计")
    print(f"物料搬运任务 {len(loads.tasks)} 条；记录 {len(loads.to_bundle().records)} 条（B01 SourceRecord）")


if __name__ == "__main__":
    main()
