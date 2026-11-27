"""B15 orchestration over a fixed run and the existing B04 action service."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime, timedelta
import logging
from typing import Any

from plantkg.core.contracts import Approval, Context, ErrorCode, EvidenceEvent, GateDecision, PendingAction, Plan, PlantkgError, Role, ToolResult
from plantkg.core.ports import Gate
from plantkg.core.versions import digest, utcnow
from .policy import PolicyMaterials, evaluate_policy
from .repository import PlanRepository

log = logging.getLogger("plantkg.gate")


def _missing_materials(plan: Plan, metadata: Mapping[str, Any]) -> PolicyMaterials:
    raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "计划门控材料提供者尚未配置")


class GateService:
    def __init__(self, writer, plant_id, actions, runs, versions, evidence=None, *, materials_provider=None, clock=utcnow, run_clock=None,
                 execution_basis_provider=None):
        self.repository = PlanRepository(writer, plant_id)
        self.plant_id, self.actions, self.runs, self.versions, self.evidence = plant_id, actions, runs, versions, evidence
        self.materials_provider = materials_provider or _missing_materials
        self.execution_basis_provider = execution_basis_provider
        # B04 invokes apply while holding its own lock. Never hold a B15 lock
        # while consulting B04, or concurrent confirmation/reads can deadlock.
        # Repository transitions use SQLite CAS for coordination instead.
        self.clock, self._lock = clock, nullcontext()
        self.run_clock = run_clock

    def _identity(self, plan: Plan, context: Context | None = None) -> None:
        if context is not None:
            if not context.can_write:
                raise PlantkgError(ErrorCode.NO_CTX, "计划操作需要可信运行上下文")
            if context.plant_id != self.plant_id:
                raise PlantkgError(ErrorCode.CROSS_PLANT, "计划不能跨厂区操作")
            if context.run_id != plan.run_id or context.version_manifest_id != plan.version_manifest_id:
                raise PlantkgError(ErrorCode.PRECONDITION, "计划与上下文的固定运行或清单不一致")
        run = self.runs.get_run(plan.run_id)
        if run.plant_id != self.plant_id or run.version_manifest_id != plan.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "计划与固定运行身份不一致")
        if context is not None and context.as_of != run.as_of:
            raise PlantkgError(ErrorCode.PRECONDITION, "上下文时点与固定运行不一致")

    def _fresh(self, plan: Plan) -> None:
        if self._now(plan) >= plan.valid_interval.end:
            raise PlantkgError(ErrorCode.PRECONDITION, "计划有效区间已过期")

    def _now(self, plan: Plan):
        return self.run_clock(plan.run_id) if self.run_clock is not None else self.clock()

    def _evaluate_with_materials(self, plan: Plan):
        materials = self.materials_provider(plan, self.repository.metadata(plan.plan_id))
        if not isinstance(materials, PolicyMaterials):
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "计划材料提供者返回了无效材料")
        if materials.request is not None:
            fixed = self.runs.get_run(plan.run_id)
            ctx = materials.request.context
            if (ctx.plant_id, ctx.run_id, ctx.version_manifest_id, ctx.as_of) != (
                    self.plant_id, plan.run_id, plan.version_manifest_id, fixed.as_of):
                raise PlantkgError(ErrorCode.PRECONDITION, "规划请求与固定运行身份或时点不一致")
        return evaluate_policy(plan, materials), materials

    def _evaluate(self, plan: Plan):
        return self._evaluate_with_materials(plan)[0]

    def _execution_basis(self, plan, materials):
        if self.execution_basis_provider is None:
            return None
        from .execution_basis import ExecutionBasis, validate_basis
        basis = self.execution_basis_provider(plan, materials)
        if not isinstance(basis, ExecutionBasis):
            raise PlantkgError(ErrorCode.PRECONDITION, "执行依据提供者返回了无效内容")
        with self.repository.writer.read_only() as con:
            row = con.execute("SELECT sha256,record FROM b09_solve_receipts WHERE plant_id=? AND run_id=? AND manifest_id=? AND plan_id=?",
                              (self.plant_id, plan.run_id, plan.version_manifest_id, plan.plan_id)).fetchone()
        if not row or row != (basis.receipt.sha256, basis.receipt.canonical_json):
            raise PlantkgError(ErrorCode.PRECONDITION, "执行依据未绑定已持久化求解回执")
        return validate_basis(plan, materials, basis)

    def _record_gate(self, plan: Plan, decision: GateDecision) -> None:
        if self.evidence is None:
            return
        identity = digest({"plant": self.plant_id, "plan": decision.plan_id, "revision": decision.revision})[:40]
        try:
            self.evidence.append(EvidenceEvent(
                event_id="b15-gate-" + identity, seq=0, occurred_at=decision.assessed_at,
                run_id=plan.run_id, trace_id="b15-" + identity, action_id=decision.pending_action_id,
                event_type="gate_decided", version_manifest_id=plan.version_manifest_id,
                result_status="conflict" if decision.outcome == "rejected" else "ok",
                params={"plan_id": plan.plan_id, "revision": decision.revision, "outcome": decision.outcome,
                        "policy_version": decision.policy_version}, summary="; ".join(decision.reasons)[:2000]))
        except Exception:  # The authoritative decision is already durable.
            log.exception("门控决定证据投影失败: %s@%s", plan.plan_id, decision.revision)

    def register(self, plan: Plan, context: Context, metadata: Mapping[str, Any] | None = None) -> Plan:
        self._identity(plan, context)
        return self.repository.register(plan, dict(metadata or {}))

    def load(self, plan_id: str, revision: int | None = None) -> Plan:
        with self._lock:
            plan = self.repository.load(plan_id)
            if plan.status == "review_required":
                action_id = self.repository.action_for(plan_id, plan.revision)
                if action_id:
                    action = self.actions.get(action_id)
                    if action.status in ("rejected", "expired"):
                        try:
                            plan = self.repository.transition(plan_id, plan.revision, "rejected")
                        except PlantkgError as exc:
                            if exc.code != ErrorCode.REVISION_CONFLICT:
                                raise
                            plan = self.repository.load(plan_id)
                    elif action.status == "confirmed":
                        plan = self._finalize(plan, action)
            return plan if revision is None or revision == plan.revision else self.repository.load(plan_id, revision)

    def _finalize(self, plan, action):
        request = {"target_ref": action.target_ref, "actor": action.decided_by, "reason": action.reason}
        staged = self.repository.result("confirm", action.action_id, request)
        if (action.kind != "plan_confirmation" or action.plant_id != self.plant_id or
                not staged or staged["review_revision"] != plan.revision or
                action.target_ref != f"{plan.plan_id}@{plan.revision}" or
                action.target_revision != plan.revision or staged["actor"] != action.decided_by or
                staged["reason"] != action.reason):
            raise PlantkgError(ErrorCode.PRECONDITION, "确认结果与计划不一致")
        approval = Approval(plan_id=plan.plan_id, revision=plan.revision + 1, mode="manual", actor=action.decided_by,
                            approved_at=staged["approved_at"], action_id=action.action_id, reason=action.reason)
        try:
            return self.repository.transition(plan.plan_id, plan.revision, "confirmed", approval=approval,
                                              execution_basis=staged.get("execution_basis"))
        except PlantkgError as exc:
            if exc.code != ErrorCode.REVISION_CONFLICT:
                raise
            current = self.repository.load(plan.plan_id)
            stored = self.repository.approval(plan.plan_id, current.revision)
            if current.status != "confirmed" or stored != approval:
                raise
            return current

    def bind(self, context: Context):
        return BoundGate(self, context)

    def preview_inputs(self, context: Context):
        if context.plant_id != self.plant_id or not context.run_id or not context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "预演计划需要固定厂区、运行和清单")
        run = self.runs.get_run(context.run_id)
        if run.version_manifest_id != context.version_manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "预演计划与运行清单不匹配")
        return self.repository.preview_inputs(run.run_id, run.version_manifest_id)

    def assess(self, plan_id: str, expected_revision: int, context: Context) -> GateDecision:
        with self._lock:
            plan = self.load(plan_id)
            self._identity(plan, context)
            if plan.revision != expected_revision:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "计划修订已变化", actual_revision=plan.revision)
            self._fresh(plan)
            if plan.status == "review_required":
                action_id = self.repository.action_for(plan_id, plan.revision)
                if not action_id or self.actions.get(action_id).status != "proposed":
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "确认动作已不再待审")
                decision = self.repository.decision(plan_id, plan.revision)
                self._record_gate(plan, decision)
                return decision
            if plan.status != "draft":
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "计划状态不允许重新评估")
            result, materials = self._evaluate_with_materials(plan)
            if result.outcome == "review_required":
                basis = self._execution_basis(plan, materials) if not result.confirmation_blockers else None
                _, decision = self._propose(plan, context, "门控需要人工确认", result, basis)
                return decision
            decision = GateDecision(plan_id=plan_id, revision=plan.revision + 1, outcome=result.outcome,
                                    reasons=result.reasons, thresholds=result.thresholds, policy_version=result.policy_version,
                                    policy_outcome=result.outcome, assessed_at=self._now(plan))
            approval = Approval(plan_id=plan_id, revision=plan.revision + 1, mode="auto", approved_at=self._now(plan)) if result.outcome == "auto_confirmed" else None
            basis = self._execution_basis(plan, materials) if approval else None
            self.repository.transition(plan_id, plan.revision, result.outcome, decision=decision,
                                       policy_snapshot=result.thresholds, approval=approval, execution_basis=basis)
            self._record_gate(plan, decision)
            return decision

    def _propose(self, plan: Plan, context: Context, rationale: str, result, execution_basis=None, reasons=None):
        if not isinstance(rationale, str) or not rationale.strip():
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "提议理由不能为空")
        recorded_reasons = result.reasons if reasons is None else tuple(reasons)
        # Reserve the complete intent before the B04 write. Its deterministic key
        # permits recovery after either write without producing a second action.
        intent = self.repository.intent(plan.plan_id, plan.revision)
        if intent is None:
            intent = {"rationale": rationale, "reasons": list(recorded_reasons), "thresholds": result.thresholds,
                      "policy_version": result.policy_version, "assessed_at": self._now(plan).isoformat(),
                      "execution_basis": execution_basis}
            try:
                intent = self.repository.reserve_intent(plan.plan_id, plan.revision, intent)
            except PlantkgError as exc:
                existing = self.repository.intent(plan.plan_id, plan.revision)
                if exc.code != ErrorCode.IDEMPOTENCY_MISMATCH or not existing or existing["rationale"] != rationale:
                    raise
                intent = existing
        elif intent["rationale"] != rationale:
            raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "同一计划修订的提议理由不同")
        target_revision = plan.revision + 1
        key = "b15-" + digest({"plant": self.plant_id, "plan": plan.plan_id, "revision": plan.revision})[:40]
        action = self.actions.propose("plan_confirmation", {"target_ref": f"{plan.plan_id}@{target_revision}",
                                      "target_revision": target_revision, "rationale": rationale},
                                      context.model_copy(update={"idempotency_key": key}))
        decision = GateDecision(plan_id=plan.plan_id, revision=target_revision, outcome="review_required",
                                reasons=tuple(intent["reasons"]), thresholds=intent["thresholds"],
                                policy_version=intent["policy_version"], policy_outcome=result.outcome,
                                assessed_at=intent["assessed_at"], pending_action_id=action.action_id)
        try:
            self.repository.transition(plan.plan_id, plan.revision, "review_required", decision=decision,
                                       policy_snapshot=intent["thresholds"], action_id=action.action_id)
        except PlantkgError as exc:
            current = self.repository.load(plan.plan_id)
            if (exc.code != ErrorCode.REVISION_CONFLICT or current.status != "review_required" or
                    current.revision != target_revision or self.repository.action_for(plan.plan_id, current.revision) != action.action_id):
                raise
            decision = self.repository.decision(plan.plan_id, current.revision)
        self._record_gate(plan, decision)
        return action, decision

    def propose(self, plan_id: str, expected_revision: int, rationale: str, context: Context):
        with self._lock:
            plan = self.load(plan_id)
            self._identity(plan, context)
            self._fresh(plan)
            user_request = {"plan_id": plan_id, "expected_revision": expected_revision, "rationale": rationale}
            key = context.idempotency_key or "auto-" + digest({"request": user_request, "agent_run": context.agent_run_id,
                                                               "trace": context.trace_id})[:40]
            prior = self.repository.result("proposal-key", key, user_request)
            if plan.status == "rejected" and (expected_revision == plan.revision or
                                               (expected_revision == plan.revision - 1 and prior is not None)):
                decision = self.repository.decision(plan_id, plan.revision)
                if decision is not None and decision.outcome == "rejected":
                    return decision
            if plan.status == "review_required" and (expected_revision == plan.revision or
                                                       (expected_revision == plan.revision - 1 and prior is not None)):
                intent = self.repository.intent(plan_id, plan.revision - 1)
                if not intent or intent["rationale"] != rationale:
                    raise PlantkgError(ErrorCode.IDEMPOTENCY_MISMATCH, "同一计划修订的提议理由不同")
                action_id = self.repository.action_for(plan_id, plan.revision)
                action = self.actions.get(action_id)
                if action.status != "proposed":
                    raise PlantkgError(ErrorCode.STATE_CONFLICT, "确认动作已不再待审")
                self.repository.save_result("proposal-key", key, user_request, {"plan_id": plan_id})
                return action
            if expected_revision != plan.revision:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "计划修订已变化", actual_revision=plan.revision)
            if plan.status != "draft":
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "计划状态不允许提议")
            if not isinstance(rationale, str) or not rationale.strip():
                raise PlantkgError(ErrorCode.INVALID_REQUEST, "提议理由不能为空")
            self.repository.save_result("proposal-key", key, user_request, {"plan_id": plan_id})
            result, materials = self._evaluate_with_materials(plan)
            if result.outcome == "rejected":
                decision = GateDecision(plan_id=plan_id, revision=plan.revision + 1, outcome="rejected",
                                        reasons=result.reasons, thresholds=result.thresholds,
                                        policy_version=result.policy_version, policy_outcome=result.outcome,
                                        assessed_at=self._now(plan))
                try:
                    self.repository.transition(plan_id, plan.revision, "rejected", decision=decision,
                                               policy_snapshot=result.thresholds)
                except PlantkgError as exc:
                    current = self.repository.load(plan_id)
                    if exc.code != ErrorCode.REVISION_CONFLICT or current.revision != plan.revision + 1 or current.status != "rejected":
                        raise
                    decision = self.repository.decision(plan_id, current.revision)
                    if decision is None or decision.outcome != "rejected":
                        raise
                self._record_gate(plan, decision)
                return decision
            basis = self._execution_basis(plan, materials) if not result.confirmation_blockers else None
            reasons = ("Agent 提议，等待人工确认",)
            if result.outcome == "review_required":
                reasons += result.reasons
            action, _ = self._propose(plan, context, rationale, result, basis, reasons)
            return action

    def apply(self, action, actor) -> ToolResult:
        with self._lock:
            if action.kind != "plan_confirmation" or action.plant_id != self.plant_id or action.decided_by != actor.actor_id:
                raise PlantkgError(ErrorCode.PRECONDITION, "确认动作身份不匹配")
            if Role.reviewer.value not in actor.roles:
                raise PlantkgError(ErrorCode.FORBIDDEN, "计划确认需要复核员角色")
            target = action.target_ref or ""
            plan_id, sep, revision_text = target.rpartition("@")
            if not sep or not revision_text.isdecimal() or int(revision_text) != action.target_revision:
                raise PlantkgError(ErrorCode.PRECONDITION, "动作目标修订无效")
            request = {"target_ref": target, "actor": actor.actor_id, "reason": action.reason}
            prior = self.repository.result("confirm", action.action_id, request)
            plan = self.load(plan_id)
            self._identity(plan)
            if action.proposed_by.run_id != plan.run_id:
                raise PlantkgError(ErrorCode.PRECONDITION, "确认动作与固定运行不一致")
            self._fresh(plan)
            if plan.revision != action.target_revision or plan.status != "review_required" or self.repository.action_for(plan_id, plan.revision) != action.action_id:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "动作对应的计划修订已变化")
            result, materials = self._evaluate_with_materials(plan)
            if result.outcome == "rejected" or result.confirmation_blockers:
                raise PlantkgError(ErrorCode.PRECONDITION, "; ".join(result.confirmation_blockers or result.reasons))
            basis = self._execution_basis(plan, materials)
            intent = self.repository.intent(plan_id, plan.revision - 1)
            original_basis = intent.get("execution_basis") if intent else None
            if basis != original_basis:
                raise PlantkgError(ErrorCode.PRECONDITION, "确认时执行依据已变更，必须重新审核")
            if prior is not None:
                if prior.get("execution_basis") != basis:
                    raise PlantkgError(ErrorCode.PRECONDITION, "已暂存确认的执行依据不一致")
                return ToolResult(ok=True, business_status="done", data={k: v for k, v in prior.items() if k != "execution_basis"})
            data = {"plan_id": plan_id, "review_revision": plan.revision, "confirmed_revision": plan.revision + 1,
                    "actor": actor.actor_id, "reason": action.reason, "approved_at": action.decided_at.isoformat(),
                    "execution_basis": basis}
            self.repository.save_result("confirm", action.action_id, request, data)
            return ToolResult(ok=True, business_status="done", data={k: v for k, v in data.items() if k != "execution_basis"})

    def executable(self, plan_id: str, revision: int | None = None) -> Plan:
        with self._lock:
            plan = self.load(plan_id)
            self._identity(plan)
            self._fresh(plan)
            if revision is not None and revision != plan.revision:
                raise PlantkgError(ErrorCode.REVISION_CONFLICT, "请求的计划修订不是当前修订")
            approval = self.repository.approval(plan_id, plan.revision)
            if plan.status not in ("confirmed", "auto_confirmed") or not approval:
                raise PlantkgError(ErrorCode.PRECONDITION, "计划缺少有效批准")
            if approval.mode == "manual":
                action = self.actions.get(approval.action_id)
                if (action.status != "confirmed" or action.decided_by != approval.actor or
                        action.target_ref != f"{plan_id}@{plan.revision - 1}"):
                    raise PlantkgError(ErrorCode.PRECONDITION, "动作确认与批准不一致")
            elif plan.status != "auto_confirmed":
                raise PlantkgError(ErrorCode.PRECONDITION, "批准模式与计划状态不一致")
            return plan

    def require_executable_in_transaction(self, con, plan_id, revision, run_id, manifest_id, instant):
        """Recheck persisted approval at the committed replay cursor, without writes."""
        if not con.in_transaction:
            raise PlantkgError(ErrorCode.PRECONDITION, "需要调用方事务")
        if not isinstance(instant, datetime) or instant.utcoffset() != timedelta(0):
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "需要 UTC 槽起点")
        plan = self.repository._load(con, plan_id)
        if type(revision) is not int or plan.revision != revision:
            raise PlantkgError(ErrorCode.REVISION_CONFLICT, "请求的计划修订不是当前修订")
        if plan.plan_id != plan_id or plan.run_id != run_id or plan.version_manifest_id != manifest_id:
            raise PlantkgError(ErrorCode.PRECONDITION, "计划与固定运行或清单不一致")
        run = self.runs._load(con, run_id)[0]
        if run.plant_id != self.plant_id or run.version_manifest_id != manifest_id or run.status not in ("created", "running"):
            raise PlantkgError(ErrorCode.PRECONDITION, "运行身份或状态不匹配")
        row = con.execute("SELECT instant, manifest_id FROM replay_clock_history WHERE plant_id=? AND run_id=? ORDER BY seq DESC LIMIT 1", (self.plant_id, run_id)).fetchone()
        if not row or row[1] != manifest_id or datetime.fromisoformat(row[0]) != instant:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "回放游标已变化")
        if (instant < plan.valid_interval.start or instant + timedelta(minutes=15) > plan.valid_interval.end
                or instant < run.clock.start or instant + timedelta(minutes=15) > run.clock.end
                or (instant - run.clock.start) % timedelta(minutes=15)):
            raise PlantkgError(ErrorCode.PRECONDITION, "候选槽不在计划有效期内")
        row = con.execute("SELECT action_id,record FROM b15_approvals WHERE plant_id=? AND plan_id=? AND revision=?", (self.plant_id, plan_id, revision)).fetchone()
        if plan.status not in ("confirmed", "auto_confirmed") or not row:
            raise PlantkgError(ErrorCode.PRECONDITION, "计划缺少有效批准")
        approval = Approval.model_validate_json(row[1])
        if approval.plan_id != plan_id or approval.revision != revision or approval.action_id != row[0]:
            raise PlantkgError(ErrorCode.PRECONDITION, "批准身份不一致")
        if approval.mode == "manual" and plan.status == "confirmed":
            action_row = con.execute("SELECT status,record FROM pending_actions WHERE plant_id=? AND action_id=?", (self.plant_id, approval.action_id)).fetchone()
            if not action_row:
                raise PlantkgError(ErrorCode.PRECONDITION, "确认动作不存在")
            action = PendingAction.model_validate_json(action_row[1])
            if (action_row[0] != "confirmed" or action.status != "confirmed" or action.kind != "plan_confirmation"
                    or action.plant_id != self.plant_id or action.action_id != approval.action_id
                    or action.decided_by != approval.actor or action.target_ref != f"{plan_id}@{revision - 1}"
                    or action.target_revision != revision - 1):
                raise PlantkgError(ErrorCode.PRECONDITION, "动作确认与批准不一致")
        elif approval.mode != "auto" or plan.status != "auto_confirmed":
            raise PlantkgError(ErrorCode.PRECONDITION, "批准模式与计划状态不一致")
        return plan

    def require_execution_snapshot_in_transaction(self, con, plan_id, revision, run_id, manifest_id, instant):
        """Require both current approval and its immutable execution evidence."""
        plan = self.require_executable_in_transaction(con, plan_id, revision, run_id, manifest_id, instant)
        row = con.execute("SELECT record FROM b15_approvals WHERE plant_id=? AND plan_id=? AND revision=?",
                          (self.plant_id, plan_id, revision)).fetchone()
        if row is None:
            raise PlantkgError(ErrorCode.PRECONDITION, "计划缺少批准")
        approval = Approval.model_validate_json(row[0])
        return self.repository.execution_snapshot_in_transaction(con, plan, approval)

    def proposal_tool(self, context: Context, plan_id: str, expected_revision: int, rationale: str) -> ToolResult:
        action = self.propose(plan_id, expected_revision, rationale, context)
        if isinstance(action, GateDecision):
            return ToolResult(ok=True, business_status="done", data={"plan_id": plan_id, "status": "rejected",
                              "revision": action.revision, "reasons": list(action.reasons)}, run_id=context.run_id,
                              version_manifest_id=context.version_manifest_id)
        return ToolResult(ok=True, business_status="done", data={"action_id": action.action_id, "status": action.status,
                          "target_ref": action.target_ref, "revision": action.revision}, run_id=context.run_id,
                          version_manifest_id=context.version_manifest_id)


class BoundGate:
    def __init__(self, service: GateService, context: Context | None):
        self.service, self.context = service, context

    def assess(self, plan: Plan, forecast_quality, rule_status, baseline):
        if self.context is None:
            raise PlantkgError(ErrorCode.NO_CTX, "门控须先绑定固定运行上下文")
        stored = self.service.load(plan.plan_id)
        if stored != plan:
            raise PlantkgError(ErrorCode.STATE_CONFLICT, "传入计划与持久化计划不一致")
        return self.service.assess(plan.plan_id, plan.revision, self.context)

    def preview_inputs(self, context: Context):
        return self.service.preview_inputs(context)


def install(registry, writer, plant_id, actions, runs, versions, evidence=None, *, run_clock=None):
    service = GateService(writer, plant_id, actions, runs, versions, evidence, run_clock=run_clock)
    registry.register(Gate, BoundGate(service, None))
    registry.register_action_handler("plan_confirmation", service)
    registry.register_tool("propose_plan_confirmation", service.proposal_tool)
    return service
