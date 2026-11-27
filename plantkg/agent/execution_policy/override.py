"""B47 人工干预（#245）：暂停自动执行、取消还没生效的动作、撤销已生效的动作。

- 暂停：运行级开关，存在 B27 事件单状态表里（整厂一条，或每个运行一条）；暂停期间非保护动作只提议、
  登记待批准队列（见 ExecutionPolicyService.execute），保护动作（预案 protection 模板）照常自动执行。
- 取消：动作已下发但还没生效，与 retract 同一语义，按动作编号摘掉。
- 撤销：动作已生效，把生效结束时刻提前到当前分钟；设备回到“没有这条动作”走的就是动作区间到期后的同一条路径，
  不另写补偿逻辑。重复撤销幂等；撤销与到期、分叉同一分钟时以拿到运行编辑锁的先后为准。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from plantkg.core.contracts import (
    ActionIntervention, Actor, AppliedAction, AutonomyStatus, ErrorCode, EvidenceEvent, EvidenceEventType, Interval,
    PlantkgError, ResultStatus, RevokeOutcome, RevokePreview,
)
from plantkg.core.ports import EvidenceSink, RunStore

log = logging.getLogger("plantkg.execution_policy.override")


def _iso(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


class HumanOverride:
    """挂在 ExecutionPolicyService 上的人工干预入口；REST 提供者与测试都经它。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    # ---------------------------------------------------------------- 公共
    @property
    def store(self) -> Any:
        if self.service.store is None:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "分级执行未装配执行表")
        return self.service.store

    def _runners(self) -> list[Any]:
        found, seen = [], set()
        for _, runner in self.service._bindings.values():
            if runner is not None and id(runner) not in seen and getattr(runner, "run_info", None) is not None:
                seen.add(id(runner))
                found.append(runner)
        return found

    def _now(self, run_id: str | None) -> datetime:
        runner = self.service._runner_for_run(run_id)
        return _utc(runner.now) if runner is not None else datetime.now(timezone.utc)

    def _check_run(self, run_id: str | None) -> None:
        if not run_id or self.service._runner_for_run(run_id) is not None:
            return
        try:
            self.service.registry.resolve(RunStore).get_run(run_id)
        except (KeyError, PlantkgError):
            raise PlantkgError(ErrorCode.NOT_FOUND, f"运行 {run_id} 不存在") from None

    def _evidence(self, event_type: EvidenceEventType, key: str, at: datetime, run_id: str | None, summary: str,
                  params: Mapping[str, Any], *, action_id: str | None = None) -> None:
        sink = self.service.registry.ports.get(EvidenceSink)
        if sink is None:
            return
        try:
            sink.append(EvidenceEvent(
                event_id=f"b47-override-{key}"[:200], seq=0, occurred_at=_utc(at), run_id=run_id,
                trace_id=f"b47-override-{key}"[:200], action_id=action_id, event_type=event_type,
                result_status=ResultStatus.ok, params=dict(params), summary=summary[:2000]))
        except Exception:  # noqa: BLE001 — 证据写失败不回滚已生效的人工干预
            log.exception("人工干预证据写入失败：%s", key)

    # ---------------------------------------------------------------- 暂停自动执行
    def status(self, run_id: str | None = None) -> AutonomyStatus:
        self._check_run(run_id)
        store = self.store
        active = store.active_pause(run_id)
        record = active or store.pause_record(run_id) or {}
        scope = active["scope"] if active else ("run" if run_id else "plant")
        return AutonomyStatus(
            paused=active is not None, scope=scope, run_id=(active or {}).get("run_id") or run_id,
            by=record.get("paused_by"), since=record.get("paused_at"), reason=record.get("reason"),
            resumed_by=record.get("resumed_by"), resumed_at=record.get("resumed_at"), backlog=self.backlog(run_id))

    def backlog(self, run_id: str | None) -> int:
        """暂停以来积压、仍待批准的提议条数（恢复后仍按条数显示，直到逐条批准或驳回）。"""
        tasks = [task for task in self.store.tasks(status="proposed") if task.autonomy_paused]
        if run_id is not None:
            return sum(1 for task in tasks if task.run_id == run_id)
        # 整厂口径只数在线运行：分叉后父运行里还留着同一批积压的副本，不能重复计数；
        # 没有在线运行时这些任务也无法批准（执行器不在线），不能回退成全部历史任务。
        online = {runner.run_info.run_id for runner in self._runners()}
        return sum(1 for task in tasks if task.run_id in online)

    def pause(self, actor: Actor, reason: str, run_id: str | None = None) -> AutonomyStatus:
        self._check_run(run_id)
        store = self.store
        record = store.pause_record(run_id)
        if record is not None and not record.get("resumed_at"):
            return self.status(run_id)  # 已经暂停：幂等，保留最初的暂停人与时间
        at = self._now(run_id)
        store.save_pause(run_id, {"paused_by": actor.actor_id, "paused_at": at.isoformat(), "reason": reason,
                                  "resumed_by": None, "resumed_at": None})
        self._announce("autonomy_paused", actor, at, run_id, f"暂停自动执行：{reason}", {"reason": reason})
        return self.status(run_id)

    def resume(self, actor: Actor, run_id: str | None = None) -> AutonomyStatus:
        self._check_run(run_id)
        store = self.store
        record = store.pause_record(run_id)
        if record is None or record.get("resumed_at"):
            if run_id and store.active_pause(run_id) is not None:
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "该运行是被整厂暂停的，请在整厂层恢复")
            return self.status(run_id)  # 没在暂停：幂等
        at = self._now(run_id)
        store.save_pause(run_id, record | {"resumed_by": actor.actor_id, "resumed_at": at.isoformat()})
        self._announce("autonomy_resumed", actor, at, run_id, "恢复自动执行；积压的提议仍需逐条批准", {})
        return self.status(run_id)

    def _announce(self, name: str, actor: Actor, at: datetime, run_id: str | None, summary: str,
                  params: Mapping[str, Any]) -> None:
        """pause/resume 各写一条证据事件，并进相关运行的记录（回放与日报可见）。"""
        scope = self.store.pause_scope(run_id).replace(":", "-").replace("@", "")
        serial = self.store.next_id("AU")  # 同一分钟的暂停/恢复也各有自己的证据编号
        self._evidence(EvidenceEventType(name), f"{name}-{scope}-{serial}", at, run_id, summary,
                       {"operator": actor.actor_id, "scope": "run" if run_id else "plant", **params})
        runners = [r for r in self._runners() if run_id is None or r.run_info.run_id == run_id]
        for runner in runners:
            self.service._note(runner, name, runner.now, {"operator": actor.actor_id, "summary": summary,
                                                          "scope": "run" if run_id else "plant", **params})

    def inherit_pause(self, parent_run: str, child_run: str) -> None:
        """分叉：运行级暂停交给子运行（整厂暂停本来就覆盖子运行）。"""
        record = self.store.pause_record(parent_run)
        if record is not None and not record.get("resumed_at"):
            self.store.save_pause(child_run, record)

    # ---------------------------------------------------------------- 已下发动作
    def _applied(self, action_id: str, run_id: str | None) -> tuple[Any, dict[str, Any]]:
        if run_id and self.service._runner_for_run(run_id) is None:
            self._check_run(run_id)
            raise PlantkgError(ErrorCode.STATE_CONFLICT, f"运行 {run_id} 已结束或已分叉，动作在当前运行里处理")
        found = []
        for runner in self._runners():
            if run_id and runner.run_info.run_id != run_id:
                continue
            dispatcher = runner.dispatcher
            rec = dispatcher.applied(action_id)[0] if hasattr(dispatcher, "applied") else None
            if rec is not None:
                found.append((runner, rec))
        if not found:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"已下发动作 {action_id} 不存在")
        if len(found) > 1:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, f"动作编号 {action_id} 在多个运行里存在，请带 run_id")
        return found[0]

    @staticmethod
    def _protection(rec: Mapping[str, Any]) -> bool:
        return (rec.get("source") or {}).get("b47") == "protection"

    def _view(self, runner: Any, rec: Mapping[str, Any]) -> AppliedAction:
        source = rec.get("source") or {}
        action = rec["action"]
        return AppliedAction(
            action_id=rec["action_id"], run_id=runner.run_info.run_id, entity_id=action["entity_id"],
            command=action["command"], params=dict(action.get("params") or {}), interval=Interval(
                start=_iso(action["interval"]["start"]), end=_iso(action["interval"]["end"])),
            stage=runner.dispatcher.action_stage(rec, runner.now), protection=self._protection(rec),
            ticket_id=source.get("ticket_id"), proposal_id=source.get("proposal_id"), temp_rule_id=source.get("temp_rule"))

    def applied_actions(self, run_id: str | None = None) -> list[AppliedAction]:
        if run_id:
            self._check_run(run_id)
        return [self._view(runner, rec) for runner in self._runners()
                if run_id is None or runner.run_info.run_id == run_id
                for rec in runner.trace.actions if rec.get("action_id") and hasattr(runner.dispatcher, "action_stage")]

    @staticmethod
    def _intervention(runner: Any, rec: Mapping[str, Any]) -> ActionIntervention:
        item = rec["intervention"]
        return ActionIntervention(
            action_id=rec["action_id"], run_id=runner.run_info.run_id, kind=item["kind"], operator=item["operator"],
            at=_iso(item["at"]), reason_kind=item.get("reason_kind"), note=item.get("note") or "",
            protection=bool(item.get("protection")),
            effective_end=_iso(item["effective_end"]) if item.get("effective_end") else None,
            original_end=_iso(item["original_end"]) if item.get("original_end") else None)

    def cancel(self, action_id: str, actor: Actor, reason: str, run_id: str | None = None,
               confirm_protection: bool = False) -> ActionIntervention:
        runner, rec = self._applied(action_id, run_id)
        with runner.edit_lock:
            stage = runner.dispatcher.action_stage(rec, runner.now)
            if stage == "cancelled":
                return self._intervention(runner, rec)  # 重复取消幂等
            if stage != "scheduled":
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"动作 {action_id} 已是 {stage}，只有还没生效的动作能取消"
                                   + ("；已生效的请用撤销" if stage == "active" else ""))
            protection = self._protection(rec)
            if protection and not confirm_protection:
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "取消保护动作需要二次确认（confirm_protection=true）")
            now = _utc(runner.now)
            item = {"kind": "cancelled", "operator": actor.actor_id, "at": now.isoformat(), "reason_kind": None,
                    "note": reason, "protection": protection, "effective_end": None,
                    "original_end": rec["action"]["interval"]["end"]}
            runner.dispatcher.cancel_action(action_id, item)
            result = self._intervention(runner, rec)
        self._intervention_evidence(runner, result, "取消保护动作" if protection else "取消未生效动作")
        return result

    def revoke(self, action_id: str, actor: Actor, reason_kind: str, note: str, run_id: str | None = None,
               confirm_protection: bool = False) -> ActionIntervention:
        if action_id.startswith("TR-"):
            return self._revoke_rule(action_id, actor, reason_kind, note)
        runner, rec = self._applied(action_id, run_id)
        with runner.edit_lock:
            stage = runner.dispatcher.action_stage(rec, runner.now)
            if stage == "revoked":
                return self._intervention(runner, rec)  # 重复撤销幂等：同一条记录，不重复记账
            if stage != "active":
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"动作 {action_id} 已是 {stage}，只有已生效的动作能撤销"
                                   + ("；还没生效的请用取消" if stage == "scheduled" else ""))
            protection = self._protection(rec)
            if protection:
                if not note.strip():
                    raise PlantkgError(ErrorCode.INVALID_REQUEST, "撤销保护动作必须填写说明")
                if not confirm_protection:
                    raise PlantkgError(ErrorCode.INVALID_REQUEST, "撤销保护动作需要二次确认（confirm_protection=true）")
            now = _utc(runner.now)
            item = {"kind": "revoked", "operator": actor.actor_id, "at": now.isoformat(), "reason_kind": reason_kind,
                    "note": note, "protection": protection, "effective_end": now.isoformat(),
                    "original_end": rec["action"]["interval"]["end"]}
            runner.dispatcher.revoke_action(action_id, now, item)
            result = self._intervention(runner, rec)
        self._intervention_evidence(runner, result, "撤销保护动作" if protection else "撤销已生效动作")
        if reason_kind == "system_wrong":
            self._record_correction(rec, actor, note)
        return result

    def _intervention_evidence(self, runner: Any, item: ActionIntervention, label: str) -> None:
        kind = EvidenceEventType.action_revoked if item.kind == "revoked" else EvidenceEventType.action_cancelled
        self._evidence(kind, f"{item.kind}-{item.run_id}-{item.action_id}", item.at, item.run_id,
                       f"{label}：{item.action_id}（{item.operator}）{item.note}".strip(),
                       {"operator": item.operator, "reason_kind": item.reason_kind or "", "protection": item.protection,
                        "note": item.note[:500]}, action_id=item.action_id)

    def _record_correction(self, rec: Mapping[str, Any], actor: Actor, note: str) -> None:
        """“系统判断有误”计入人工纠错：经 evolution.record_correction 钩子交给 B32（最小样本规则在 B32 里）。

        “其他”不走这里，只留撤销记录；来源不是事件单的动作（如临时规则）没有类别，同样只留记录。
        """
        ticket_id = (rec.get("source") or {}).get("ticket_id")
        if not ticket_id:
            return
        self.service.emit_ticket_feedback(
            ticket_id, "revoke", outcome="rejected", system_error=True, note=note, actor=actor,
            key=f"b47-revoke-{rec['action_id']}")

    # ---------------------------------------------------------------- 临时规则
    def _find_rule(self, rule_id: str) -> Any:
        stored = next((r for r in self.store.temp_rules() if r.rule.rule_id == rule_id), None)
        if stored is None:
            raise PlantkgError(ErrorCode.NOT_FOUND, f"临时规则 {rule_id} 不存在")
        return stored

    def _revoke_rule(self, rule_id: str, actor: Actor, reason_kind: str, note: str) -> ActionIntervention:
        for attempt in (0, 1):  # 撤销与分叉交错时规则行刚好改归子运行：找不到运行就重读一次再找
            stored = self._find_rule(rule_id)
            if stored.status == "cancelled" and stored.intervention:  # 已撤销的规则分叉后留在旧运行里：重复撤销仍返回原记录
                return ActionIntervention.model_validate(stored.intervention)
            runner = self.service._runner_for_run(stored.run_id)
            if runner is not None:
                break
        if runner is None:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, f"临时规则 {rule_id} 所在运行（{stored.run_id}）已结束或不在线，无法撤销")
        with runner.edit_lock:
            stored = self._find_rule(rule_id)  # 拿到锁后重读：并发撤销按先后只生效一次
            if stored.status == "cancelled" and stored.intervention:
                return ActionIntervention.model_validate(stored.intervention)
            now = _utc(runner.now)
            if stored.status != "active" or stored.rule.effective.end <= now:
                raise PlantkgError(ErrorCode.STATE_CONFLICT,
                                   f"临时规则 {rule_id} 已{'失效' if stored.status == 'active' else stored.status}，不能撤销")
            started = stored.rule.effective.start < now  # 还没开始的规则按取消处理：摘掉它还没生效的动作
            kind = "revoked" if started else "cancelled"
            for entry in list(runner.state.sims.get("actions", ())):  # 活动动作表里挂着这条规则的动作
                if (entry.get("source") or {}).get("temp_rule") != rule_id:
                    continue
                rec = runner.dispatcher.applied(entry["action_id"])[0]
                stage = runner.dispatcher.action_stage(rec, runner.now)
                item = {"kind": kind, "operator": actor.actor_id, "at": now.isoformat(),
                        "reason_kind": reason_kind, "note": note, "protection": False,
                        "effective_end": now.isoformat() if stage == "active" else None,
                        "original_end": rec["action"]["interval"]["end"]}
                if stage == "active":
                    runner.dispatcher.revoke_action(rec["action_id"], now, item)
                elif stage == "scheduled":
                    runner.dispatcher.cancel_action(rec["action_id"], item | {"kind": "cancelled"})
            rule = stored.rule.model_copy(update={"effective": Interval(start=stored.rule.effective.start, end=now)}
                                          if started else {})
            result = ActionIntervention(
                action_id=rule_id, run_id=runner.run_info.run_id, kind=kind, operator=actor.actor_id, at=now,
                reason_kind=reason_kind, note=note, effective_end=now if started else None,
                original_end=stored.rule.effective.end)
            self.store.save_temp_rule(stored.model_copy(update={
                "rule": rule, "status": "cancelled", "intervention": result.model_dump(mode="json")}))
            runner.trace.temp_rules[rule_id] = rule
            runner.temp_rules.rules[rule_id] = rule
            if started:  # 场景库里补一条截短后的规则记录：导出/刷新后显示截短后的结束时刻
                runner.db.store.write_records(runner.run_info.run_id, [
                    ("temp_rule", rule_id, rule.created_at, rule.model_dump(mode="json"))])
            else:  # 开始前就取消：导出时跳过这条规则（不改 TempRule 契约）
                runner.db.store.write_records(runner.run_info.run_id, [
                    ("temp_rule_cancelled", rule_id, now, {"rule_id": rule_id, "at": now.isoformat()})])
        self._intervention_evidence(runner, result, "撤销临时规则")
        return result

    # ---------------------------------------------------------------- 预计影响
    def revoke_preview(self, action_id: str, run_id: str | None = None) -> RevokePreview:
        from plantkg.world.scenario.runner import TrialRunner

        runner, rec = self._applied(action_id, run_id)
        with runner.edit_lock:
            stage = runner.dispatcher.action_stage(rec, runner.now)
            if stage != "active":
                raise PlantkgError(ErrorCode.STATE_CONFLICT, f"动作 {action_id} 已是 {stage}，没有可撤销的剩余窗口")
            now = _utc(runner.now)
            window = Interval(start=now, end=_iso(rec["action"]["interval"]["end"]))
            keep_snapshot, revoke_snapshot = runner.snapshot(), runner.snapshot()
        for item in revoke_snapshot.pack_states["world"]["sims"]["actions"]:
            if item.get("action_id") == action_id:
                item["action"]["interval"]["end"] = now.isoformat()
        trial = TrialRunner(runner.world)
        keep = self._outcome(trial.trial(keep_snapshot, (), window))
        revoke = self._outcome(trial.trial(revoke_snapshot, (), window))
        protection = self._protection(rec)
        return RevokePreview(
            action_id=action_id, run_id=runner.run_info.run_id, window=window, revoke=revoke, keep=keep,
            cost_delta_cny=round(revoke.cost_cny - keep.cost_cny, 4),
            violation_minutes_delta=revoke.violation_minutes - keep.violation_minutes,
            t1_peak_delta_kw=round(revoke.t1_peak_kw - keep.t1_peak_kw, 3), protection=protection,
            note="保护动作：撤销需要填写说明并二次确认" if protection else "")

    @staticmethod
    def _outcome(result: Any) -> RevokeOutcome:
        """硬约束违规分钟数（容量逐分钟，硬区域温度只记首分钟）加舒适带越界分钟数；T1 峰值取分钟购电折算的 kW。"""
        minutes = len({violation.at for violation in result.baseline_violations}) + int(result.soft_cost.comfort_min)
        peak = max((entry.grid_import_kwh * 60 for entry in result.minute_ledger), default=0.0)
        return RevokeOutcome(cost_cny=round(result.soft_cost.cny, 4), violation_minutes=minutes, t1_peak_kw=round(peak, 3))
