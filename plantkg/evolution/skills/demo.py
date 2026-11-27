"""B43 演示：合成处置记录 → 挖重复成功流程 → 候选 Skill → 评估 → 发布 v1 → v2 → 回滚 → 版本记录。

由 `python -m plantkg.evolution.skills demo` 调起（装配在命令行入口里做，本模块不 import bootstrap），
结束时打印该 Skill 的三条版本记录；数据全部是 synthetic。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from plantkg.core.contracts import Actor, ComponentRef, Context, ErrorCode, ManifestComponent, PlantkgError, Role
from plantkg.core.ports import CandidateGenerator, PendingActionStore, ValidationPolicy, VersionStore
from plantkg.evolution.artifacts import skill_artifacts
from plantkg.evolution.service import EvolutionService
from plantkg.evolution.skills import miner, validation
from plantkg.evolution.skills.history import skill_history
from plantkg.evolution.store import EvolutionStore

BASE_REF = "scenario-knowledge:empty"


def run_demo(services: Any, *, out: Callable[[str], None] = print) -> dict[str, Any]:
    """在已装配的 services 上跑完整演示；库与配置由命令行入口准备。"""
    return _demo(services, out)


def _demo(services: Any, out: Callable[[str], None]) -> dict[str, Any]:
    registry = services.registry
    versions = registry.resolve(VersionStore)
    actions = registry.resolve(PendingActionStore)
    store = EvolutionStore(versions.writer, versions.plant_id)
    store.registry = registry
    versions.register_verifier("scenario-knowledge:", lambda _component: "complete")
    reviewer = Actor(actor_id="demo-reviewer", roles=(Role.reviewer,))
    base = _base_manifest(versions, reviewer)
    context = Context(plant_id=versions.plant_id, industry_pack=services.settings.industry_pack,
                      actor=reviewer, channel="offline", trace_id="b43-demo",
                      as_of=datetime(2026, 7, 1, 9, tzinfo=timezone.utc))
    service = EvolutionService(store, versions, actions, None, registry, context)
    generator = registry.resolve(CandidateGenerator)
    policy = registry.resolve(ValidationPolicy)
    records = miner.load_execution_records()
    out("B43 演示：重复成功的处置流程 → 候选 Skill → 评估 → 发布/回滚（处置记录 = synthetic）")

    v1_records = _records_for_run(records, "run-b43-v1") or records
    patterns = miner.mine_patterns(v1_records)
    if not patterns:
        out("没有挖到重复成功流程：合成记录不够或窗口内有失败")
        return {"ok": False, "reason": "no-pattern"}
    pattern = patterns[0]
    out(f"挖掘：{pattern.event_type} 重复成功 {pattern.success_count} 次，动作签名 "
        f"{' → '.join(f'{entity}:{command}' for entity, command in pattern.signature)}")
    candidate = generator.propose({"pattern": pattern, "run_id": "run-b43-demo-v1", "source": "demo"},
                                  base, context)[0]
    out(f"候选：{_skill_name(versions, candidate)} v{_skill_version(versions, candidate)} "
        f"({candidate.candidate_id})")
    evaluated = validation.evaluate_skill_candidate(service, policy, candidate.candidate_id)
    out(f"评估：{evaluated['verdict']}（命中率 {evaluated['hit_rate']:.0%}；"
        f"结论 {evaluated['report'].verdict}）")
    if evaluated["verdict"] != "validated":
        out("评估未通过：" + "；".join(evaluated["reasons"]))
        return {"ok": False, "reason": "rejected", "candidate_id": candidate.candidate_id}
    v1_manifest = _publish(service, actions, evaluated["candidate"], reviewer, "演示：复核员确认发布 v1",
                           "b43-demo-publish-v1", out)
    name = _skill_name(versions, evaluated["candidate"])

    v2_records = _records_for_run(records, "run-b43-v2")
    if v2_records:
        patterns = miner.mine_patterns(records)
    if not patterns:
        out("没有可发布的新版本")
        return {"ok": False, "reason": "no-pattern-v2", "name": name}
    second = patterns[0]
    candidate2 = generator.propose({"pattern": second, "run_id": "run-b43-demo-v2", "source": "demo"},
                                   versions.active(), context)[0]
    out(f"再挖：重复成功 {second.success_count} 次 → {name} v{_skill_version(versions, candidate2)} "
        f"({candidate2.candidate_id})")
    evaluated2 = validation.evaluate_skill_candidate(service, policy, candidate2.candidate_id)
    out(f"评估：{evaluated2['verdict']}（命中率 {evaluated2['hit_rate']:.0%}；"
        f"结论 {evaluated2['report'].verdict}）")
    if evaluated2["verdict"] != "validated":
        out("评估未通过：" + "；".join(evaluated2["reasons"]))
        return {"ok": False, "reason": "rejected-v2", "candidate_id": candidate2.candidate_id}
    _publish(service, actions, evaluated2["candidate"], reviewer, "演示：复核员确认发布 v2",
             "b43-demo-publish-v2", out)
    v2_manifest = versions.active().version_manifest_id

    rollback = service.propose_rollback(v1_manifest, "演示：v2 回归观察后回滚到 v1")
    actions.confirm(rollback.action_id, reviewer, "演示：复核员确认回滚 v1", 0, "b43-demo-rollback")
    out(f"回滚：统一确认端点批准 → 活动清单回到 v1 {v1_manifest}")

    history = skill_history(versions, store, name)
    out(f"版本记录（skill={name}）：")
    print_history(history, out)
    return {"ok": True, "name": name, "history": history,
            "v1_manifest_id": v1_manifest, "v2_manifest_id": v2_manifest}


def _base_manifest(versions: Any, reviewer: Actor) -> Any:
    try:
        return versions.active()
    except PlantkgError as exc:
        if exc.code != ErrorCode.PRECONDITION:
            raise
    base = versions.prepare({"rules": ComponentRef(ref=BASE_REF, content_hash="sha256:" + "0" * 64)},
                            "B43 演示基线（synthetic）")
    if base.status == "prepared":
        base = versions.check_integrity(base.version_manifest_id)
    return versions.activate(None, base.version_manifest_id, reviewer, "B43 演示基线")


def _publish(service: EvolutionService, actions: Any, candidate: Any, reviewer: Actor, reason: str,
             key: str, out: Callable[[str], None]) -> str:
    saved = service.store.candidate(candidate.candidate_id)
    action = service.propose_publish(saved)
    actions.confirm(action.action_id, reviewer, reason, 0, key)
    manifest = service.versions.active().version_manifest_id
    out(f"发布：统一确认端点 {action.action_id} 由 {reviewer.actor_id} 批准 → 新清单 {manifest}")
    return manifest


def _records_for_run(records: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    found = [record for record in records if record.get("run_id") == run_id]
    return found or []


def _skill_body(versions: Any, candidate: Any) -> dict[str, Any]:
    return skill_artifacts(versions.writer, versions.plant_id).load(candidate.changes[ManifestComponent.skills])


def _skill_name(versions: Any, candidate: Any) -> str:
    return _skill_body(versions, candidate)["name"]


def _skill_version(versions: Any, candidate: Any) -> int:
    return int(_skill_body(versions, candidate)["version"])


def print_history(history: list[Any], out: Callable[[str], None]) -> None:
    header = ("version", "action", "manifest", "candidate", "verdict", "at", "operator",
              "source_tickets", "run_id", "current")
    rows = [header]
    for record in history:
        rows.append((str(record.version), record.action, record.manifest_id[:18], record.candidate_id,
                     record.verdict, record.at.isoformat(timespec="seconds"), record.actor or "-",
                     ",".join(record.source_tickets), record.run_id or "-", "是" if record.current else "否"))
    widths = [max(len(row[index]) for row in rows) for index in range(len(header))]
    for row in rows:
        out("  " + "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())
