"""Graph and document evidence lookup using the existing B04 event store.

The caller supplies a trusted context and the domain authorised to search its
own library. This service does not execute actions or infer numeric facts.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from plantkg.core.contracts import EvidenceEvent, EvidenceRef, Locator
from plantkg.knowledge.documents.retrieval import search


class EvidenceService:
    def __init__(self, graph: Any, library: Any, sink: Any, *, expert: str | None = None) -> None:
        self.graph, self.library, self.sink = graph, library, sink
        # Bind this at the trusted expert-dispatch boundary, never from a model
        # argument. A service without a bound expert supports reads only.
        self.expert = expert

    def explain(self, *, context: Any, action_id: str, ticket_id: str,
                entity: str, expert: str, query: str, target: str | None = None,
                rule_id: str | None = None, rule_version: str | None = None,
                graph_claim: dict | None = None, document_claims: tuple[dict, ...] = ()) -> dict:
        """Record evidence for a supplied action; never interpret text as a number.

        A graph path is only returned if B25 actually returned it. A document hit
        is an excerpt, not an assertion that it supports the action.
        """
        if context.plant_id != self.graph.plant_id:
            raise ValueError("graph belongs to another plant")
        if not context.run_id or not action_id:
            raise ValueError("run_id and action_id are required")
        if not self.expert or expert != self.expert:
            raise ValueError("search must use the expert bound by the trusted caller")
        graph_result = self.graph.trace_impact(entity) if self.graph.node(entity) else None
        paths = []
        if graph_result:
            paths = [row["path"] for row in graph_result["affected"] + graph_result["zones"]
                     if target is None or row.get("code", row.get("zone")) == target]
        hits = search(self.library, expert, query, as_of=context.as_of)
        documents = [{"document_id": h.document.document_id, "locator": h.locator,
                      "excerpt": h.text, "available_at": h.document.available_at.isoformat(),
                      "nature": h.document.nature.value, "source_level": h.level.value,
                      "source_version": "sha256:" + hashlib.sha256(self.library.upload_file(h.document).read_bytes()).hexdigest()}
                     for h in hits]
        # Compare only structured assertions about the same subject and field.
        # The caller must provide the graph assertion and the originating
        # document identifier; an unrelated search hit is never a conflict.
        comparisons = []
        safety = None
        if (graph_claim and graph_claim.get("field") == "action_permission" and
                graph_claim.get("value") == "work_order" and graph_claim.get("action") and
                self.graph.node(graph_claim.get("subject", ""))):
            safety = self.graph.safety_tags(graph_claim["subject"], graph_claim["action"])
        if safety and safety["verdict"] == "work_order" and any(t.get("clause") for t in safety["tags"]):
            visible = {d["document_id"]: d for d in documents}
            for claim in document_claims:
                if (claim.get("document_id") in visible and
                    claim.get("subject") == graph_claim.get("subject") and
                    claim.get("field") == graph_claim.get("field") and
                    claim.get("action") == graph_claim.get("action") and
                    claim.get("value") == "auto_ok" and
                    claim.get("unit") == graph_claim.get("unit") and
                    "value" in claim and "value" in graph_claim and
                    claim.get("quote") and claim["quote"] in visible[claim["document_id"]]["excerpt"] and
                    f"{claim['subject']} {claim['action']} {claim['value']}" in claim["quote"]):
                    comparisons.append({"graph": graph_claim, "document": claim,
                                        "graph_safety": safety,
                                        "conflict": graph_claim["value"] != claim["value"]})
        missing = []
        if not paths:
            missing.append("graph_path")
        if not documents:
            missing.append("document")
        if not rule_id or not rule_version:
            missing.append("rule_version")
        missing.append("verified_action_link")
        result = {"action_id": action_id, "ticket_id": ticket_id,
                  "run_id": context.run_id, "trace_id": context.trace_id,
                  "version_manifest_id": context.version_manifest_id,
                  "graph_version": graph_result["knowledge_version"] if graph_result else None,
                  "graph_paths": paths, "documents": documents, "rule_id": rule_id,
                  "rule_version": rule_version, "route": "reasoning" if paths else "retrieval",
                  "conflict": any(c["conflict"] for c in comparisons) if comparisons else None,
                  "comparisons": comparisons, "evidence_status": "candidate_only", "missing": missing}
        self._record(context, action_id, result)
        return result

    def _record(self, context: Any, action_id: str, result: dict) -> None:
        # Content-addressed IDs preserve B04's idempotency, including for a
        # different visibility time or changed evidence.
        body = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256((context.as_of.isoformat() + body).encode()).hexdigest()
        sources = tuple(EvidenceRef(evidence_id=d["document_id"], locator=Locator(clause=d["locator"]),
                                    excerpt=d["excerpt"][:500]) for d in result["documents"])
        events = [("graph_query", "reasoning", {"paths": result["graph_paths"], "graph_version": result["graph_version"]}, (), "configured")]
        events.extend(("retrieval", "retrieval", {"document": doc}, (ref,), doc["nature"])
                      for doc, ref in zip(result["documents"], sources))
        if not sources:
            events.append(("retrieval", "retrieval", {"documents": []}, (), None))
        if result["rule_id"]:
            events.append(("rule_lookup", "reasoning", {"rule_version": result["rule_version"]}, (), "configured"))
        if not result["graph_paths"]:
            events.append(("path_switch", "retrieval", {"reason": "missing_graph_path"}, (), None))
        if result["conflict"]:
            conflicting = [c for c in result["comparisons"] if c["conflict"]]
            doc_ids = {c["document"]["document_id"] for c in conflicting}
            cited = tuple(ref for doc, ref in zip(result["documents"], sources) if doc["document_id"] in doc_ids)
            graph_refs = tuple(EvidenceRef(evidence_id="configs/sites/factory/graph/regulations.yaml",
                locator=Locator(clause=tag["clause"]), excerpt=tag["clause_text"][:500])
                for comparison in conflicting for tag in comparison["graph_safety"]["tags"] if tag.get("clause"))
            events.append(("conflict_detected", "reasoning", {"comparisons": result["comparisons"]}, cited + graph_refs, None))
        for index, (kind, path, params, refs, nature) in enumerate(events):
            graph_rules = tuple(ref.locator.clause for ref in refs if ref.locator and ref.locator.clause
                                and ref.evidence_id == "configs/sites/factory/graph/regulations.yaml")
            self.sink.append(EvidenceEvent(event_id=f"b31-{digest}-{kind}-{index}", seq=0,
                occurred_at=context.as_of, available_at=context.as_of, run_id=context.run_id,
                agent_run_id=context.agent_run_id, trace_id=context.trace_id, action_id=action_id,
                version_manifest_id=context.version_manifest_id, event_type=kind, path=path,
                result_status="conflict" if result["conflict"] else "degraded" if result["missing"] else "ok", params=params, nature=nature,
                rule_ids=((result["rule_id"],) if result["rule_id"] else ()) + graph_rules, sources=refs))

    def list_action(self, *, context: Any, action_id: str, cursor: str | None = None,
                    page_size: int | None = None) -> Any:
        """Read an action's existing events through B04's paginated interface (visible as_of + trace 在 SQL 统一过滤)。"""
        if not context.run_id:
            raise ValueError("run_id is required for action evidence")
        return self.sink.list(context.run_id, cursor, context, action_id=action_id, trace_id=context.trace_id,
                              page_size=page_size)
