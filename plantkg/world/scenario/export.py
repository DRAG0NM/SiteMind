"""export-day（B17）：把一次真实场景运行按 day.schema.json 写出。

沿 runs 接续链（forked_from）拼出完整一天：runs、events、state_series、ledger_minutes 取链末运行（分叉时已复制分叉前的记录），
事件单、工单、临时规则、知识版本、决策、提案、Agent 运行与问答按链合并、按 id 去重（分叉不复制提案与 Agent 运行）。
Agent 运行还可合并 B23 的 `SqliteAgentRunStore.list(run_id=...)`；证据与资产由装配层传入，只导出被证据引用到的资产。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from plantkg.core.contracts import (
    AgentRunRecord, AssetVersion, DayContext, DayDecision, DayEvent, DayMetric, DayRecord, EvidenceEvent, ExpertProposal, KnowledgeVersionEntry,
    QaEntry, RunChainEntry, TempRule, Ticket, WorkOrder, safe_error_details,
)

from .runner import ScenarioDB

TICKET_FIELDS = set(Ticket.model_fields)

# 场景注册表钩子（#238 方案 1）：领域包登记 fn(site, policy) -> list[DayMetric]，
# 导出时汇总成 day.json 的 config 组限值；数值由各包从自己的配置读，导出侧不写死。
CONFIG_METRICS_HOOK = "export.config_metrics"


def chain(db: ScenarioDB, run_id: str) -> list:
    runs = [db.runs.get_run(run_id)]
    while runs[0].forked_from is not None:
        runs.insert(0, db.runs.get_run(runs[0].forked_from))
    return runs


def latest_scenario_run(db: ScenarioDB) -> str:
    """最近一次场景运行的链末（没有被分叉过的那个）。"""
    items, cursor, out = [], None, []
    while True:
        page, cursor = db.runs.list_runs(cursor, 200)
        items += page
        if cursor is None:
            break
    scen = [r for r in items if r.kind == "scenario"]
    parents = {r.forked_from for r in scen if r.forked_from}
    out = [r for r in scen if r.run_id not in parents]
    if not out:
        raise SystemExit("库里没有场景运行，先运行 run")
    return out[-1].run_id


def config_metrics(registry: Any, site: Any = None, policy: Any = None) -> tuple[DayMetric, ...]:
    """汇总各领域包登记的 `export.config_metrics`（#238 方案 1）。

    钩子签名 `(site, policy) -> list[DayMetric]`，数值由包自己从配置读；没有登记就返回空。
    """
    out = []
    if registry is not None:
        for hook in registry.hooks_for(CONFIG_METRICS_HOOK):
            out.extend(hook(site, policy) or ())
    return tuple(out)


def day_window(runs: Sequence[Any], local_date: str | None = None,
               timezone: str = "Asia/Shanghai") -> tuple[date, datetime, datetime]:
    """导出日窗口：厂区当地 [00:00, 次日 00:00) 对应的 UTC 区间；local_date 缺省取链首运行起始日。"""
    tz = ZoneInfo(timezone)
    day = datetime.fromisoformat(local_date).date() if local_date else runs[0].clock.start.astimezone(tz).date()
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(ZoneInfo("UTC"))
    return day, start, start + timedelta(days=1)


def _status_at_day_end(record: dict, end: datetime) -> tuple[str | None, tuple[dict, ...]]:
    """#217-3：当日结束前最后一次状态迁移，以及截到当日结束的 status_history。

    运行时记录仍保存全链历史；导出只带当日结束前的条目，避免 7-01 的 day.json 提前出现 7-02 的迁移。
    """
    latest = None
    history = []
    for change in record.get("status_history") or ():
        at, status = (change.get("at"), change.get("status")) if isinstance(change, dict) else (change.at, change.status)
        if isinstance(at, str):
            at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if at < end:
            history.append(change)
            latest = status
    return latest, tuple(history)


def _with_sensor_case(event: DayEvent, cases: dict[str, dict]) -> DayEvent:
    """#236：把 b17_records(kind="sensor_case") 的字段合并进对应事件 payload 顶层键，不改运行时事件流。"""
    case = cases.get(event.event_id)
    return event if not case else event.model_copy(update={"payload": {**event.payload, **case}})


# #236：可疑阶段字段；结论字段要等试探结论产生当天才带出
SENSOR_SUSPECT_FIELDS = ("event_id", "ticket_id", "sensor", "reading", "baseline", "residual_c", "residual_limit_c",
                         "quality", "candidates", "nature")


def _sensor_case_at_day_end(record: dict, end: datetime) -> dict:
    """#236：试探结论在当日结束之后才产生时，只带可疑阶段字段，不提前暴露结论。运行时记录不变。"""
    verified_at = record.get("verified_at")
    if not verified_at:
        return record
    at = verified_at if isinstance(verified_at, datetime) else datetime.fromisoformat(str(verified_at).replace("Z", "+00:00"))
    if at < end:
        return record
    return {key: record[key] for key in SENSOR_SUSPECT_FIELDS if key in record}


def export_day(db: ScenarioDB, run_id: str, local_date: str | None = None, timezone: str = "Asia/Shanghai", *,
               agent_runs: Any = None, metrics: Sequence[DayMetric] = (), evidence: Sequence[EvidenceEvent] = (),
               assets: Sequence[AssetVersion] = (), registry: Any = None, site: Any = None,
               policy: Any = None, asset_metrics_provider: Any = None) -> DayRecord:
    """agent_runs：B23 运行记录库（有 `list(run_id=...)`）；metrics：B33 指标（含 knowledge 组）；
    evidence/assets：B26 证据事件与候选资产条目，只导出链内运行当天、且被证据引用到的资产。
    registry/site/policy（#238）：经 `export.config_metrics` 钩子汇总配置限值（config 组）。"""
    runs = chain(db, run_id)
    run_ids = {r.run_id for r in runs}
    day, start, end = day_window(runs, local_date, timezone)
    inside = lambda t: start <= t < end  # noqa: E731
    final = runs[-1].run_id
    store = db.store
    configured = config_metrics(registry, site, policy)
    # Persisted minute intervals represent the clock actually reached, not the planned end.
    minutes = store.ledger_minutes(final)
    reached = max((m.entry.interval.end for m in minutes), default=runs[-1].clock.start)
    cutoff = min(end - timedelta(microseconds=1), reached)
    complete = reached >= end
    if asset_metrics_provider is None:
        note = "scope=plant_catalog；资产目录未装配；统计不可用"
        catalog_metrics = tuple(DayMetric(metric=name, group="knowledge", value=None,
                                        unit="ratio" if name == "asset_activation_rate" else "asset_version",
                                        nature="derived", as_of=cutoff, note=note)
                                for name in ("asset_total", "asset_activated", "asset_state_counts", "asset_activation_rate"))
    else:
        catalog_metrics = tuple(asset_metrics_provider(cutoff, complete))
    official_names = {m.metric for m in catalog_metrics}
    sensor_cases = {}
    for r in runs:
        for raw in store.records(r.run_id, "sensor_case"):
            if raw.get("event_id"):
                sensor_cases[raw["event_id"]] = _sensor_case_at_day_end(raw, end)
    tickets, work_orders, temp_rules, decisions, kvs = {}, {}, {}, {}, {}
    cancelled_rules: set[str] = set()
    proposals, agent_run_records, qa = {}, {}, {}
    for r in runs:
        for t in store.records(r.run_id, "ticket"):
            payload = {k: v for k, v in t.items() if k in TICKET_FIELDS}
            status, history = _status_at_day_end(t, end)  # #217-3：当日末状态 + 截到当日结束的历史
            payload["status"] = status or t["status"]
            payload["status_history"] = history
            tickets[t["ticket_id"]] = Ticket.model_validate(payload)
        for w in store.records(r.run_id, "work_order"):
            status, history = _status_at_day_end(w, end)
            work_orders[w["work_order_id"]] = WorkOrder.model_validate(
                {**w, "status": status or w["status"], "status_history": history})
        for x in store.records(r.run_id, "temp_rule"):
            if x.get("kind") == "temp_rule":  # B47 指令路径用 runner.note 在同一记录名下写备注，不是 TempRule 契约记录
                continue
            temp_rules[x["rule_id"]] = TempRule.model_validate(x)
        for x in store.records(r.run_id, "temp_rule_cancelled"):  # 开始前就被人工取消的规则不进当天导出（#245）
            cancelled_rules.add(x["rule_id"])
        for k in store.records(r.run_id, "knowledge_version"):
            kvs[k["version"]] = KnowledgeVersionEntry.model_validate(k)
        for decision in store.records(r.run_id, "decision"):
            decisions[decision["decision_id"]] = DayDecision.model_validate(decision)
        for raw in store.records(r.run_id, "proposal"):
            proposal = ExpertProposal.model_validate(raw)
            proposals[proposal.proposal_id] = proposal
        for raw in store.records(r.run_id, "agent_run"):
            record = AgentRunRecord.model_validate(raw)
            agent_run_records[record.agent_run_id] = record
        for raw in store.records(r.run_id, "qa"):
            entry = QaEntry.model_validate(raw)
            qa[entry.qa_id] = entry
    if agent_runs is not None:  # B23 记录库与场景记录是两套：同一编号只留场景记录
        for r in runs:
            for record in agent_runs.list(run_id=r.run_id):
                agent_run_records.setdefault(record.agent_run_id, record)
    day_evidence = tuple(e for e in evidence if e.run_id in run_ids and inside(e.occurred_at))
    referenced = {ref.asset_version_id for e in day_evidence for ref in e.sources if ref.asset_version_id}
    chain_entries = tuple(RunChainEntry(run_id=r.run_id, forked_from=r.forked_from, forked_at=r.forked_at,
                                        knowledge_version=db.versions.knowledge_version(r.version_manifest_id), data_sources=r.data_sources)
                          for r in runs)
    day_events = tuple(_with_sensor_case(e, sensor_cases)
                       for e in (DayEvent.model_validate(x) for x in store.records(final, "event")) if inside(e.at))
    # 前一日打开、当日仍有记录的事件单也算在当日：当日提案/决策/工单引用到的事件单一并保留，
    # 否则次日导出只按 opened_at 过滤，会连它的当日提案、决策、工单一起丢掉。
    # 值班确认/纠错事件（operator_confirm/operator_correct）的 payload["event"] 指向事件单里的事件 id，
    # 只有这类跨日记录的事件单也要通过它带进当日导出。
    keep = {t.ticket_id for t in tickets.values() if inside(t.opened_at)}
    keep |= {p.ticket_id for p in proposals.values() if p.binding is not None and inside(p.binding.trigger_at)}
    keep |= {d.ticket_id for d in decisions.values() if inside(d.at)}
    keep |= {w.ticket_id for w in work_orders.values() if inside(w.created_at)}
    referenced_events = {e.payload["event"] for e in day_events
                         if e.event_type in ("operator_confirm", "operator_correct") and isinstance(e.payload.get("event"), str)}
    keep |= {t.ticket_id for t in tickets.values() if referenced_events.intersection(t.event_ids)}
    day_tickets = tuple(t for t in tickets.values() if t.ticket_id in keep)
    day_work_orders = tuple(w for w in work_orders.values() if w.ticket_id in keep and inside(w.created_at))
    day_proposals = tuple(p for p in proposals.values() if p.ticket_id in keep and (p.binding is None or inside(p.binding.trigger_at)))
    day_decisions = tuple(sorted((d for d in decisions.values() if d.ticket_id in keep and inside(d.at)),
                                 key=lambda d: (d.at, d.decision_id)))
    # 跨日引用闭包（#217-1、#217-2 / #238 方案 3）：当日记录引用、但当日没导出的记录原样放进 context。
    # 事件要沿整条运行链找：当日 events 只取链末运行，链内更早运行里的跨日事件也要能找到。
    proposal_refs = {pid for d in day_decisions for pid in d.conflict.proposal_ids} - {p.proposal_id for p in day_proposals}
    event_refs = {eid for t in day_tickets for eid in t.event_ids} - {e.event_id for e in day_events}
    ticket_refs = {w.ticket_id for w in day_work_orders} - {t.ticket_id for t in day_tickets}
    chain_events: dict[str, DayEvent] = {}
    if event_refs:
        for r in runs:
            for raw in store.records(r.run_id, "event"):
                event = _with_sensor_case(DayEvent.model_validate(raw), sensor_cases)
                chain_events.setdefault(event.event_id, event)
    context = DayContext(
        proposals=tuple(sorted((proposals[pid] for pid in proposal_refs if pid in proposals), key=lambda p: p.proposal_id)),
        events=tuple(sorted((chain_events[eid] for eid in event_refs if eid in chain_events), key=lambda e: (e.at, e.event_id))),
        tickets=tuple(sorted((tickets[tid] for tid in ticket_refs if tid in tickets), key=lambda t: t.ticket_id)),
    )
    return DayRecord(
        plant_id=db.plant_id, local_date=day.isoformat(), timezone=timezone, run_id=final, runs=chain_entries,
        events=day_events,
        tickets=day_tickets, work_orders=day_work_orders,
        temp_rules=tuple(r for r in temp_rules.values() if inside(r.created_at) and r.rule_id not in cancelled_rules),
        knowledge_versions=tuple(sorted(kvs.values(), key=lambda k: k.version)),
        proposals=day_proposals,
        decisions=day_decisions,
        context=context,
        qa=tuple(q for q in qa.values() if inside(q.at)),
        agent_runs=tuple(r for r in agent_run_records.values()
                         if inside(r.binding.trigger_at if r.binding else r.timing.requested_at)),
        metrics=tuple(m for m in metrics if not (m.group == "knowledge" and m.metric in official_names)) + configured + catalog_metrics,
        assets=tuple(a for a in assets if a.asset_version_id in referenced),
        evidence=day_evidence,
        state_series=tuple(f for f in store.frames(final) if inside(f.at)),
        ledger_minutes=tuple(m.entry for m in minutes if inside(m.entry.interval.start)),
    )


def validate_chain(db: ScenarioDB, run_id: str, *, timezone: str = "Asia/Shanghai", registry: Any = None,
                   site: Any = None, policy: Any = None, evidence_for_day: Any = None, asset_metrics_provider: Any = None) -> list[str]:
    """#238 方案 5：逐日导出运行链覆盖的所有日期，校验链内并集与引用完整性。返回问题清单，空表示通过。

    evidence_for_day(runs, as_of) -> (evidence, assets) 由装配层给出（CLI 从同库证据流与资料目录读）；
    缺省不带证据小节，只校验记录并集与 proposal/ticket/event 引用。
    """
    runs = chain(db, run_id)
    if not runs:
        return [f"运行 {run_id} 不在库里"]
    tz = ZoneInfo(timezone)
    first_day = runs[0].clock.start.astimezone(tz).date()
    # 覆盖到运行结束时刻所在的当地日期：结束时刻是次日 00:00 时，(end - 1µs) 仍落在最后一天，闭区间不漏天
    last_day = (runs[-1].clock.end - timedelta(microseconds=1)).astimezone(tz).date()
    dates = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    records, problems = [], []
    for day in dates:
        as_of = day_window(runs, day.isoformat(), timezone)[2]
        evidence, assets = evidence_for_day(runs, as_of) if evidence_for_day is not None else ((), ())
        record = export_day(db, run_id, day.isoformat(), timezone, registry=registry, site=site, policy=policy,
                            evidence=evidence, assets=assets, asset_metrics_provider=asset_metrics_provider)
        records.append(record)
        try:
            DayRecord.model_validate_json(record.model_dump_json())
        except ValueError as exc:
            details = safe_error_details(exc) if hasattr(exc, "errors") else ({"loc": (), "msg": str(exc)},)
            problems.extend(f"{record.local_date} 导出不能通过 DayRecord 校验：{d['msg']}" for d in details)
    store = db.store

    def ids(kind: str, key: str) -> set[str]:
        return {raw[key] for r in runs for raw in store.records(r.run_id, kind)}

    chain_ids = {kind: ids(kind, key) for kind, key in
                 (("ticket", "ticket_id"), ("proposal", "proposal_id"), ("decision", "decision_id"), ("work_order", "work_order_id"))}
    exported = {"ticket": set(), "proposal": set(), "decision": set(), "work_order": set()}
    for record in records:
        exported["ticket"] |= {t.ticket_id for t in (*record.tickets, *record.context.tickets)}
        exported["proposal"] |= {p.proposal_id for p in (*record.proposals, *record.context.proposals)}
        exported["decision"] |= {d.decision_id for d in record.decisions}
        exported["work_order"] |= {w.work_order_id for w in record.work_orders}
    labels = {"ticket": "事件单", "proposal": "提案", "decision": "决策", "work_order": "工单"}
    for kind, label in labels.items():
        missing = sorted(chain_ids[kind] - exported[kind])
        if missing:
            problems.append(f"链内{label}没有出现在任何一天的导出里：{missing}")
    for record in records:
        tickets = {t.ticket_id for t in (*record.tickets, *record.context.tickets)}
        proposals = {p.proposal_id for p in (*record.proposals, *record.context.proposals)}
        events = {e.event_id for e in (*record.events, *record.context.events)}
        assets = {a.asset_version_id for a in record.assets}
        dangling = []
        for decision in record.decisions:
            dangling += [f"决策 {decision.decision_id} 引用的提案 {pid} 不在当日或 context"
                         for pid in decision.conflict.proposal_ids if pid not in proposals]
        for item in (*record.proposals, *record.decisions, *record.work_orders):
            if item.ticket_id not in tickets:
                dangling.append(f"{type(item).__name__} 引用的事件单 {item.ticket_id} 不在当日或 context")
        for ticket in (*record.tickets, *record.context.tickets):
            dangling += [f"事件单 {ticket.ticket_id} 引用的事件 {eid} 不在当日或 context"
                         for eid in ticket.event_ids if eid not in events]
        for event in record.evidence:
            for ref in event.sources:
                if ref.asset_version_id and ref.asset_version_id not in assets:
                    dangling.append(f"证据 {event.event_id} 引用的资料 {ref.asset_version_id} 不在当日 assets")
        problems.extend(f"{record.local_date}：{line}" for line in dangling)
    return problems
