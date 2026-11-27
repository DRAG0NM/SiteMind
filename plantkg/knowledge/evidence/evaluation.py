"""Small, fixed synthetic two-document QA comparison; no model calls."""
from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from plantkg.core.hashing import hash_file, hash_text
from plantkg.knowledge.documents.retrieval import search
from plantkg.knowledge.documents.spec import DocumentSpec, ExpertSpec, KnowledgeLibrary, Manifest
from plantkg.knowledge.graph.graph import GlobalGraph

AS_OF = datetime(2026, 7, 1, tzinfo=timezone.utc)
VERSION = "b31-synthetic-qa-v2"
# Each row creates a maintenance/operation record and a separate procedure.
# Both documents are needed: the first names the procedure, the second its action.
CASES = (
    ("AC-1", "泄漏巡检", "检查支管"), ("AC-2", "泄漏复核", "检查接头"),
    ("AC-3", "维修后核查", "复测压力"), ("AIR-HDR", "主管压力异常", "检查阀门"),
    ("FE-1", "货梯排队", "按生产任务排序"), ("FE-2", "货梯故障", "隔离故障梯"),
    ("FE-3", "货梯高峰", "合并运输批次"), ("FE-4", "货梯维保", "安排备用梯"),
    ("PE-1", "人梯早高峰", "首层集中派梯"), ("PE-2", "人梯晚高峰", "优先下行"),
    ("PE-3", "人梯低谷", "停用部分电梯"), ("PE-4", "人梯拥堵", "分区运行"),
    ("BAT1", "储能峰段", "按计划放电"), ("PV1", "光伏下降", "核查发电功率"),
    ("LINE-1", "峰时工序", "延后可平移工序"), ("LINE-2", "峰时排产", "合并低谷批次"),
    ("CR-AHU1", "洁净温度异常", "保持硬约束"), ("CR-AHU2", "洁净区削峰", "禁止停机"),
    ("HZ-EXF1", "危化排风异常", "派操作工单"), ("DC-IT1", "机房供电异常", "检查备用供电"),
    ("DC-CRAC1", "机房空调故障", "启用备用空调"), ("DC-CRAC2", "机房冷却异常", "通知值班人员"),
    ("AHU-P1", "生产区温度异常", "核查设定温度"), ("SYN-MISSING", "备用设备预冷", "按计划预冷"),
)
UPSTREAM = ("P-AIR", "P-AIR", "P-AIR", "AC-1", "P-LIFT-F", "P-LIFT-F", "P-LIFT-F", "P-LIFT-F",
            "P-ADMIN-LIFT", "P-ADMIN-LIFT", "P-ADMIN-LIFT", "P-ADMIN-LIFT", "P-PARK", "P-PARK",
            "P-LINE", "P-LINE", "P-CR", "P-CR", "P-HZ", "P-DC", "P-DC", "P-DC", "P-HVAC-P", "SYN-SOURCE")


def _corpus(root: Path):
    docs, questions = [], []
    for i, (entity, topic, answer) in enumerate(CASES, 1):
        rule, first, second = f"rule{i:02d}", f"case-{i:02d}-a", f"case-{i:02d}-b"
        fallback = "上游来源: SYN-SOURCE\n" if entity == "SYN-MISSING" else ""
        texts = {first: f"# 运行记录\n设备 {entity}，事项 {topic}。\n适用规程: {rule}\n{fallback}",
                 second: f"# 规程条款\n规程: {rule}\n处置: {answer}\n"}
        for name, text in texts.items():
            (root / f"{name}.md").write_text(text, encoding="utf-8", newline="\n")  # #148：固定 LF
        for doc_id, level in ((first, "experience_record"), (second, "equipment_manual")):
            docs.append(DocumentSpec(document_id=doc_id, expert="equipment", title=doc_id, path=f"{doc_id}.md",
                        kind="text", asset_type="manual", source_level=level, available_at=AS_OF, nature="synthetic"))
        questions.append({"id": f"q{i:02d}", "question": f"{entity} 的直接上游是谁，{topic}应按哪条规程如何处置？",
                          "entity": entity, "topic": topic, "gold_rule": rule,
                          "gold_answer": answer, "gold_upstream": UPSTREAM[i-1], "required_documents": [first, second], "required_document_count": 2,
                          "required_versions": {d: hash_text(text) for d, text in texts.items()}, "nature": "synthetic"})
    manifest = Manifest(experts={"equipment": ExpertSpec(display_name="设备", agent="equipment")}, documents=tuple(docs))
    return KnowledgeLibrary(manifest, root=root), questions


def _answer(library, query, graph_path=()):
    """The same two-query deterministic answerer is used for both arms."""
    first = search(library, "equipment", query, as_of=AS_OF, top_k=1)
    if not first:
        return None, []
    match = re.search(r"适用规程:\s*(rule\d+)", first[0].text)
    if not match:
        return None, [first[0]]
    second = search(library, "equipment", match.group(1), as_of=AS_OF, top_k=2)
    hits = first + [h for h in second if h.document.document_id != first[0].document.document_id][:1]
    if len(hits) < 2:
        return None, hits
    found = re.search(r"处置:\s*(.+)", hits[1].text)
    source = re.search(r"上游来源:\s*([A-Za-z0-9-]+)", first[0].text)
    upstream = graph_path[-2] if len(graph_path) >= 2 else source.group(1) if source else None
    return {"rule": match.group(1), "action": found.group(1).strip(), "upstream": upstream} if found else None, hits


def evaluate(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    graph = GlobalGraph.load()
    with tempfile.TemporaryDirectory(prefix="b31-eval-") as temp:
        library, questions = _corpus(Path(temp))
        rows = []
        paths = graph.trace_impact("GRID")["affected"]
        for question in questions:
            graph_path = next((x["path"] for x in paths if x["code"] == question["entity"]), [])
            for mode in ("graph-first", "rag"):
                query = (f"{graph_path[-1]} {question['topic']}" if mode == "graph-first" and graph_path
                         else question["question"])
                answer, hits = _answer(library, query, graph_path if mode == "graph-first" else ())
                ids = [h.document.document_id for h in hits]
                correct = answer == {"rule": question["gold_rule"], "action": question["gold_answer"], "upstream": question["gold_upstream"]}
                complete = (set(question["required_documents"]).issubset(ids) and all(h.text and h.locator for h in hits)
                            and bool(answer and answer["upstream"]))
                rows.append({"question_id": question["id"], "mode": mode, "answer": answer,
                             "correct": correct, "evidence_complete": complete, "documents": ids,
                             "citations": [{"id": h.document.document_id, "locator": h.locator,
                                            "excerpt": h.text,
                                            "version": hash_file(library.resolve(h.document))}
                                           for h in hits],
                             "graph_path": graph_path if mode == "graph-first" else []})
        totals = {mode: {"questions": len(questions), "correct": sum(r["correct"] for r in rows if r["mode"] == mode),
                         "complete": sum(r["evidence_complete"] for r in rows if r["mode"] == mode)}
                  for mode in ("graph-first", "rag")}
        payload = {"mode": "offline-synthetic", "version": VERSION, "as_of": AS_OF.isoformat(), "seed": 0,
                   "answerer": "same deterministic two-query extractor", "retrieval_budget": "1 + 2 hits",
                   "questions": questions, "results": rows, "summary": totals}
        _write(out / "results.json", json.dumps(payload, ensure_ascii=False, indent=2))
        with (out / "results.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=("question_id", "mode", "correct", "evidence_complete", "documents"), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        lines = ["# 合成场景离线对照（非真实 LLM 实测）", "", "| 路线 | 正确率 | 证据完整率 |", "| --- | --- | --- |"]
        for mode, total in totals.items():
            n = total["questions"]
            lines.append(f"| {mode} | {total['correct']}/{n} ({total['correct']/n:.1%}) | {total['complete']}/{n} ({total['complete']/n:.1%}) |")
        lines += ["", "两组共享合成资料、时点、两次检索预算和确定性回答器；问题刻意要求上游关系，23 题该关系仅在图谱中，1 题图谱缺路径并从资料回退。结果不代表一般 RAG 或真实模型性能。", ""]
        _write(out / "results.md", "\n".join(lines))
        return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件再替换。"""
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)
