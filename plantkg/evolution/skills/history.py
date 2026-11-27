"""B43 Skill 版本链：从 B32 时间线的 published / rollback 事件重建记录。

版本号与来源工单取自 skill 体（生成候选时写入），评估结论取发布时的回测报告；
回滚记录的候选编号取被回滚到的清单此前那次发布。时间线按写入顺序读，不另建表。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from plantkg.core.contracts import ErrorCode, ManifestComponent, PlantkgError, SkillSummary, SkillVersionRecord
from plantkg.evolution.artifacts import skill_artifacts


def skill_history(versions: Any, store: Any, name: str) -> list[SkillVersionRecord]:
    """某 Skill 的版本链，按时间线顺序（如发布 v1、发布 v2、回滚到 v1 三条）。"""
    artifacts = skill_artifacts(versions.writer, versions.plant_id)
    timeline = store.timeline()
    active_ref = _active_skill_ref(versions)
    published: dict[str, dict[str, Any]] = {}
    records: list[SkillVersionRecord] = []
    for event in timeline:
        kind = event.get("event")
        if kind == "published":
            candidate_id = str(event.get("candidate_id") or "")
            manifest_id = str(event.get("manifest_id") or "")
            candidate, body = _candidate_body(store, artifacts, candidate_id)
            ref = candidate.changes.get(ManifestComponent.skills) if candidate is not None else None
            report_id = event.get("report_id") or (candidate.backtest_report_id if candidate else None)
            published[manifest_id] = {"candidate_id": candidate_id, "report_id": report_id}
            version = _body_version(body, name)
            if version is None:
                # 同一个 skills 槽被其他来源（如 B32 调查模板）发布顶掉：版本链记一条“被覆盖”
                previous = (_manifest_body(versions, artifacts, candidate.base_manifest_id)
                            if candidate is not None and ManifestComponent.skills in candidate.changes else None)
                previous_version = _body_version(previous, name)
                if previous_version is not None:
                    verdict, failures = _report_outcome(store, report_id)
                    records.append(SkillVersionRecord(
                        name=name, version=previous_version, manifest_id=manifest_id, candidate_id=candidate_id,
                        action="overridden", verdict=verdict, failures=failures, at=event["at"],
                        actor=event.get("actor"), source_tickets=_tickets(previous), run_id=previous.get("run_id"),
                        from_manifest_id=candidate.base_manifest_id, current=False))
                continue
            verdict, failures = _report_outcome(store, report_id)
            records.append(SkillVersionRecord(
                name=name, version=version, manifest_id=manifest_id, candidate_id=candidate_id,
                action="publish", verdict=verdict, failures=failures, at=event["at"],
                actor=event.get("actor"), source_tickets=_tickets(body), run_id=body.get("run_id"),
                from_manifest_id=None, current=ref == active_ref))
        elif kind == "rollback":
            manifest_id = str(event.get("manifest_id") or "")
            body = _manifest_body(versions, artifacts, manifest_id)
            version = _body_version(body, name)
            if version is None:
                # 回滚到不含该 Skill 的清单：版本链记一条“回滚到无 skill”，版本号取被撤下的那一版
                from_id = str(event.get("from_manifest_id") or "")
                removed = _manifest_body(versions, artifacts, from_id)
                removed_version = _body_version(removed, name)
                info = published.get(from_id) or _published_info(timeline, from_id)
                if removed_version is not None and (info or {}).get("candidate_id"):
                    verdict, failures = _report_outcome(store, info.get("report_id"))
                    records.append(SkillVersionRecord(
                        name=name, version=removed_version, manifest_id=manifest_id,
                        candidate_id=str(info["candidate_id"]), action="rollback_to_none", verdict=verdict,
                        failures=failures, at=event["at"], actor=event.get("actor"),
                        source_tickets=_tickets(removed), run_id=removed.get("run_id"),
                        from_manifest_id=from_id, current=False))
                continue
            info = published.get(manifest_id) or _published_info(timeline, manifest_id)
            candidate_id = str((info or {}).get("candidate_id") or "")
            if not candidate_id:  # 找不到对应发布记录：缺候选编号，不凭空造一条
                continue
            verdict, failures = _report_outcome(store, (info or {}).get("report_id"))
            records.append(SkillVersionRecord(
                name=name, version=version, manifest_id=manifest_id, candidate_id=candidate_id,
                action="rollback", verdict=verdict, failures=failures, at=event["at"],
                actor=event.get("actor"), source_tickets=_tickets(body), run_id=body.get("run_id"),
                from_manifest_id=event.get("from_manifest_id"),
                current=_manifest_skill_ref(versions, manifest_id) == active_ref))
    return records


def skill_summaries(versions: Any, store: Any) -> list[SkillSummary]:
    """Skill 列表：最新一条版本记录决定版本号、清单与状态；按名称排序。

    只挖出候选、还没发布的 Skill 也列出（status=candidate，manifest_id 与 published_at 留空），
    工作台据此能看到待发布版本。
    """
    timeline = store.timeline()
    names, candidates = _timeline_names(versions, store, timeline)
    summaries: list[SkillSummary] = []
    for name in sorted(names):
        records = skill_history(versions, store, name)
        if records:
            latest = records[-1]
            summaries.append(SkillSummary(name=name, latest_version=latest.version,
                                          candidate_id=latest.candidate_id, manifest_id=latest.manifest_id,
                                          status=_summary_status(latest), published_at=latest.at,
                                          source_tickets=latest.source_tickets))
            # 已有发布记录的 Skill 又挖出待发布的新版本：追加一条 status=candidate 条目，列表上看得到
            pending = candidates.get(name) or {}
            version = _event_version(pending)
            if (version is not None and version > max(record.version for record in records)
                    and pending.get("candidate_id") not in {record.candidate_id for record in records}
                    and _candidate_pending(store, str(pending.get("candidate_id")))):
                summaries.append(SkillSummary(name=name, latest_version=version,
                                              candidate_id=pending.get("candidate_id"), manifest_id=None,
                                              status="candidate", published_at=None,
                                              source_tickets=_tickets(pending)))
            continue
        event = candidates.get(name) or {}
        version = _event_version(event)
        if version is None:
            continue
        summaries.append(SkillSummary(name=name, latest_version=version,
                                      candidate_id=event.get("candidate_id"), manifest_id=None,
                                      status="candidate", published_at=None,
                                      source_tickets=_tickets(event)))
    return summaries


def _timeline_names(versions: Any, store: Any,
                    timeline: Sequence[Mapping[str, Any]]) -> tuple[set[str], dict[str, Mapping[str, Any]]]:
    """时间线里出现过的 Skill 名称：发布/回滚清单里的 skill 体，加上挖出的候选（附最后一条候选事件）。"""
    artifacts = skill_artifacts(versions.writer, versions.plant_id)
    names: set[str] = set()
    candidates: dict[str, Mapping[str, Any]] = {}
    for event in timeline:
        kind = event.get("event")
        if kind == "skill_candidate_created":
            name = str(event.get("skill_name") or "")
            if name:
                names.add(name)
                candidates[name] = event
        elif kind == "published":
            _candidate, body = _candidate_body(store, artifacts, str(event.get("candidate_id") or ""))
            name = str((body or {}).get("name") or "")
            if name:
                names.add(name)
        elif kind == "rollback":
            body = _manifest_body(versions, artifacts, str(event.get("manifest_id") or ""))
            name = str((body or {}).get("name") or "")
            if name:
                names.add(name)
    return names, candidates


def _summary_status(record: SkillVersionRecord) -> str:
    if record.current:
        return "active"
    if record.action == "overridden":
        return "overridden"
    return "rolled_back" if record.action in ("rollback", "rollback_to_none") else "published"


def _candidate_pending(store: Any, candidate_id: str) -> bool:
    """候选是否还在待发布（proposed/validated）；已发布、已拒绝的不算。"""
    try:
        return str(store.candidate(candidate_id).status) in ("proposed", "validated")
    except PlantkgError:
        return False


def _body_version(body: Mapping[str, Any] | None, name: str) -> int | None:
    """skill 体的 name 与版本号；不是这个 Skill（如 B32 调查模板）返回 None。"""
    if not body or body.get("name") != name:
        return None
    try:
        version = int(body["version"])
    except (KeyError, TypeError, ValueError):
        return None
    return version if version >= 1 else None


def _event_version(event: Mapping[str, Any]) -> int | None:
    """skill_candidate_created 事件里的候选版本号。"""
    try:
        version = int(event["version"])
    except (KeyError, TypeError, ValueError):
        return None
    return version if version >= 1 else None


def _tickets(body: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(item) for item in body.get("source_tickets") or ())


def _report_outcome(store: Any, report_id: str | None) -> tuple[str, tuple[str, ...]]:
    """评估结论取自发布时的回测报告；没有报告（旧动作）按 inconclusive 如实标注。"""
    if not report_id:
        return "inconclusive", ()
    try:
        report = store.report(report_id)
    except PlantkgError:
        return "inconclusive", ()
    return report.verdict, tuple(report.failures)


def _candidate_body(store: Any, artifacts: Any,
                    candidate_id: str) -> tuple[Any | None, dict[str, Any] | None]:
    if not candidate_id:
        return None, None
    try:
        candidate = store.candidate(candidate_id)
    except PlantkgError:
        return None, None
    ref = candidate.changes.get(ManifestComponent.skills)
    if ref is None:
        return candidate, None
    try:
        return candidate, artifacts.load(ref)
    except PlantkgError:
        return candidate, None


def _manifest_body(versions: Any, artifacts: Any, manifest_id: str) -> dict[str, Any] | None:
    if not manifest_id:
        return None
    try:
        manifest = versions.get(manifest_id)
    except PlantkgError:
        return None
    ref = manifest.components.get(ManifestComponent.skills)
    if ref is None:
        return None
    try:
        return artifacts.load(ref)
    except PlantkgError:
        return None


def _published_info(timeline: Sequence[Mapping[str, Any]], manifest_id: str) -> dict[str, Any] | None:
    """回滚目标清单的发布记录（正常扫描时已在 published 表里，这里兜底跨库导入的时间线）。"""
    for event in timeline:
        if event.get("event") == "published" and str(event.get("manifest_id")) == manifest_id:
            return {"candidate_id": event.get("candidate_id"), "report_id": event.get("report_id")}
    return None


def _manifest_skill_ref(versions: Any, manifest_id: str) -> Any:
    if not manifest_id:
        return None
    try:
        return versions.get(manifest_id).components.get(ManifestComponent.skills)
    except PlantkgError:
        return None


def _active_skill_ref(versions: Any) -> Any:
    """活动清单的 skills 组件 ref：current 按它判定，不按 manifest_id（其他组件发布也会换清单 id）。"""
    try:
        return versions.active().components.get(ManifestComponent.skills)
    except PlantkgError as exc:
        if exc.code != ErrorCode.PRECONDITION:  # 无活动版本不算错误，只是没有 current
            raise
        return None
