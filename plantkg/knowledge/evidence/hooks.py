"""Append B17/B28 facts to the B04 evidence stream via scenario hooks."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from plantkg.core.contracts import EvidenceEvent, EvidenceRef, Locator
from plantkg.core.evidence import SqliteEvidenceSink
from plantkg.core.ports import EvidenceSink
from plantkg.knowledge.documents import DocumentSpec, KnowledgeLibrary, load_library, search
from plantkg.knowledge.graph.graph import DEFAULT_CONFIG, GlobalGraph
from plantkg.knowledge.rules.normalise import entity_keys

logger = logging.getLogger(__name__)


def _sink(runner):
    """B31 写入端：装配传进来的 EvidenceSink 端口优先；没有端口时退回场景库并标记 assembly。"""
    sink = getattr(runner, "_b31_evidence_sink", None)
    return sink if sink is not None else SqliteEvidenceSink(runner.db.writer, runner.db.plant_id)


def _assembly(runner):
    return "storage" if getattr(runner, "_b31_evidence_sink", None) is not None else "scenario_db"


def _event(runner, action_id, kind, params, at, *, sources=(), path="none", rule_ids=(), nature=None, status="ok"):
    run = runner.run_info
    params = dict(params)
    params.setdefault("assembly", _assembly(runner))
    data = json.dumps([run.run_id, action_id, kind, params, at], sort_keys=True, ensure_ascii=False, default=str)
    proposal_id = params.get("proposal_id")
    agent_run_id = next((item.agent_run_id for item in reversed(runner.trace.agent_runs)
                         if proposal_id and proposal_id in item.proposal_ids and item.binding
                         and item.binding.run_id == run.run_id), None) if hasattr(runner, "trace") else None
    return EvidenceEvent(event_id="b31-" + hashlib.sha256(data.encode()).hexdigest(), seq=0,
                         occurred_at=at, available_at=at, run_id=run.run_id,
                         agent_run_id=agent_run_id,
                         trace_id=f"scenario-{runner.world.scenario.scenario_id}", action_id=action_id,
                         version_manifest_id=run.version_manifest_id, event_type=kind,
                         path=path, sources=sources, rule_ids=rule_ids, result_status=status,
                         nature=nature or runner.world.scenario.nature, params=params)


def _action_key(proposal_id, action):
    return (proposal_id, json.dumps({k: action[k] for k in ("entity_id", "command", "params")},
                                    sort_keys=True, ensure_ascii=False))


@lru_cache(maxsize=1)
def _graph():
    return GlobalGraph.load()


@lru_cache(maxsize=1)
def _library():
    return load_library(Path(__file__).resolve().parents[3])


@lru_cache(maxsize=1)
def _supply_paths():
    graph = _graph()
    found = graph.trace_impact("GRID")
    return graph.version, {row["code"]: row["path"] for row in found["affected"]}, {
        row["zone"]: row["path"] for row in found["zones"]}


def _topology(runner, record):
    if _graph().plant_id != runner.db.plant_id:
        return None
    entity = record["action"]["entity_id"]
    version, paths, zones = _supply_paths()
    members = runner.world.index.groups.get(entity, ())
    selected = [(member, paths[member]) for member in members if member in paths]
    source_name = "scenario.site_index.groups" if members else None
    if not selected:
        # 聚合实体到图谱编号的映射只在 B30 normalise 维护一处（#175）
        for key in entity_keys(entity):
            if key.startswith("zone:"):
                code = key.split(":", 1)[1]
                if code in zones:
                    selected, source_name = [(code, zones[code])], "B30 entity map"
                    break
            elif key in paths:
                selected, source_name = [(key, paths[key])], "B30 entity map"
                break
    if not selected:
        return None
    return {"graph_version": version, "target": entity, "paths": [path for _, path in selected],
            "group_members": list(members), "scope": "supply_topology",
            "group_source": source_name}


def _document_support(runner, record):
    """Only cite B26 records explicitly registered for this action and expert."""
    library, action, source = _library(), record["action"], record["source"]
    expert = source.get("expert")
    if expert not in library.experts:
        return []
    ref = "action:" + action["command"]
    authorized = {a.document_id for a in library.activation_refs()
                  if a.stage == "referenced" and a.ref == ref and a.at <= runner.now}
    if action["command"] == "discharge":
        node = _graph().node(action["entity_id"])
        if not node or node["type"] != "battery":
            authorized.clear()
    proposal = next((p for p in runner.trace.proposals if p.proposal_id == source.get("proposal_id")), None)
    # B30's existing synthetic dispatch fixture supplies the B20 knowledge
    # reference in the demonstration, while remaining scoped to elevator.
    if (expert == "elevator" and proposal and "knowledge:elevator_peak_dispatch" in proposal.evidence_refs
            and "elevator_manual" in runner.knowledge and action["command"] in {"set_mode", "set_active"}):
        doc = DocumentSpec(document_id="b31-elevator-dispatch", expert="elevator", title="电梯群控运行规程（演示版）",
                           path="plantkg/knowledge/rules/fixtures/elevator-dispatch.md", kind="text",
                           asset_type="process_doc", source_level="equipment_manual", nature="synthetic",
                           available_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        library = KnowledgeLibrary(library.manifest.model_copy(update={"documents": (*library.manifest.documents, doc)}), root=library.root)
        if doc.available_at <= runner.now:
            authorized.add(doc.document_id)
    ticket_state = runner.tickets.tickets.get(source.get("ticket_id"))
    if (expert == "energy" and action["command"] == "shed" and ticket_state and
            ticket_state.event.get("event_type") in {"load_spike", "sim_limit", "capacity_shed_request"} and
            graph_node_is_unprotected_lighting(action["entity_id"])):
        doc = DocumentSpec(document_id="b31-energy-shedding", expert="energy", title="削峰演示规程（合成）",
                           path="plantkg/knowledge/evidence/fixtures/energy-shedding.md", kind="text",
                           asset_type="process_doc", source_level="equipment_manual", nature="synthetic",
                           available_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        library = KnowledgeLibrary(library.manifest.model_copy(update={"documents": (*library.manifest.documents, doc)}), root=library.root)
        if doc.available_at <= runner.now:
            authorized.add(doc.document_id)
    if (expert == "energy" and action["command"] == "switch_circuit" and ticket_state and
            ticket_state.event.get("event_type") == "lighting_fault" and
            graph_node_is_unprotected_lighting(action["entity_id"])):
        doc = DocumentSpec(document_id="b31-lighting-switch", expert="energy", title="照明故障切换演示规程（合成）",
                           path="plantkg/knowledge/evidence/fixtures/lighting-switch.md", kind="text",
                           asset_type="process_doc", source_level="equipment_manual", nature="synthetic",
                           available_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        library = KnowledgeLibrary(library.manifest.model_copy(update={"documents": (*library.manifest.documents, doc)}), root=library.root)
        if doc.available_at <= runner.now:
            authorized.add(doc.document_id)
    if (expert == "equipment" and action["command"] == "isolate_leak" and ticket_state and proposal and
            ticket_state.event.get("event_type") == "compressor_pressure_drop" and
            "knowledge:compressor_leak_rule" in proposal.evidence_refs and "compressor_leak_rule" in runner.knowledge):
        authorized.update(a.document_id for a in library.activation_refs()
                          if a.stage == "referenced" and a.ref == "rule:air_leak" and a.at <= runner.now)
    if not authorized:
        return []
    query = {"discharge": "储能 放电", "maintenance": "维护 保养", "set_mode": "早高峰 人梯 分区",
             "set_active": "早高峰 人梯 增派", "shed": "照明 削峰 负荷"}.get(action["command"], action["command"])
    if action["command"] == "switch_circuit":
        query = "照明 故障 切换 回路"
    elif action["command"] == "isolate_leak":
        query = "泄漏 定位 隔离 支管"
    hits = search(library, expert, query, as_of=runner.now, top_k=len(library.documents(expert, runner.now)))
    return [hit for hit in hits if hit.document.document_id in authorized]


def graph_node_is_unprotected_lighting(entity):
    graph = _graph()
    node = graph.node(entity)
    return bool(node and node["type"] == "lighting" and graph.safety_tags(entity, "power_off")["verdict"] == "auto_ok")


def _graph_support(runner, record):
    """Return a graph playbook only when its expert step matches the command."""
    source = record["source"]
    state = runner.tickets.tickets.get(source.get("ticket_id"))
    if state is None:
        return None
    event = state.event
    event_type = {"load_spike": "load_surge", "compressor_pressure_drop": "pressure_drop",
                  "elevator_trapped": "elevator_stuck"}.get(event.get("event_type"), event.get("event_type"))
    action = record["action"]
    keyword = {"discharge": "放电", "isolate_leak": "隔离", "repair": "维修",
               "set_mode": "分区" if action.get("params", {}).get("mode") == "zone" else "预冷"}.get(action["command"])
    if not keyword:
        return None
    graph = _graph()
    if graph.plant_id != runner.db.plant_id:
        return None
    for book in graph.playbooks(event=event_type):
        if not any(step.get("by") == source.get("expert") and keyword in step.get("do", "")
                   for step in book["steps"]):
            continue
        event_node, action_node = event.get("entity_id"), action["entity_id"]
        if not event_node or not graph.node(event_node) or not graph.node(action_node):
            continue
        left = graph.find_sources(event_node, "power_loss")["candidates"]
        right = graph.find_sources(action_node, "power_loss")["candidates"]
        candidates = [(a["path"], b["path"]) for a in left for b in right if a["code"] == b["code"]]
        if not candidates:
            continue
        paths = min(candidates, key=lambda pair: len(pair[0]) + len(pair[1]))
        regulation = Path(DEFAULT_CONFIG).with_name("regulations.yaml")
        version = "sha256:" + hashlib.sha256(regulation.read_bytes()).hexdigest()
        ref = EvidenceRef(evidence_id="configs/sites/factory/graph/regulations.yaml",
                          locator=Locator(clause=book["clause"]), excerpt=book["clause_text"][:500])
        return {"paths": paths, "playbook": book["id"], "graph_version": graph.version,
                "source_version": version, "clause": book["clause"],
                "link_type": "posthoc_candidate", "manifest_binding": "unverified"}, ref
    return None


def _conflicts(documents):
    """Compare an explicit auto-permission claim with a B25 safety veto."""
    conflicts = []
    graph = _graph()
    for hit in documents:
        for subject, action in re.findall(r"\b([A-Za-z0-9-]+)\s+([a-z_]+)\s+auto_ok\b", hit.text):
            if not graph.node(subject):
                continue
            safety = graph.safety_tags(subject, action)
            if safety["verdict"] == "work_order" and any(t.get("clause") for t in safety["tags"]):
                ref = EvidenceRef(evidence_id=hit.document.document_id, locator=Locator(clause=hit.locator), excerpt=hit.text[:500])
                conflicts.append(({"subject": subject, "field": "action_permission", "action": action,
                                   "graph_value": "work_order", "graph_safety": safety,
                                   "document_value": "auto_ok", "graph_version": graph.version,
                                   "document_id": hit.document.document_id,
                                   "document_version": "sha256:" + hashlib.sha256(_library().upload_file(hit.document).read_bytes()).hexdigest()}, ref))
    return conflicts


def energy_action_basis(runner, action, plan, minute_state, target, controller_result):
    """Capture the actual plan and controller inputs, without changing control."""
    sink = _sink(runner)
    plan_data = plan.model_dump(mode="json")
    digest = "sha256:" + hashlib.sha256(json.dumps(plan_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = getattr(runner, "_b31_plans", None)
    if cache is None:
        cache = runner._b31_plans = {}
    key = (runner.run_info.run_id, plan.plan_id, digest)
    if key not in cache:
        event = _event(runner, None, "plan_created", {"plan_id": plan.plan_id, "plan_version": digest,
                       "plan": plan_data, "note": "实际控制计划快照；滚动重解计划不代表通过额外审批"}, runner.now,
                       sources=(EvidenceRef(evidence_id=plan.plan_id),))
        sink.append(event)
        cache[key] = event.event_id
    runner._b31_energy_basis = {"action": action.model_dump(mode="json"), "plan_event_id": cache[key],
        "plan_id": plan.plan_id, "plan_version": digest, "plan_manifest_id": plan.version_manifest_id,
        "minute_state": json.loads(json.dumps(minute_state, default=str)),
        "slot_target": json.loads(json.dumps(asdict(target), default=str)),
        "controller_result": controller_result}


def action_recorded(runner, record):
    # The execution record is a fact about what happened, not a claim that the
    # action is justified. Missing supporting evidence remains explicit.
    source = record["source"]
    if not hasattr(runner, "_b31_action_runs"):
        runner._b31_action_runs = {}
    runner._b31_action_runs[record["action_id"]] = runner.run_info.run_id
    sink = _sink(runner)
    errors = []
    key = _action_key(source.get("proposal_id"), record["action"])
    proof = getattr(runner, "_b31_preflight", {}).pop(key, None)
    checked = proof is not None
    if checked:
        support, documents, versions, topology, conflicts = proof
    else:
        versions = {}
        try:
            support = _graph_support(runner, record)
        except Exception as exc:  # The action is already recorded; never undo it on lookup failure.
            support = None
            errors.append(f"graph:{type(exc).__name__}")
        try:
            documents = _document_support(runner, record)
        except Exception as exc:
            documents = []
            errors.append(f"retrieval:{type(exc).__name__}")
        try:
            conflicts = _conflicts(documents)
        except Exception as exc:
            conflicts = []
            errors.append(f"conflicts:{type(exc).__name__}")
        try:
            topology = _topology(runner, record)
        except Exception as exc:
            topology = None
            errors.append(f"topology:{type(exc).__name__}")
    energy_basis = getattr(runner, "_b31_energy_basis", None)
    if energy_basis and energy_basis["action"] == record["action"] and energy_basis["plan_id"] == source.get("plan_id"):
        sink.append(_event(runner, record["action_id"], "action_decided", energy_basis, runner.now,
                           sources=(EvidenceRef(evidence_id=energy_basis["plan_event_id"]),)))
        runner._b31_energy_basis = None
    else:
        energy_basis = None
    data = {"action": record["action"], "ticket_id": source.get("ticket_id"),
            "proposal_id": source.get("proposal_id"),
            "plan_id": source.get("plan_id"), "work_order_id": source.get("work_order_id"),
            "evidence_status": "controller_input" if energy_basis else "conflict" if conflicts else "preflight_checked" if checked and (support or documents) else
                               "candidate_only" if support or documents else "missing_evidence", "lookup_errors": errors}
    sink.append(_event(runner, record["action_id"], "execution_recorded", data, runner.now,
                       status="ok" if data["evidence_status"] == "preflight_checked" else "degraded"))
    for details, ref in conflicts:
        tags = details.get("graph_safety", {}).get("tags", ())
        graph_refs = tuple(EvidenceRef(evidence_id="configs/sites/factory/graph/regulations.yaml",
            locator=Locator(clause=tag["clause"]), excerpt=tag["clause_text"][:500]) for tag in tags if tag.get("clause"))
        sink.append(_event(runner, record["action_id"], "conflict_detected",
            {"ticket_id": source.get("ticket_id"), "proposal_id": source.get("proposal_id"), **details},
            runner.now, sources=(ref, *graph_refs), path="reasoning",
            rule_ids=tuple(tag["clause"] for tag in tags if tag.get("clause")), status="conflict"))
    proposal = next((item for item in runner.trace.proposals if item.proposal_id == source.get("proposal_id")
                     and item.binding and item.binding.run_id == runner.run_info.run_id), None)
    if proposal and any(a.entity_id == record["action"]["entity_id"] and a.command == record["action"]["command"]
                        and a.params == record["action"]["params"] for a in proposal.actions):
        details = {"proposal_id": proposal.proposal_id, "ticket_id": proposal.ticket_id,
                   "expert": proposal.expert, "confidence": proposal.confidence,
                   "proposal_evidence_refs": list(proposal.evidence_refs), "binding": proposal.binding.model_dump(mode="json"),
                   "proposal": proposal.model_dump(mode="json"),
                   "trigger_event": getattr(runner.tickets.tickets.get(proposal.ticket_id), "event", None),
                   "policy_version": runner.policy.policy_version,
                   "note": "结构化提案记录；并不单独证明规程依据完整"}
        refs = [EvidenceRef(evidence_id=proposal.proposal_id)]
        if details["trigger_event"] and details["trigger_event"].get("event_id"):
            refs.append(EvidenceRef(evidence_id=details["trigger_event"]["event_id"]))
        sink.append(_event(runner, record["action_id"], "action_decided", details, runner.now, sources=tuple(refs)))
    if topology:
        topology["link_type"] = "preflight_observation" if checked else "posthoc_candidate"
        topology["proposal_id"] = source.get("proposal_id")
        sink.append(_event(runner, record["action_id"], "graph_query", topology, runner.now,
                           path="reasoning", nature="configured"))
    if support:
        details, ref = support
        details["link_type"] = "preflight_observation" if checked else "posthoc_candidate"
        details["proposal_id"] = source.get("proposal_id")
        sink.append(_event(runner, record["action_id"], "graph_query", details, runner.now,
                           sources=(ref,), path="reasoning", rule_ids=(details["clause"],), nature="synthetic"))
    for hit in documents:
        doc = hit.document
        version = versions.get(doc.document_id)
        if version is None:
            version = "sha256:" + hashlib.sha256(_library().upload_file(doc).read_bytes()).hexdigest()
        ref = EvidenceRef(evidence_id=doc.document_id, locator=Locator(clause=hit.locator), excerpt=hit.text[:500])
        details = {"document_id": doc.document_id, "source_version": version,
                   "available_at": doc.available_at.isoformat(), "activation_ref": ("knowledge:elevator_peak_dispatch"
                       if doc.document_id == "b31-elevator-dispatch" else "b31:synthetic-shedding"
                       if doc.document_id == "b31-energy-shedding" else "b31:synthetic-lighting-switch"
                       if doc.document_id == "b31-lighting-switch" else "rule:air_leak"
                       if record["action"]["command"] == "isolate_leak" else "action:" + record["action"]["command"]),
                   "excerpt": hit.text, "proposal_id": source.get("proposal_id"),
                   "link_type": "preflight_observation" if checked else "posthoc_candidate", "manifest_binding": "unverified"}
        sink.append(_event(runner, record["action_id"], "retrieval", details, runner.now,
                           sources=(ref,), path="retrieval", nature=doc.nature))


def decision_recorded(runner, decision):
    selected = set(decision.result.selected_proposal_ids)
    for record in runner.trace.actions:
        source = record["source"]
        if (getattr(runner, "_b31_action_runs", {}).get(record.get("action_id")) == runner.run_info.run_id and
            source.get("ticket_id") == decision.ticket_id and
            source.get("proposal_id") in selected and source.get("round") == decision.round):
            data = {"decision_id": decision.decision_id, "ticket_id": decision.ticket_id,
                    "proposal_id": source["proposal_id"], "policy_version": decision.conversion.policy_version,
                    "outcome": decision.result.outcome}
            _sink(runner).append(
                _event(runner, record["action_id"], "action_decided", data, max(decision.at, runner.now)))


def preflight(runner, proposal, state):
    """Observe sources before delivery; never change the dispatch decision."""
    checked = getattr(runner, "_b31_preflight", None)
    if checked is None:
        checked = runner._b31_preflight = {}
    for action in proposal.actions:
        if action.readiness != "ready":
            continue
        body = action.model_dump(mode="json", exclude={"readiness", "depends_on"})
        record = {"action": body, "source": {"expert": proposal.expert, "ticket_id": proposal.ticket_id,
                                             "proposal_id": proposal.proposal_id}}
        graph_support, documents = None, []
        try:
            graph_support = _graph_support(runner, record)
            documents = _document_support(runner, record)
            conflicts = _conflicts(documents)
            supported = bool(graph_support or documents) and not conflicts
        except Exception:
            supported = False
            conflicts = []
        for details, ref in conflicts:
            data = {"ticket_id": proposal.ticket_id, "proposal_id": proposal.proposal_id, **details}
            tags = details.get("graph_safety", {}).get("tags", ())
            graph_refs = tuple(EvidenceRef(evidence_id="configs/sites/factory/graph/regulations.yaml",
                locator=Locator(clause=tag["clause"]), excerpt=tag["clause_text"][:500]) for tag in tags if tag.get("clause"))
            _sink(runner).append(
                _event(runner, None, "conflict_detected", data, runner.now, sources=(ref, *graph_refs), path="reasoning",
                       rule_ids=tuple(tag["clause"] for tag in tags if tag.get("clause")), status="conflict"))
        if graph_support or documents:
            try:
                topology = _topology(runner, record)
            except Exception:
                topology = None
            versions = {h.document.document_id: "sha256:" + hashlib.sha256(_library().upload_file(h.document).read_bytes()).hexdigest()
                        for h in documents}
            checked[_action_key(proposal.proposal_id, body)] = (graph_support, documents, versions, topology, conflicts)
        if not supported:
            data = {"ticket_id": proposal.ticket_id, "proposal_id": proposal.proposal_id,
                    "action": body, "reason": "conflict" if conflicts else "missing_evidence", "disposition": "record_only"}
            _sink(runner).append(
                _event(runner, None, "action_decided", data, runner.now, status="degraded"))
    return proposal


def finish(runner, proposal):
    cache = getattr(runner, "_b31_preflight", {})
    for key in list(cache):
        if key[0] == proposal.proposal_id:
            del cache[key]


def register(registry, settings=None):
    def observe(hook, *, preflight_hook=False):
        def safe(*args):
            runner = args[0]
            if not hasattr(runner, "_b31_evidence_sink"):
                runner._b31_evidence_sink = (registry.ports.get(EvidenceSink)
                                             or getattr(runner, "registry", None) and runner.registry.ports.get(EvidenceSink))
            try:
                return hook(*args)
            except Exception:
                logger.exception("B31 evidence hook %s failed; execution continues", hook.__name__)
                if hook is energy_action_basis:
                    args[0]._b31_energy_basis = None
                return args[1] if preflight_hook else None
        return safe

    def install(scenario_registry):
        scenario_registry.register_hook("scenario.action_recorded", observe(action_recorded))
        scenario_registry.register_hook("scenario.energy_action_basis", observe(energy_action_basis))
        scenario_registry.register_hook("scenario.decision_recorded", observe(decision_recorded))
        scenario_registry.register_hook("arbitration.evidence_preflight", observe(preflight, preflight_hook=True))
        scenario_registry.register_hook("arbitration.evidence_finish", observe(finish))

    registry.register_hook("scenario_plugin", install)
