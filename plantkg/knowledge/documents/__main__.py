"""python -m plantkg.knowledge.documents：B26 怎么看效果。

依次做：清单里的资料入库（B06 资产版本）→ 图片结构化描述 → 按专家隔离的检索与排序 →
B23 会注入的 document_paths → 资产激活率 → 历史工单挖掘（确定性聚类 → 解读 → 候选规则）。
默认用临时 SQLite，不碰 runtime/ 与本机数据；`--db` 指定文件可保留结果，
`--refresh-images` 用 deepseek-flash 重新识读图片（需 DASHSCOPE_API_KEY）。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from plantkg.core.contracts import Actor, Context, Role
from plantkg.core.registry import Registry
from plantkg.knowledge.assets.catalog import STAGES, SQLiteAssetCatalog
from plantkg.knowledge.documents import (
    LEVEL_LABEL,
    STAGE_LABEL,
    AssetCandidateStore,
    RuleBasedInterpreter,
    apply_activations,
    describe_image,
    ingest_all,
    install_document_tools,
    load_description,
    load_library,
    mine,
    publish,
    report,
    search,
    search_workorders,
    write_description,
)
from plantkg.storage.migration import MigrationRunner
from plantkg.storage.sqlite import SQLiteWriter

ROOT = Path(__file__).resolve().parents[3]
PLANT = "demo-electronics"
AS_OF = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m plantkg.knowledge.documents")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--db", default=None, help="SQLite 文件；缺省用临时目录（跑完删除）")
    parser.add_argument("--as-of", default=AS_OF.isoformat())
    parser.add_argument("--refresh-images", action="store_true", help="重新调用 deepseek-flash 识读图片并覆盖描述文件")
    parser.add_argument("--json", default=None, help="把本次运行结果写成 JSON（默认 runtime/b26/documents-demo.json）")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    root = Path(args.root)
    as_of = datetime.fromisoformat(args.as_of)
    library = load_library(root)
    run_id = "run-b26-" + secrets.token_hex(4)
    print(f"B26 专家私有知识库演示（run_id={run_id}，as_of={as_of.isoformat()}，plant_id={PLANT}）")
    print(f"清单：{root / 'nexent_assets/knowledge/catalog.yaml'}；上传结果索引：{library.uploaded_at or '未上传（注入派生路径）'}\n")

    if args.refresh_images:
        _refresh_images(library)
    temporary = None
    if args.db:
        db_path = Path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.TemporaryDirectory(prefix="plantkg-b26-")
        db_path = Path(temporary.name) / "catalog.db"
    writer = SQLiteWriter(db_path)
    MigrationRunner(writer).apply()
    catalog = SQLiteAssetCatalog(writer, PLANT, db_path.parent / "asset_blobs", base_dir=root)
    context = Context(plant_id=PLANT, industry_pack="manufacturing", actor=Actor(actor_id="dev-reviewer", roles=(Role.operator,)),
                      channel="offline", trace_id="t-b26-demo", as_of=as_of)
    result: dict = {"run_id": run_id, "as_of": as_of.isoformat(), "nature": "synthetic/derived/real（逐份见清单）"}
    try:
        ingested = ingest_all(catalog, context, library)
        result["ingest"] = _print_assets(ingested)
        result["images"] = _print_images(library, ingested)
        result["retrieval"] = _print_retrieval(library, as_of, ingested)
        result["document_paths"] = _print_document_paths(library, as_of)
        result["activation"] = _print_activation(catalog, context, library, ingested)
        result["mining"] = _print_mining(library, catalog, context, db_path.parent / "candidates", as_of)
    finally:
        writer.close()
        if temporary is not None:
            temporary.cleanup()
    out = Path(args.json) if args.json else ROOT / "runtime" / "b26" / "documents-demo.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 JSON（含资产版本与来源引用）：{out}")
    return 0


def _refresh_images(library) -> None:
    print("重新识读图片（deepseek-flash，真实调用）：")
    for document in library.documents():
        if document.kind != "image":
            continue
        description = describe_image(library.resolve(document), document_id=document.document_id, kind=document.image_kind)
        # 描述文件进仓库，记录相对仓库根的图片路径，不写本机绝对路径
        description = replace(description, image_path=Path(document.path).as_posix())
        write_description(description, library.root / (document.description_path or ""))
        print(f"  {document.title}：{'，'.join(f'{key}={value}' for key, value in description.fields.items())}（{description.elapsed_s} s）")
    print()


def _print_assets(ingested) -> list[dict]:
    print("== 1 资料入库（B06），按专家分库")
    rows = []
    for expert in ("elevator", "hvac", "energy", "equipment"):
        items = [item for item in ingested if item.document.expert == expert]
        print(f"  [{expert}] {len(items)} 份")
        for item in items:
            doc = item.document
            kind = {"text": "文本", "table": "表格", "image": "图片"}[doc.kind]
            dormant = "沉睡" if doc.dormant else "在用"
            print(f"    {doc.title}｜{kind}｜{LEVEL_LABEL[doc.source_level]}｜{doc.nature.value}｜{dormant}｜"
                  f"{item.asset.asset_version_id}")
            rows.append({"document_id": doc.document_id, "expert": expert, "title": doc.title, "kind": doc.kind,
                         "source_level": doc.source_level.value, "nature": doc.nature.value, "dormant": doc.dormant,
                         "asset_version_id": item.asset.asset_version_id, "content_hash": item.asset.content_hash,
                         "source_uri": item.asset.source_uri,
                         "description_asset_version_id": item.description.asset_version_id if item.description else None})
    print()
    return rows


def _print_images(library, ingested) -> list[dict]:
    print("== 2 图片 → 结构化描述（deepseek-flash 识读，描述作为派生资产入库）")
    rows = []
    by_document = {item.document.document_id: item for item in ingested}
    for document in library.documents():
        if document.kind != "image":
            continue
        front = load_description(library.root / (document.description_path or ""))
        item = by_document[document.document_id]
        print(f"  {document.title}（{document.path}）")
        print(f"    模型 {front.get('model')}｜生成于 {front.get('generated_at')}｜性质 {front.get('nature')}")
        print(f"    字段 {json.dumps(front.get('fields'), ensure_ascii=False)}")
        print(f"    描述资产 {item.description.asset_version_id if item.description else '（未入库）'}")
        rows.append({"document_id": document.document_id, "title": document.title, "model": front.get("model"),
                     "fields": front.get("fields"), "description": front.get("body"),
                     "description_asset_version_id": item.description.asset_version_id if item.description else None,
                     "image_asset_version_id": item.asset.asset_version_id})
    print()
    return rows


def _print_retrieval(library, as_of: datetime, ingested) -> dict:
    print("== 3 按专家隔离检索（本地替代检索，真实检索由 Nexent 知识库按注入路径执行）")
    assets = {item.document.document_id: item.asset.asset_version_id for item in ingested}
    descriptions = {item.document.document_id: item.description.asset_version_id
                    for item in ingested if item.description is not None}
    out: dict = {}
    hits = search(library, "elevator", "困人 救援", as_of=as_of, top_k=3)
    print("  电梯专家问「困人救援怎么做」：")
    for hit in hits:
        print(f"    {hit.score:.2f} {LEVEL_LABEL[hit.level]}｜{hit.document.title}｜{hit.locator}")
        print(f"         {hit.text[:110]}")
    if hits:
        print(f"  ≈ 专家回答（引用自己库里的原文）：{hits[0].text[:150]}"
              f"【依据《{hits[0].document.title}》{hits[0].locator}，{LEVEL_LABEL[hits[0].level]}】")
    out["elevator_entrapment"] = [_hit_json(hit, assets) for hit in hits]

    print("\n  跨专家隔离（同一问题分别问电梯与暖通）：")
    queries = [("elevator", "冷冻水 预冷 设定温度"), ("hvac", "冷冻水 预冷 设定温度"),
               ("hvac", "困人 救援 盘车"), ("elevator", "困人 救援 盘车")]
    matrix = []
    for expert, query in queries:
        found = search(library, expert, query, as_of=as_of, top_k=3)
        titles = [hit.document.title for hit in found]
        cross = [hit.document.document_id for hit in found if hit.document.expert != expert]
        print(f"    {expert:9}｜{query}｜命中 {len(found)} 条：{'、'.join(titles) or '无（只查自己库）'}")
        matrix.append({"expert": expert, "query": query, "hits": [_hit_json(hit, assets) for hit in found],
                       "cross_expert": cross})
    out["isolation"] = matrix

    print("\n  排序：相关度优先，相同相关度按来源等级（国家标准/政策 > 设备手册 > 经验记录）")
    ranked = search(library, "equipment", "主管压力 泄漏 工单", as_of=as_of, top_k=4)
    for position, hit in enumerate(ranked, start=1):
        print(f"    {position}. 相关度 {hit.score:.2f}｜{LEVEL_LABEL[hit.level]}｜{hit.document.title}｜{hit.locator}")
    out["ranking"] = [_hit_json(hit, assets) for hit in ranked]

    image_hits = search(library, "equipment", "铭牌 识读 额定功率 出厂编号", as_of=as_of, top_k=1)
    if image_hits:
        hit = image_hits[0]
        print(f"\n  图片依据：{hit.document.title}（{hit.document.path}）")
        print(f"    {hit.text[:150]}")
        print(f"    图片资产：{assets.get(hit.document.document_id)}")
        print(f"    描述资产：{descriptions.get(hit.document.document_id)}")
    out["image_evidence"] = [_hit_json(hit, assets) for hit in image_hits]
    print("\n  说明：政策 PDF 正文由 Nexent 解析，本地替代检索只读 Markdown/CSV；图片检索读的是描述文件。\n")
    return out


def _print_document_paths(library, as_of: datetime) -> dict:
    print("== 4 B23 网关按专家注入的 document_paths（每位专家只看自己的库）")
    out: dict = {}
    for expert, spec in library.experts.items():
        paths = library.paths(expert, as_of)
        others = {library.injection_path(doc) for other in library.experts if other != expert
                  for doc in library.documents(other)}
        foreign = [path for path in paths if path in others]
        print(f"  {expert:9}（{spec.agent}，索引 {library.index_name(expert) or '未上传'}）：{len(paths)} 条，跨专家 {len(foreign)} 条")
        for path in paths[:3]:
            print(f"    {path}")
        not_injected = [doc.document_id for doc in library.documents(expert, as_of)
                        if library.live and doc.kb_upload and not library.indexed_path(doc)]
        opt_out = [doc.document_id for doc in library.documents(expert, as_of) if not doc.kb_upload]
        if not_injected:
            print(f"    未注入（Nexent 未完成处理）：{'、'.join(not_injected)}")
        if opt_out:
            print(f"    按设计不入库（kb_upload=false）：{'、'.join(opt_out)}")
        out[expert] = {"agent": spec.agent, "index_name": library.index_name(expert), "paths": list(paths),
                       "cross_expert": foreign, "not_injected": not_injected, "kb_opt_out": opt_out}
    early = datetime(2025, 5, 10, tzinfo=timezone.utc)
    print(f"  as_of 过滤：能源专家在 {early.date()} 可见 {len(library.documents('energy', early))} 份，"
          f"在 {as_of.date()} 可见 {len(library.documents('energy'))} 份")
    out["as_of_example"] = {"early": early.isoformat(),
                            "documents_early": [doc.document_id for doc in library.documents("energy", early)],
                            "documents_now": [doc.document_id for doc in library.documents("energy", as_of)]}
    print()
    return out


def _print_activation(catalog, context, library, ingested) -> dict:
    print("== 5 资产激活率（沉睡 → 已标识 → 已入图 → 被规则或动作引用）")
    before = report(catalog, context, ingested)
    print("  入库后、使用前：" + "，".join(f"{STAGE_LABEL[stage]} {before['by_stage'][stage]}" for stage in STAGES)
          + f"；被引用率 {before['referenced_rate']:.0%}")
    applied = apply_activations(catalog, context, library, ingested)
    after = report(catalog, context, ingested)
    print(f"  登记 {len(applied)} 条使用事件后：")
    for row in after["documents"]:
        print(f"    {row['stage_label']:3}｜{row['expert']:9}｜{row['level']}｜{row['title']}")
    print("  " + "，".join(f"{STAGE_LABEL[stage]} {after['by_stage'][stage]}" for stage in STAGES)
          + f"；被引用率 {after['referenced_rate']:.0%}（资料被使用的程度，不等于业务收益）")
    print(f"  资产目录全量统计：{after['catalog_summary']['total']} 个版本，被引用率 "
          f"{after['catalog_summary']['referenced_rate']:.0%}（含图片描述等派生资产）\n")
    return {"before": {"by_stage": before["by_stage"], "referenced_rate": before["referenced_rate"]},
            "applied_events": applied,
            "after": {"by_stage": after["by_stage"], "referenced_rate": after["referenced_rate"],
                      "documents": after["documents"], "catalog_summary": after["catalog_summary"]}}


def _print_mining(library, catalog, context, staging_dir: Path, as_of: datetime) -> dict:
    print("== 6 历史工单挖掘（D54）：确定性聚类 → 规则解读 → 候选规则")
    result = mine(library, as_of=as_of, interpreter=RuleBasedInterpreter(), created_at=as_of)
    store = AssetCandidateStore(catalog, context, staging_dir)
    result = publish(result, store=store)
    print(f"  工单 {len(result.orders)} 张，聚成 {result.cluster_count} 类；重复（≥3 张）{sum(1 for c in result.clusters if c.count >= 3)} 类")
    for cluster in result.clusters:
        print(f"    {cluster.cluster_id}｜{cluster.equipment} {cluster.phenomenon}｜{cluster.count} 张｜"
              f"{cluster.first_at:%Y-%m-%d}~{cluster.last_at:%Y-%m-%d}")
    print(f"  候选规则 {result.candidate_count} 条，落库（待发布候选）{result.adopted_count} 条，"
          f"引用工单 {result.represented} 张；KnowledgePublisher 未登记 → 落成 rule_candidate 资产")
    for record in result.records:
        print(f"\n  候选 {record.candidate.candidate_id}（{record.reading.kind}，置信度 {record.reading.confidence}）：")
        print(f"    规则：{record.reading.rule_text}")
        print(f"    根因判断：{record.reading.root_cause}（解读方式 {record.reading.interpreter}，性质 {record.reading.nature}）")
        print(f"    发布通道：{'KnowledgePublisher 提议 ' + str(record.publish_action_id) if record.publish_action_id else '未登记，已落库为 ' + str(record.candidate_asset_version_id)}")
        print("    引用的工单原文（可点开核对）：")
        for order in record.citations:
            print(f"      {order.order_id}（{order.occurred_at:%Y-%m-%d}，{order.equipment}）：{order.text}")
    registry = Registry()
    install_document_tools(registry, library)
    by_equipment, _ = search_workorders(library, as_of=as_of, equipment="AC-2")
    tool_result = registry.tools["workorder_search"].handler(context, equipment="AC-2", page_size=5)
    print(f"\n  注册表工具 workorder_search：按设备 AC-2 命中 {tool_result.data['total']} 张，"
          f"聚类 {tool_result.data['matched_clusters']} 类（工具只返回原文摘要，完整原文见候选记录）")
    print()
    return result.summary() | {"tool": {"registered": "workorder_search" in registry.tools,
                                        "ac2_orders": len(by_equipment),
                                        "ac2_clusters": tool_result.data["matched_clusters"]}}


def _hit_json(hit, assets: dict[str, str]) -> dict:
    return {"document_id": hit.document.document_id, "title": hit.document.title, "expert": hit.document.expert,
            "level": hit.level.value, "score": hit.score, "locator": hit.locator, "text": hit.text,
            "source_asset_version": assets.get(hit.document.document_id), "path": hit.document.path}


if __name__ == "__main__":
    raise SystemExit(main())


