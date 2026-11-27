"""看效果：

    python -m plantkg.world.scenario run configs/scenarios/factory_demo_day.yaml [--mode evaluation|live|trial] [--group independent|coordinated] [--json] [--out 路径]
    python -m plantkg.world.scenario export-day [--run RUN_ID] [--date 2026-07-01] [--out runtime/day.json]

run 在终端打印一天的事件、动作、事件单、逐小时状态与账本摘要，并把同样内容（JSON）写到 --out。
数据库默认 runtime/scenario.db（已在 .gitignore）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from plantkg.core.contracts import AssetVersion, Context, ContextChannel, DayMetric, EvidenceEvent, Interval, PlantkgError
from plantkg.decision.planner.rules import priced_rules
from plantkg.packs.tariff import load_tariff_config, load_tariff_rules, price_schedule
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter

from .export import chain, day_window, export_day, latest_scenario_run
from .inputs import SiteInputProvider
from .runner import ScenarioDB, ScenarioRunner, ScenarioWorld, default_registry
from .script import ROOT, Scenario, load_scenario

DEFAULT_DB = ROOT / "runtime" / "scenario.db"


# ---- 装配（本模块是命令行入口，属装配层：在这里接电价包与 SQLite，平面内代码只收注入）


def tariff_prices(scenario: Scenario):
    """B12 分时电价：规则 + 计价基础 → 逐 15 分钟时段 (元/kWh, 时段名)。"""
    rules_path = scenario.resolve(scenario.sources.tariff_rules)
    rules = load_tariff_rules(rules_path, "scenario-tariff-rules")
    pack, demo = load_tariff_config(scenario.resolve(scenario.sources.tariff_config), "scenario-tariff")

    def prices(slots):
        if not slots:
            return []
        horizon = Interval(start=slots[0].start, end=slots[-1].end)
        return [(p.price_cny_per_kwh, p.period) for p in price_schedule(priced_rules(rules, scenario.start, horizon),
                                                                       pack.base_prices, slots)]
    return prices, {"source": f"tariff:{rules_path.stem}", "demo_base_price": demo}


def build_world(scenario: Scenario, simulators=None) -> ScenarioWorld:
    prices, note = tariff_prices(scenario)
    return ScenarioWorld.build(scenario, SiteInputProvider(scenario, prices, price_note=note), simulators)


def open_db(path, plant_id: str) -> ScenarioDB:
    writer = SQLiteWriter(Path(path))
    MigrationRunner(writer).apply()
    return ScenarioDB.from_writer(writer, plant_id)


def open_chain_db(path, run_id: str) -> ScenarioDB:
    """按库内 plant_id 打开场景库（`validate --chain` 等不给厂区编号的入口用）。

    只读入口：库不存在直接报错、不新建；先用只读连接确认是场景库，再打开 writer，不跑迁移、不建表。
    """
    import sqlite3

    db_path = Path(path)
    if not db_path.is_file():
        raise SystemExit(f"库 {db_path} 不存在")
    try:
        con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise SystemExit(f"库 {db_path} 打不开：{exc}") from None
    try:
        row = con.execute("SELECT plant_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    except sqlite3.Error as exc:
        con.close()
        raise SystemExit(f"库 {db_path} 不是场景库或缺少运行表：{exc}") from None
    con.close()
    if row is None:
        raise SystemExit(f"运行 {run_id} 不在库 {db_path} 里")
    return ScenarioDB.from_writer(SQLiteWriter(db_path), row[0])


def read_context(db: ScenarioDB, as_of) -> Context:
    """只读导出用的上下文：通道 offline，不冒充人工；as_of 取当日结束。"""
    from plantkg.core.config import load_settings

    return Context(plant_id=db.plant_id, industry_pack=load_settings().industry_pack, actor=None,
                   channel=ContextChannel.offline, trace_id="export-day", as_of=as_of)


def chain_evidence(db: ScenarioDB, runs, as_of) -> tuple[EvidenceEvent, ...]:
    """从同库证据流按运行链读该截止时刻可见的证据（#238 方案 6）。"""
    from plantkg.core.evidence import SqliteEvidenceSink

    sink = SqliteEvidenceSink(db.writer, db.plant_id)
    context = read_context(db, as_of)
    out = []
    for r in runs:
        cursor = None
        while True:
            page = sink.list(r.run_id, cursor, context, page_size=200)
            out.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
    return tuple(out)


def chain_assets(db: ScenarioDB, evidence, as_of) -> tuple[AssetVersion, ...]:
    """只取被证据 sources 引用的资料版本；不可见或不存在的不带，由 `validate --chain` 报悬空。"""
    referenced = sorted({ref.asset_version_id for event in evidence for ref in event.sources if ref.asset_version_id})
    if not referenced:
        return ()
    from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog

    catalog = SQLiteAssetCatalog(db.writer, db.plant_id, db.writer.path.parent / "asset_blobs", base_dir=ROOT)
    context = read_context(db, as_of)
    out = []
    for asset_id in referenced:
        try:
            out.append(catalog.get(context, asset_id))
        except PlantkgError:  # 不可见或不存在：不导出，链校验会指出证据引用悬空
            continue
    return tuple(out)


def day_exports(db: ScenarioDB, runs, as_of) -> tuple[tuple[EvidenceEvent, ...], tuple[AssetVersion, ...]]:
    """某日导出用的证据与资产（validate --chain 与 export-day 共用）。"""
    evidence = chain_evidence(db, runs, as_of)
    return evidence, chain_assets(db, evidence, as_of)


def catalog_metrics_provider(db: ScenarioDB):
    """Reuse the scenario database writer; no second writer or blob access is needed."""
    from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog
    from plantkg.knowledge.assets.day_metrics import asset_metrics_provider

    catalog = SQLiteAssetCatalog(db.writer, db.plant_id, db.writer.path.parent / "asset_blobs", base_dir=ROOT)
    return asset_metrics_provider(catalog)


def read_metrics(paths) -> tuple[DayMetric, ...]:
    """读 B33 `metrics.json`：顶层 dict 的 `metrics` 列表，也接受裸列表（可重复传多个文件）。"""
    out = []
    for path in paths or ():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = data.get("metrics") if isinstance(data, dict) else data
        for item in entries or ():
            out.append(DayMetric.model_validate({k: item[k] for k in ("metric", "group", "value", "unit", "note") if k in item}))
    return tuple(out)


def _short(eid: str | None) -> str:
    return eid.split("/", 3)[-1] if eid and eid.startswith("entity://") else (eid or "-")


def trace_json(trace, scenario) -> dict:
    tz = ZoneInfo(scenario.site_tz)
    hourly = []
    for t, s in trace.states:
        if t.minute == 0:
            hourly.append({"at": t.astimezone(tz).isoformat(), "devices": {k: v for k, v in s.items() if "/zone/" in k or "/elevator_group/" in k
                                                                            or "/battery/" in k or "/air_system/" in k}})
    return {"summary": trace.summary(), "events": [e.model_dump(mode="json") for e in trace.events], "actions": trace.actions,
            "decisions": [d.model_dump(mode="json") for d in trace.decisions],
            "tickets": trace.tickets, "work_orders": {k: v.model_dump(mode="json") for k, v in trace.work_orders.items()},
            "temp_rules": {k: v.model_dump(mode="json") for k, v in trace.temp_rules.items()}, "notes": trace.notes,
            "state_hourly": hourly, "slots": [s.model_dump(mode="json") for s in trace.slots]}


def print_trace(trace, scenario, world) -> None:
    tz = ZoneInfo(scenario.site_tz)
    loc = lambda t: t.astimezone(tz).strftime("%m-%d %H:%M")  # noqa: E731
    chain = " → ".join(f"{r.run_id}(v{r.knowledge_version}{'，' + loc(r.forked_at) + ' 分叉' if r.forked_at else ''})" for r in trace.runs)
    print(f"# {scenario.title}（{scenario.scenario_id}，数据性质 {scenario.nature}）")
    print(f"运行链：{chain}\n")
    print("## 事件")
    for e in trace.events:
        if e.severity == 0:
            continue
        print(f"{loc(e.at)}  {e.event_type:<24} {_short(e.entity_id):<28} {e.summary}")
    noise = sum(1 for e in trace.events if e.severity == 0)
    if noise:
        print(f"（另有 {noise} 条噪声告警，程序准入丢弃）")
    print("\n## 动作（apply 到仿真）")
    for a in trace.actions:
        act, src = a["action"], a["source"]
        start = act["interval"]["start"]
        st = datetime.fromisoformat(start)
        who = src.get("expert") or ("临时规则" if src.get("temp_rule") else "-")
        params = {k: v for k, v in act["params"].items() if k != "temp_rule"}
        print(f"{loc(st)}  {_short(act['entity_id']):<28} {act['command']:<14} {json.dumps(params, ensure_ascii=False):<26} ← {src.get('ticket_id', '')} {who}")
    print("\n## 事件单与工单")
    for tid, t in trace.tickets.items():
        print(f"{tid}  {t['status']:<9} 第 {t['round']} 轮  专家 {','.join(t['experts'])}  {t['summary']}")
    for wid, w in trace.work_orders.items():
        print(f"{wid}  工单 {w.status:<9} {w.reason}")
    for rid, r in trace.temp_rules.items():
        print(f"{rid}  临时规则 {loc(r.effective.start)}～{loc(r.effective.end)} {r.text}（试算代价 {r.estimated_cost_cny} 元）")
    kinds = {}
    for n in trace.notes:
        kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
    if kinds:
        print("记录：" + "，".join(f"{k} {v}" for k, v in kinds.items()))
    print("\n## 逐小时状态（厂区当地时间）")
    ids = world.ids()
    print(f"{'时刻':<12}{'SOC':>6}{'行政楼℃':>8}{'洁净℃':>7}{'低区排队':>8}{'空压bar':>8}{'购电kW':>8}{'光伏kW':>7}")
    minute = {m.entry.interval.start: m for m in trace.ledger}
    for t, s in trace.states:
        if t.minute != 0:
            continue
        m = minute[t]
        print(f"{loc(t):<12}{s[ids['battery']]['soc']:>6.2f}{s[ids['zone:admin']]['temperature']:>8.1f}{s[ids['zone:cleanroom']]['temperature']:>7.1f}"
              f"{s[ids['admin_low']]['queue']:>8.0f}{s[ids['air_system']]['pressure_bar']:>8.2f}{m.import_kw:>8.0f}{m.entry.pv_used_kwh * 60:>7.0f}")
    print("\n## 账本摘要")
    print(json.dumps(trace.ledger_summary(), ensure_ascii=False, indent=1))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m plantkg.world.scenario")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="跑完一个剧本")
    r.add_argument("scenario")
    r.add_argument("--db", default=str(DEFAULT_DB))
    r.add_argument("--mode", default="evaluation", choices=["evaluation", "trial", "live"])
    r.add_argument("--group", default="independent", choices=["independent", "coordinated"], help="独立组走直派器，协同组走值守层")
    r.add_argument("--until", default=None, help="剧本时刻写法，如 '12:00' 或 '+1 00:00'")
    r.add_argument("--out", default=None, help="JSON 输出路径，默认 runtime/<scenario_id>.json")
    r.add_argument("--json", action="store_true", help="终端打印 JSON 而不是表格")
    e = sub.add_parser("export-day", help="按 day.schema.json 导出一天")
    e.add_argument("--db", default=str(DEFAULT_DB))
    e.add_argument("--run", default=None, help="链末运行 ID，默认最近一次场景运行")
    e.add_argument("--date", default=None, help="厂区当地日期，默认运行首日")
    e.add_argument("--out", default=str(ROOT / "runtime" / "day.json"))
    e.add_argument("--metrics", action="append", default=None, metavar="FILE",
                   help="B33 metrics.json（顶层 metrics 列表或裸列表），可重复；缺省 metrics 0 条")
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "run" and a.mode == "live" and a.group == "coordinated":
        from scripts.nexent_live_scenario import main as run_live

        live_args = [a.scenario, "--until", a.until or "end", "--db", a.db,
                     "--out", a.out or str(ROOT / "runtime" / f"{load_scenario(a.scenario).scenario_id}.json")]
        if a.json:
            live_args.append("--json")
        return run_live(live_args)
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    if a.cmd == "run":
        scenario = load_scenario(a.scenario)
        world = build_world(scenario)
        db = open_db(a.db, world.site.plant_id)
        try:
            from plantkg.experts.energy.minute import install_minute_layer  # 装配层：B19 分钟层接进 CLI（#188）

            runner = ScenarioRunner(world, db, mode=a.mode, group=a.group)
            runner.start()
            install_minute_layer(runner)  # 分钟层接在事件注入后、本分钟结算前；CLI 是权威入口
            trace = runner.run_until(scenario.instant(a.until) if a.until else None)
            data = trace_json(trace, scenario)
            out = Path(a.out) if a.out else ROOT / "runtime" / f"{scenario.scenario_id}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            if a.json:
                print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
            else:
                print_trace(trace, scenario, world)
                print(f"\nJSON 已写到 {out}")
        finally:
            db.close()
    else:
        from plantkg.world.site.model import SiteModel
        site = SiteModel.load(ROOT / "configs/sites/factory/site.yaml")
        db = open_db(a.db, site.plant_id)
        try:
            run_id = a.run or latest_scenario_run(db)
            runs = chain(db, run_id)
            as_of = day_window(runs, a.date)[2]  # 证据、资产的可见截止 = 当日结束
            metrics = read_metrics(a.metrics)
            evidence, assets = day_exports(db, runs, as_of)
            record = export_day(db, run_id, a.date, metrics=metrics, evidence=evidence, assets=assets,
                                registry=default_registry(), site=site, asset_metrics_provider=catalog_metrics_provider(db))
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(record.model_dump_json(indent=1), encoding="utf-8")
            configured = sum(1 for m in record.metrics if m.group == "config")
            print(f"{a.out}：{len(record.runs)} 个运行、{len(record.events)} 条事件、{len(record.proposals)} 条提案、"
                  f"{len(record.agent_runs)} 条 Agent 运行、{len(record.qa)} 条问答、"
                  f"{len(record.state_series)} 帧状态、{len(record.ledger_minutes)} 条分钟账本、"
                  f"{len(record.metrics)} 条指标（config {configured} 条）、{len(record.evidence)} 条证据、"
                  f"{len(record.assets)} 个资产（{record.local_date}）")
            if not metrics:
                print("metrics 0 条（未传 --metrics）" if not a.metrics else "metrics 0 条（--metrics 文件没有可用条目）")
            if not record.evidence:
                print("evidence 0 条（库内无证据）")
            if not record.assets:
                print("assets 0 个（库内没有被当日证据引用的资料版本）")
            if not configured:
                print("config 指标 0 条（领域包未登记 export.config_metrics 或配置缺项）")
        finally:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
