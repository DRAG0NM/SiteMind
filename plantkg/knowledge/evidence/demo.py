"""Offline scenario and evidence demo. Every constructed document is synthetic."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from plantkg.core.evidence import SqliteEvidenceSink
from plantkg.knowledge.documents.spec import DocumentSpec, ExpertSpec, KnowledgeLibrary, Manifest
from plantkg.knowledge.graph.graph import GlobalGraph

from .service import EvidenceService

ROOT = Path(__file__).resolve().parents[3]


def run_demo(build_world, open_db, scenario_runner, load_scenario):
    scenario = load_scenario(ROOT / "configs/scenarios/factory_demo_day.yaml")
    with tempfile.TemporaryDirectory(prefix="b31-demo-") as tmp:
        root = Path(tmp)
        world = build_world(scenario)
        db = open_db(root / "scenario.db", world.site.plant_id)
        try:
            runner = scenario_runner(world, db, mode="evaluation", group="coordinated")
            runner.run(until=scenario.instant("08:20"))
            sink = SqliteEvidenceSink(db.writer, db.plant_id)
            context = runner._context(runner.run_info.version_manifest_id).model_copy(
                update={"run_id": runner.run_info.run_id, "as_of": runner.now})
            chosen = None
            statuses = []
            for action in runner.trace.actions:
                if getattr(runner, "_b31_action_runs", {}).get(action["action_id"]) != context.run_id:
                    continue
                page = sink.list(context.run_id, None, context, action_id=action["action_id"])
                statuses.extend(e.params.get("evidence_status") for e in page.items if e.event_type == "execution_recorded")
                if any(e.event_type == "graph_query" for e in page.items) and any(
                    e.event_type == "action_decided" and e.params.get("decision_id") for e in page.items
                ):
                    chosen = chosen or action
            if chosen is None:
                raise RuntimeError("No B28 action with graph playbook evidence in the scenario")
            page = sink.list(context.run_id, None, context, action_id=chosen["action_id"])
            print("== B28 动作与 B04 证据（synthetic）")
            print(json.dumps({"actions_checked": len(statuses), "missing_count": sum(s == "missing_evidence" for s in statuses)}))
            print(json.dumps({"action": chosen, "evidence": [e.model_dump(mode="json") for e in page.items]}, ensure_ascii=False, indent=2))
            if runner.trace.ledger:
                print("== 结构化分钟账本（原始记录）")
                print(json.dumps(runner.trace.ledger[-1].to_json(), ensure_ascii=False, default=str))

            # Separate synthetic source to demonstrate a contradicting edge.
            (root / "conflict.md").write_text("# 操作记录\nDC-IT1 power_off auto_ok\n", encoding="utf-8")
            doc = DocumentSpec(document_id="synthetic-conflict", expert="equipment", title="拓扑记录",
                               path="conflict.md", kind="text", asset_type="manual", source_level="equipment_manual",
                               available_at=context.as_of, nature="synthetic")
            library = KnowledgeLibrary(Manifest(experts={"equipment": ExpertSpec(display_name="设备", agent="equipment")},
                                                documents=(doc,)), root=root)
            service = EvidenceService(GlobalGraph.load(), library, sink, expert="equipment")
            check_context = context.model_copy(update={"run_id": "run-synthetic-check", "trace_id": "trace-synthetic-check"})
            common = dict(context=check_context, action_id="synthetic-check-action", ticket_id="synthetic-check-ticket",
                          expert="equipment", query="DC-IT1 power_off", rule_id="synthetic-safety", rule_version="v1")
            conflict = service.explain(entity="TX2", target="DC-IT1", **common,
                graph_claim={"subject": "DC-IT1", "field": "action_permission", "action": "power_off", "value": "work_order"},
                document_claims=({"document_id": "synthetic-conflict", "subject": "DC-IT1", "field": "action_permission",
                                  "action": "power_off", "value": "auto_ok", "quote": "DC-IT1 power_off auto_ok"},))
            count_before = len(service.list_action(context=check_context, action_id="synthetic-check-action").items)
            service.explain(entity="TX2", target="DC-IT1", **common,
                graph_claim={"subject": "DC-IT1", "field": "action_permission", "action": "power_off", "value": "work_order"},
                document_claims=({"document_id": "synthetic-conflict", "subject": "DC-IT1", "field": "action_permission",
                                  "action": "power_off", "value": "auto_ok", "quote": "DC-IT1 power_off auto_ok"},))
            count_after = len(service.list_action(context=check_context, action_id="synthetic-check-action").items)
            fallback = service.explain(entity="unknown-device", **common)
            early = check_context.model_copy(update={"as_of": check_context.as_of.replace(year=check_context.as_of.year - 1)})
            visible = library.documents("equipment", early.as_of)
            print("== 缺边回退、冲突和未来资料过滤")
            print(json.dumps({"fallback": fallback["route"], "missing": fallback["missing"],
                              "conflict": conflict["conflict"], "comparisons": conflict["comparisons"],
                              "future_document_visible": bool(visible),
                              "future_events_visible": bool(service.list_action(context=early, action_id="synthetic-check-action").items),
                              "repeat_added_events": count_after - count_before}, ensure_ascii=False, indent=2))
        finally:
            db.close()
