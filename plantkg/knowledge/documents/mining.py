"""历史工单挖掘（D54）：流程段聚类 → Agent 段解读 → 候选规则 → 提交发布或落库。

流程段（`workorders.cluster_workorders`）只按设备、故障现象与时间间隔确定性切类。
Agent 段由 `ClusterInterpreter` 实现：默认 `RuleBasedInterpreter`（按常理编写的合成解读，离线可跑），
真实平台用 `AgentInterpreter` 把工单原文交给专家 Agent 解读。解读结果生成 `Candidate`（kind=knowledge，
changes 指向 rules 组件），经 `KnowledgePublisher.propose_publish` 提交；端口未登记时由
`AssetCandidateStore` 把候选记录（含规则文本与引用的工单原文）落成 B06 资产，等待 B30 发布。

统计口径：`cluster_count` 是聚出的类数；`candidate_count` 是解读后生成的候选规则数；
`adopted_count` 是成功提交给发布端口或落库的候选数（提交 ≠ 已发布，发布仍要复核员确认）。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from plantkg.core.contracts import Candidate, ComponentRef, Context, ErrorCode, ManifestComponent, PlantkgError
from plantkg.knowledge.assets.catalog import SQLiteAssetCatalog
from plantkg.knowledge.documents.spec import KnowledgeLibrary
from plantkg.knowledge.documents.workorders import WorkOrder, WorkOrderCluster, cluster_workorders, load_workorders

INTERPRETER_VERSION = "b26-workorder-mining-v1"
BASE_MANIFEST = "vm-b26-workorder-demo"  # 演示用清单号（synthetic）；真实运行时由调用方传当前清单


@dataclass(frozen=True)
class ClusterReading:
    cluster_id: str
    is_pattern: bool
    kind: Literal["warning", "maintenance"]
    root_cause: str
    rule_text: str
    confidence: float
    interpreter: str = "rule_based"
    nature: str = "synthetic"

    def summary(self) -> dict[str, Any]:
        return {"cluster_id": self.cluster_id, "is_pattern": self.is_pattern, "kind": self.kind,
                "root_cause": self.root_cause, "rule": self.rule_text, "confidence": self.confidence,
                "interpreter": self.interpreter, "nature": self.nature}


class ClusterInterpreter(Protocol):
    def interpret(self, cluster: WorkOrderCluster) -> ClusterReading: ...


class RuleBasedInterpreter:
    """离线替身：按出现次数、时间跨度与常见处理动作套模板，结果标 synthetic。"""

    def __init__(self, *, min_orders: int = 3, max_span_days: int = 365) -> None:
        self.min_orders, self.max_span_days = min_orders, max_span_days

    def interpret(self, cluster: WorkOrderCluster) -> ClusterReading:
        span = (cluster.last_at - cluster.first_at).days
        if cluster.count < self.min_orders or span > self.max_span_days:
            return ClusterReading(cluster.cluster_id, False, "maintenance", "样本不足，暂不生成规则", "", 0.0)
        action = Counter(order.action for order in cluster.orders).most_common(1)[0][0]
        rule = (f"{cluster.equipment} 在 {cluster.first_at:%Y-%m-%d} 至 {cluster.last_at:%Y-%m-%d} 共 {span} 天内出现 "
                f"{cluster.count} 次「{cluster.phenomenon}」，处理集中在「{action}」。建议纳入预警：同类现象再次出现时，"
                f"先按现行维护手册的判别流程检查，并复核工单 {'、'.join(cluster.citations)} 的原文。")
        kind: Literal["warning", "maintenance"] = "warning" if any(
            word in cluster.phenomenon for word in ("压力", "漏", "温度", "下降", "异响", "振动")) else "maintenance"
        return ClusterReading(cluster.cluster_id, True, kind, f"重复出现的「{cluster.phenomenon}」，处理动作集中在「{action}」",
                              rule, round(min(0.85, 0.5 + 0.05 * cluster.count), 2))


class AgentInterpreter:
    """Agent 段：把聚类里的工单原文交给专家 Agent，要求返回一行 JSON；不自带模型，注入 ask 即可。"""

    def __init__(self, ask, *, interpreter: str = "agent") -> None:
        self.ask, self.name = ask, interpreter

    def interpret(self, cluster: WorkOrderCluster) -> ClusterReading:
        lines = "\n".join(f"{i}. {order.text}" for i, order in enumerate(cluster.orders, start=1))
        prompt = ("下面是一组历史工单，已按设备、故障现象和时间确定性聚成一类。请判断是否是真的重复规律、"
                  "可能根因是什么，并给一条预警或维保候选规则（要能引用工单原文）。只输出一行 JSON，不要解释：\n"
                  '{"is_pattern":true|false,"kind":"warning|maintenance","root_cause":"...","rule":"...","confidence":0.0}\n'
                  f"设备：{cluster.equipment}；现象：{cluster.phenomenon}；时间：{cluster.first_at:%Y-%m-%d} 至 "
                  f"{cluster.last_at:%Y-%m-%d}；次数：{cluster.count}\n工单：\n{lines}")
        answer = self.ask(prompt)
        start, end = answer.find("{"), answer.rfind("}")
        if start < 0 or end <= start:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Agent 解读没有返回 JSON")
        try:
            parsed = json.loads(answer[start:end + 1])
            is_pattern = bool(parsed["is_pattern"])
            kind = parsed.get("kind", "warning")
            if kind not in ("warning", "maintenance"):
                raise ValueError
            confidence = float(parsed.get("confidence", 0.0))
            if not 0 <= confidence <= 1:
                raise ValueError
            return ClusterReading(cluster.cluster_id, is_pattern, kind, str(parsed.get("root_cause", "")),
                                  str(parsed.get("rule", "")), confidence, interpreter=self.name, nature="derived")
        except (KeyError, TypeError, ValueError):
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Agent 解读的 JSON 缺字段或取值不合法") from None


@dataclass(frozen=True)
class MiningRecord:
    cluster: WorkOrderCluster
    reading: ClusterReading
    candidate: Candidate
    citations: tuple[WorkOrder, ...]
    publish_action_id: str | None = None
    candidate_asset_version_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"cluster": self.cluster.summary(), "reading": self.reading.summary(),
                "candidate": self.candidate.model_dump(mode="json"),
                "citations": [order.summary() for order in self.citations],
                "publish_action_id": self.publish_action_id,
                "candidate_asset_version_id": self.candidate_asset_version_id}


@dataclass(frozen=True)
class MiningResult:
    orders: tuple[WorkOrder, ...]
    clusters: tuple[WorkOrderCluster, ...]
    readings: tuple[ClusterReading, ...]
    records: tuple[MiningRecord, ...]
    represented: int = 0  # 被候选规则引用的工单数
    adopted_count: int = 0

    @property
    def cluster_count(self) -> int:
        return len(self.clusters)

    @property
    def candidate_count(self) -> int:
        return len(self.records)

    def summary(self) -> dict[str, Any]:
        return {"workorder_count": len(self.orders), "cluster_count": self.cluster_count,
                "repeated_clusters": sum(1 for cluster in self.clusters if cluster.count >= 3),
                "candidate_count": self.candidate_count, "adopted_count": self.adopted_count,
                "represented_workorders": self.represented,
                "clusters": [cluster.summary() for cluster in self.clusters],
                "records": [record.as_dict() for record in self.records]}


def mine(library: KnowledgeLibrary, *, as_of: datetime, interpreter: ClusterInterpreter | None = None,
         window_days: int = 90, created_at: datetime | None = None, base_manifest_id: str = BASE_MANIFEST) -> MiningResult:
    """只挖掘 as_of 下可见的工单；as_of 早于工单资料生效日时没有可挖的工单。"""
    orders = load_workorders(library, as_of=as_of)
    clusters = cluster_workorders(orders, window_days=window_days)
    interpreter = interpreter or RuleBasedInterpreter()
    created_at = created_at or datetime.now(timezone.utc)
    readings = tuple(interpreter.interpret(cluster) for cluster in clusters)
    records = tuple(_record(cluster, reading, created_at, base_manifest_id)
                    for cluster, reading in zip(clusters, readings) if reading.is_pattern and reading.rule_text)
    represented = sum(record.cluster.count for record in records)
    return MiningResult(orders=tuple(orders), clusters=tuple(clusters), readings=readings, records=records,
                        represented=represented, adopted_count=0)


def publish(result: MiningResult, *, publisher: Any = None, store: "AssetCandidateStore | None" = None) -> MiningResult:
    """有 KnowledgePublisher 就提交待确认发布；没登记时落成待发布候选资产。返回记录已更新的结果。"""
    if publisher is None and store is None:
        raise PlantkgError(ErrorCode.PRECONDITION, "没有 KnowledgePublisher，也没有候选落库器；候选不能只留在内存")
    def submit(record):
        supports = getattr(publisher, "supports_candidate", None) if publisher is not None else None
        if publisher is not None and (supports is None or supports(record.candidate)):
            return replace(record, publish_action_id=publisher.propose_publish(record.candidate).action_id)
        if store is not None:
            return replace(record, candidate_asset_version_id=store.save(record))
        raise PlantkgError(ErrorCode.PRECONDITION, "发布器不支持该候选且没有候选落库器；候选不能只留在内存")
    records = tuple(submit(record) for record in result.records)
    return replace(result, records=records, adopted_count=len(records))


class AssetCandidateStore:
    """端口未登记时的落库：候选记录写成 JSON，作为 B06 资产（rule_candidate，derived）保存。"""

    def __init__(self, catalog: SQLiteAssetCatalog, context: Context, staging_dir: Path) -> None:
        self.catalog, self.context, self.staging_dir = catalog, context, Path(staging_dir)

    def save(self, record: MiningRecord) -> str:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        path = self.staging_dir / f"{record.candidate.candidate_id}.json"
        path.write_text(json.dumps(record.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        asset = self.catalog.ingest(self.context, str(path), "rule_candidate", self.context.as_of, nature="derived",
                                    title=f"工单挖掘候选规则 {record.candidate.candidate_id}")
        self.catalog.record_activation(self.context, asset.asset_version_id, "identified", ref="workorder_mining",
                                       available_at=self.context.as_of)
        return asset.asset_version_id


def _record(cluster: WorkOrderCluster, reading: ClusterReading, created_at: datetime, base_manifest_id: str) -> MiningRecord:
    rule_hash = hashlib.sha256((reading.rule_text + cluster.cluster_id).encode()).hexdigest()
    candidate_id = "cand-b26-" + rule_hash[:8]
    candidate = Candidate(
        candidate_id=candidate_id, kind="knowledge", base_manifest_id=base_manifest_id,
        changes={ManifestComponent.rules: ComponentRef(ref=f"rules@b26-workorder-{rule_hash[:8]}", content_hash="sha256:" + rule_hash)},
        status="proposed",
        trigger=(f"历史工单挖掘：{cluster.equipment} / {cluster.phenomenon} 在 {cluster.first_at:%Y-%m-%d} 至 "
                 f"{cluster.last_at:%Y-%m-%d} 出现 {cluster.count} 次；引用工单 {'、'.join(cluster.citations)}"),
        created_at=created_at, validation_policy_version=INTERPRETER_VERSION)
    return MiningRecord(cluster=cluster, reading=reading, candidate=candidate, citations=cluster.orders)
