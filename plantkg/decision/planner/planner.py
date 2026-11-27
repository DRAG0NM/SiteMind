"""共享规划器（B09）：Planner.solve(PlanningRequest) → PlanResult。

流程：取快照/预测/规则 → 编译规则 → 各包按请求顺序 contribute 到同一个 SharedModelBuilder → 汇总唯一平衡与目标
→ HiGHS 求解 → 取整、置零后 validate_solution 独立校验 → 生成 Plan/Action。

状态（契约 SolverStatus，ADR-0001/0008）：
- optimal：HiGHS 报告达到配置 gap；feasible：触及预算但有可行解，附实际 gap；
- timeout：触及预算且无可行解；infeasible：HiGHS 证明不可行，附按包删减得到的冲突线索（不是 IIS，不放宽约束出计划）；
- error：无界、不可行或无界未区分、求解器异常、规则约束对不上或独立校验未通过。
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyomo.environ as pyo
from pyomo.core.expr.visitor import identify_variables

from plantkg.core.contracts import (
    LEDGER_TOLERANCE,
    Action,
    ErrorCode,
    Forecast,
    ForecastBundle,
    Interval,
    PendingIssue,
    Plan,
    PlanningRequest,
    PlanResult,
    PlantkgError,
    Ref,
    Rule,
    SolverBudget,
    SolverInfo,
    StateSnapshot,
)
from plantkg.core.ports import DomainPack
from plantkg.decision.planner.builder import PLANNER_SCOPE, SharedModelBuilder
from plantkg.decision.planner.rules import CompiledRules, RuleCompilerRegistry, check_built

ZERO_KW = LEDGER_TOLERANCE["power_zero_kw"]
INT_TOL = 1e-6  # 整数性容差：|x − round(x)| 超过即判 error
CON_TOL = 1e-5  # 约束违反容差（相对 max(1, |界|)）
BASE_LOAD, PV_AVAILABLE = "base_load", "pv_available"
CAP_REL, CAP_ABS = 1e-7, 1e-6  # 第二阶段费用上限 = 第一阶段费用 + max(CAP_ABS, CAP_REL × |费用|)，只吸收求解器容差
POLISH_MIN_S = 0.5  # 第二阶段是固定整数后的 LP，第一阶段用完预算时也至少给这么多时间
CURTAIL_QUANTITY = "pv_curtail"  # 契约 Action.quantity 中的弃光动作，B08/B17 从 Plan 读到的就是它
_SOLVER_LOCK = threading.RLock()  # Pyomo 建模与求解共用一把进程级可重入锁：模型构建、求解器调用与 stdout 接管都不能并发


# ---------------------------------------------------------------- 求解后端


@dataclass
class SolveOutcome:
    termination: str  # optimal | time_limit | infeasible | unbounded | infeasible_or_unbounded | error
    has_solution: bool  # 已把可行解写回模型变量
    objective: float | None
    bound: float | None
    elapsed_s: float  # 只算实际求解，不含排队
    message: str = ""
    queued_s: float = 0.0  # 等待进程级求解锁的时间


class HighsBackend:
    """HiGHS 的线程调度器在进程内只初始化一次：首次求解的线程数之后不能再改，换值会返回 unknown（2026-10-30 实测）。
    所以进程内锁定首次的线程数，后续请求一律用它，实际值写进 SolverInfo.params["threads_effective"]。
    版本探测、求解与结果加载都在进程级锁 _SOLVER_LOCK 内执行；time_limit 只约束拿到锁之后的求解。"""

    name = "highs"
    _process_threads: int | None = None
    _version: str | None = None

    def effective_threads(self, requested: int) -> int:
        if HighsBackend._process_threads is None:
            HighsBackend._process_threads = requested
        return HighsBackend._process_threads

    def version(self) -> str:
        from pyomo.contrib.solver.common.factory import SolverFactory

        if HighsBackend._version is None:
            with _SOLVER_LOCK:
                HighsBackend._version = ".".join(map(str, SolverFactory("highs").version()))
        return HighsBackend._version

    def solve(self, model: pyo.ConcreteModel, budget: SolverBudget) -> SolveOutcome:
        from pyomo.contrib.solver.common.factory import SolverFactory
        from pyomo.contrib.solver.common.results import SolutionStatus, TerminationCondition as TC

        waited = time.perf_counter()
        with _SOLVER_LOCK:
            start = time.perf_counter()
            queued = start - waited
            try:
                res = SolverFactory("highs").solve(
                    model, time_limit=budget.time_limit_s, rel_gap=budget.mip_rel_gap, threads=self.effective_threads(budget.threads),
                    load_solutions=False, raise_exception_on_nonoptimal_result=False, solver_options={"random_seed": budget.random_seed},
                )
                has_solution = res.solution_status in (SolutionStatus.optimal, SolutionStatus.feasible)
                if has_solution:
                    res.solution_loader.load_vars()
            except Exception as exc:  # 求解器崩溃也要变成 error 结果，不让后台任务挂掉
                return SolveOutcome("error", False, None, None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}", queued)
            elapsed = time.perf_counter() - start
        termination = {
            TC.convergenceCriteriaSatisfied: "optimal", TC.maxTimeLimit: "time_limit", TC.iterationLimit: "time_limit",
            TC.provenInfeasible: "infeasible", TC.locallyInfeasible: "infeasible", TC.unbounded: "unbounded",
            TC.infeasibleOrUnbounded: "infeasible_or_unbounded",
        }.get(res.termination_condition, "error")
        bound = res.objective_bound if res.objective_bound is not None and math.isfinite(res.objective_bound) else None
        return SolveOutcome(termination, has_solution, res.incumbent_objective, bound, elapsed, str(res.termination_condition), queued)


# ---------------------------------------------------------------- 独立校验


def validate_solution(builder: SharedModelBuilder) -> list[str]:
    """不信求解器的状态码：先取整与置零，再逐个检查变量取值、上下界、整数性与全部约束（含公共功率平衡）。"""
    errors: list[str] = []
    m = builder.model
    for var in m.component_data_objects(pyo.Var, descend_into=True):
        if var.value is None:
            continue  # 没进任何约束与目标的变量，求解器不返回取值；生成动作时按 0 处理
        value = float(var.value)
        if not math.isfinite(value):
            errors.append(f"{var.name} 取值 {value} 非有限")
            continue
        if var.is_integer() or var.is_binary():
            if abs(value - round(value)) > INT_TOL:
                errors.append(f"{var.name}={value:.9g} 不满足整数性")
            value = float(round(value))
        elif abs(value) <= ZERO_KW:
            value = 0.0
        var.set_value(value, skip_validation=True)
        lb, ub = var.lb, var.ub
        if (lb is not None and value < lb - CON_TOL * max(1.0, abs(lb))) or (ub is not None and value > ub + CON_TOL * max(1.0, abs(ub))):
            errors.append(f"{var.name}={value:.9g} 超出 [{lb}, {ub}]")
    for con in m.component_data_objects(pyo.Constraint, active=True, descend_into=True):
        body = pyo.value(con.body, exception=False)
        if body is None or not math.isfinite(body):
            errors.append(f"约束 {con.name} 无法求值")
            continue
        lo, up = pyo.value(con.lower), pyo.value(con.upper)
        if lo is not None and body < lo - CON_TOL * max(1.0, abs(lo)):
            errors.append(f"约束 {con.name} 违反：{body:.9g} < {lo:.9g}")
        if up is not None and body > up + CON_TOL * max(1.0, abs(up)):
            errors.append(f"约束 {con.name} 违反：{body:.9g} > {up:.9g}")
    return errors


# ---------------------------------------------------------------- 规划器


class SharedPlanner:
    """实现 ports.Planner。快照、预测、规则按 ID 由注入的查询函数取得（正式实现归 B04/B07/B14/B24）。"""

    def __init__(
        self,
        packs: Mapping[str, DomainPack],
        snapshot_of: Callable[[str], StateSnapshot],
        forecasts_of: Callable[[Sequence[str]], ForecastBundle],
        rules_of: Callable[[Sequence[str]], Sequence[Rule]],
        export_limit_kw: float = 0.0,
        backend: HighsBackend | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        diagnose_infeasible: bool = True,
        export_ref: str = "config://configs/defaults.yaml#planning.export_limit_kw",
    ) -> None:
        self._packs = dict(packs)
        self._snapshot_of, self._forecasts_of, self._rules_of = snapshot_of, forecasts_of, rules_of
        self._export_limit_kw = export_limit_kw
        self._backend = backend or HighsBackend()
        self._clock = clock
        self._diagnose = diagnose_infeasible
        self._export_ref = export_ref
        self.rule_registry = RuleCompilerRegistry(self._packs)
        self.last_builder: SharedModelBuilder | None = None  # 仅供测试与离线复核读取最近一次模型；并发求解会互相覆盖

    def solve(self, request: PlanningRequest) -> PlanResult:
        # 模型构建也必须在同一把锁内：后台任务线程与前台请求同时建 Pyomo 模型/求解会改共享状态
        with _SOLVER_LOCK:
            return self._solve(request, None)

    def solve_with_receipt(self, request: PlanningRequest):
        """Return the result and per-call evidence, without using last_builder."""
        captured = []
        with _SOLVER_LOCK:
            result = self._solve(request, captured)
        return result, captured[0] if captured else None

    def _solve(self, request: PlanningRequest, captured) -> PlanResult:
        ctx = request.context
        time_set = _time_set(request.horizon, request.step_minutes)
        missing = [p for p in request.packs if p not in self._packs]
        if missing or len(set(request.packs)) != len(request.packs):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"规划请求的领域包 {request.packs} 有重复或未装配：{missing}")
        if captured is not None:
            from .receipt import pack_evidence
            fixed_pack_evidence = pack_evidence({name: self._packs[name] for name in request.packs})
        snapshot = self._snapshot_of(request.snapshot_id)
        if snapshot.run_id != ctx.run_id or snapshot.version_manifest_id != ctx.version_manifest_id or snapshot.as_of > ctx.as_of:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "快照的版本清单或时点与请求上下文不一致")
        forecasts = self._forecasts_of(request.forecast_ids)
        series = _planner_series(forecasts, request, ctx.as_of)
        compiled = self.rule_registry.compile(_fetched_rules(self._rules_of(request.rule_ids), request.rule_ids), ctx, request.horizon,
                                              request.packs)

        builder = self.last_builder = SharedModelBuilder(time_set, self._export_limit_kw, series.get(PV_AVAILABLE))
        with builder.scope(PLANNER_SCOPE):
            builder.register_balance("demand", list(series[BASE_LOAD]), BASE_LOAD)
        pending: list[PendingIssue] = list(compiled.pending)
        receipts = {}
        for name in request.packs:
            try:
                with builder.scope(name):
                    receipt = self._packs[name].contribute(builder, snapshot, compiled.by_pack[name], forecasts)
            except PlantkgError:
                raise
            except Exception as exc:  # 包内 Pyomo/校验异常带上包名，便于定位
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"包 {name} 贡献模型失败：{type(exc).__name__}: {exc}") from exc
            builder.audit(name)
            if receipt.pack != name:
                raise PlantkgError(ErrorCode.INTERNAL, f"包 {name} 返回的回执属于 {receipt.pack}")
            receipts[name] = receipt
            pending.extend(receipt.pending_issues)
        builder.finalize()

        params = request.solver_budget.model_dump()
        if hasattr(self._backend, "effective_threads"):
            params["threads_effective"] = self._backend.effective_threads(request.solver_budget.threads)
        info = SolverInfo(name=self._backend.name, version=self._backend.version(), params=params)
        base = dict(solver=info, pending_issues=tuple(pending))
        wiring = check_built(compiled, builder.state.constraints, builder.model)
        if wiring:
            return PlanResult(solver_status="error", elapsed_s=0.0, validation_errors=tuple(wiring), **base)

        out = self._backend.solve(builder.model, request.solver_budget)
        if out.termination == "infeasible":
            started = time.perf_counter()
            hints = [f"HiGHS 证明不可行（{out.message}）；以下为按包删减的线索，不是 IIS"]
            hints += self._conflict_hints(builder, request) if self._diagnose else []
            elapsed = out.elapsed_s + time.perf_counter() - started
            return PlanResult(solver_status="infeasible", elapsed_s=elapsed, conflict_hints=tuple(hints), **base)
        if not out.has_solution:
            status = "timeout" if out.termination == "time_limit" else "error"
            detail = () if status == "timeout" else (f"求解结束于 {out.termination}：{out.message}",)
            return PlanResult(solver_status=status, elapsed_s=out.elapsed_s, validation_errors=detail, **base)
        if out.termination not in ("optimal", "time_limit"):
            return PlanResult(solver_status="error", elapsed_s=out.elapsed_s, validation_errors=(f"求解结束于 {out.termination}",), **base)

        status = "optimal" if out.termination == "optimal" else "feasible"
        bound = out.bound
        if bound is None and status == "optimal" and not builder.has_integer_vars():
            bound = float(pyo.value(builder.primary_cost))  # 纯 LP 达到最优：对偶可行即证明，下界等于目标
        if bound is None:
            return PlanResult(solver_status="error", elapsed_s=out.elapsed_s, **base, validation_errors=(
                f"求解器给出可行解但没有有效下界（{out.message}）；契约要求计划带下界，不用目标值冒充",))
        stage2, stage2_s = self._prefer_less_import(builder, request.solver_budget, out.elapsed_s)
        elapsed = out.elapsed_s + stage2_s
        base["solver"] = info.model_copy(update={"params": {**params, "stage2": stage2}})

        errors = validate_solution(builder)
        if errors:
            # 独立校验未通过时绝不构造动作：坏取值交给 error 结果，不穿出 pydantic 内部异常
            return PlanResult(solver_status="error", elapsed_s=elapsed, validation_errors=tuple(errors[:50]), **base)
        actions, action_errors = _actions(builder, receipts, ctx.plant_id, self._export_ref)
        errors = action_errors + _ledger_rules(actions, builder, request.step_minutes / 60)
        if errors:
            return PlanResult(solver_status="error", elapsed_s=elapsed, validation_errors=tuple(errors[:50]), **base)

        terms = builder.objective_terms()
        objective = float(pyo.value(builder.primary_cost))  # 取整/置零后的原费用目标，与账本费用口径一致
        if not math.isfinite(objective) or any(not math.isfinite(value) for value in terms.values()):
            return PlanResult(solver_status="error", elapsed_s=elapsed, **base,
                              validation_errors=("模型目标值或成本项不是有限数，拒绝生成计划",))
        gap = max(objective - bound, 0.0) / max(abs(objective), 1e-10) if objective - bound > 1e-9 else 0.0
        plan = Plan(
            plan_id=self._plan_id(request, compiled), revision=0, run_id=ctx.run_id, status="draft", solver_status=status,
            valid_interval=request.horizon, version_manifest_id=ctx.version_manifest_id, snapshot_id=request.snapshot_id,
            forecast_ids=request.forecast_ids, policy=request.policy, actions=tuple(actions), objective_terms=terms,
            created_at=self._clock(),
        )
        if captured is not None:
            from .receipt import SolveReceipt, pack_evidence
            if fixed_pack_evidence != pack_evidence({name: self._packs[name] for name in request.packs}):
                raise PlantkgError(ErrorCode.STATE_CONFLICT, "求解期间领域包配置发生变化")
            captured.append(SolveReceipt.create({
                "schema_version": 1,
                "request": request.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
                "forecasts": forecasts.model_dump(mode="json"),
                "rules_by_pack": {name: [rule.model_dump(mode="json") for rule in rules]
                                  for name, rules in compiled.by_pack.items()},
                "contributions": {name: receipt.model_dump(mode="json") for name, receipt in receipts.items()},
                "pack_evidence": fixed_pack_evidence,
                "export_limit_kw": self._export_limit_kw,
                "export_ref": self._export_ref,
                "end_floor_kwh": _end_floor_kwh(builder),
                "solver": info.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
            }))
        return PlanResult(solver_status=status, plan=plan, mip_gap=gap, objective_value=objective, objective_bound=bound,
                          elapsed_s=elapsed, objective_terms=terms, **base)

    def _prefer_less_import(self, builder: SharedModelBuilder, budget: SolverBudget, used_s: float) -> tuple[str, float]:
        """第二阶段（词典序精修）：整数变量固定为第一阶段取值，原费用限制在第一阶段的值以内，再最小化购电电量。

        零电价时段里“用光伏”与“弃光再购电”费用相同，第一阶段可能选后者，B08 会判“弃光同时购电”。固定整数后是 LP，
        第一阶段的解一定可行（本机演示日约 0.06～0.08 s，主要是 Pyomo 重建模型）；预算 max(剩余预算, POLISH_MIN_S)。
        原费用最多比第一阶段高 max(CAP_ABS, CAP_REL×|费用|)，第一阶段的下界仍有效。原费用为常数（无费用项）时不加费用上限。
        构建或求解失败都保留第一阶段的解，是否符合账本口径交给随后的 _ledger_rules 判定。
        返回（ok | no_solution | failed，第二阶段求解耗时，不含排队）。
        """
        m = builder.model
        fixed = [v for v in m.component_data_objects(pyo.Var) if (v.is_integer() or v.is_binary()) and not v.fixed and v.value is not None]
        try:
            for v in fixed:
                v.fix(round(v.value))
            m.objective.deactivate()
            if any(True for _ in identify_variables(builder.primary_cost)):
                cost = float(pyo.value(builder.primary_cost))
                m.stage2_cost_cap = pyo.Constraint(expr=builder.primary_cost <= cost + max(CAP_ABS, CAP_REL * abs(cost)))
            m.stage2_objective = pyo.Objective(expr=sum(m.grid_import[t] for t in m.T), sense=pyo.minimize)
            out = self._backend.solve(m, budget.model_copy(update={"time_limit_s": max(budget.time_limit_s - used_s, POLISH_MIN_S)}))
            return ("ok" if out.has_solution else "no_solution"), out.elapsed_s
        except Exception as exc:  # noqa: BLE001 - 精修失败不影响第一阶段已得到的解
            return f"failed: {type(exc).__name__}", 0.0
        finally:
            for v in fixed:
                v.unfix()

    def _plan_id(self, request: PlanningRequest, compiled: CompiledRules) -> str:
        """同一请求（不含 trace/agent 运行号/幂等键/操作人）、同样的规则内容、外送上限与求解器得到同一 plan_id。"""
        payload = request.model_dump_json(exclude={"context": {"trace_id", "agent_run_id", "idempotency_key", "actor"}})
        rules = sorted(r.model_dump_json() for rs in compiled.by_pack.values() for r in rs)
        extra = f"|{rules}|{self._export_limit_kw}|{self._backend.name}-{self._backend.version()}"
        return f"plan-{request.context.run_id}-{hashlib.sha256((payload + extra).encode()).hexdigest()[:12]}"

    def _conflict_hints(self, builder: SharedModelBuilder, request: PlanningRequest) -> list[str]:
        """逐个停用一个包的全部约束再求解：停用后可行的包就是冲突的参与者。只停用、不放宽，结论只作线索。"""
        m = builder.model
        packs = [p for p in request.packs if builder.pack_constraints(p)]
        total = request.solver_budget.time_limit_s / 2  # 诊断总预算：原预算的一半
        deadline = time.perf_counter() + total
        hints: list[str] = []
        for k, pack in enumerate(packs):
            remaining = deadline - time.perf_counter()
            if remaining <= 0.01:
                hints.append(f"诊断预算 {total:.2f} s 用完，未检查的包：{packs[k:]}")
                break
            per_try = SolverBudget(time_limit_s=remaining / (len(packs) - k), mip_rel_gap=0.5, threads=request.solver_budget.threads,
                                   random_seed=request.solver_budget.random_seed)
            names = builder.pack_constraints(pack)
            for cname in names:
                m.component(cname).deactivate()
            try:
                out = self._backend.solve(m, per_try)
            finally:
                for cname in names:
                    m.component(cname).activate()
            if out.has_solution or out.termination == "optimal":
                refs = sorted({r.id for c in names for r in builder.state.constraints[c].refs})
                hints.append(f"停用 {pack} 包的约束 {names} 后可行；涉及依据 {refs}")
        if not hints:  # 每个包都查过了，没有一个停用后可行
            hints.append("单独停用任一包的约束都不能恢复可行：冲突跨多个包，或在公共功率平衡（如外送上限 "
                         f"{self._export_limit_kw} kW 下供给无处可去）")
        return hints


def _end_floor_kwh(builder: SharedModelBuilder) -> float:
    """本次求解模型里 storage__energy_end 的下界（期末目标，kWh）；没有储能包时为 0。"""
    constraint = builder.model.component("storage__energy_end")
    if constraint is None:
        return 0.0
    return float(pyo.value(constraint.lower))


def _time_set(horizon: Interval, step_minutes: int) -> list[Interval]:
    step = timedelta(minutes=step_minutes)
    n = (horizon.end - horizon.start) / step
    if n != int(n) or n < 1:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"规划区间不能被 {step_minutes} 分钟整除")
    return [Interval(start=horizon.start + i * step, end=horizon.start + (i + 1) * step) for i in range(int(n))]


def _ledger_rules(actions: Sequence[Action], builder: SharedModelBuilder, hours: float) -> list[str]:
    """B08 计划账本里模型约束表达不了的一条：同一时段弃光与购电不能同时超过电量容差。

    弃光量取 Plan 里 quantity=pv_curtail 的动作，与 B08/B17 只凭 Plan 复算时的口径相同，不依赖包另外登记。
    只核对这一条；初始 SOC 等由输入状态决定的问题归 B08 账本本身。
    """
    tol = LEDGER_TOLERANCE["energy_abs_kwh"]
    curtailed_by_slot: dict[int, float] = {}
    for a in actions:
        if a.quantity == CURTAIL_QUANTITY:
            t = builder.time_set.index(a.interval)
            curtailed_by_slot[t] = curtailed_by_slot.get(t, 0.0) + a.value
    errors = []
    m = builder.model
    for t, curtailed in sorted(curtailed_by_slot.items()):
        imported = float(m.grid_import[t].value or 0.0)
        if curtailed * hours > tol and imported * hours > tol:
            errors.append(f"时段 {t} 弃光 {curtailed:.6g} kW 的同时购电 {imported:.6g} kW，B08 账本不认可")
    return errors


def _fetched_rules(rules: Sequence[Rule], requested: Sequence[str]) -> list[Rule]:
    """请求的规则必须一条不少、一条不多地取回；漏取会让规划静默少约束。"""
    rules = list(rules)
    ids = [r.rule_id for r in rules]
    missing = sorted(set(requested) - set(ids))
    if missing:
        raise PlantkgError(ErrorCode.NOT_FOUND, f"请求的规则未取回：{missing}，不能按缺约束的模型规划")
    extra = sorted(set(ids) - set(requested))
    if extra or len(ids) != len(set(ids)):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"规则查询返回了未请求或重复的记录：{extra or ids}")
    return rules


def _planner_series(forecasts: ForecastBundle, request: PlanningRequest, as_of: datetime) -> dict[str, tuple[float, ...]]:
    """只用请求列出的预测；base_load 必须有，同一量不能有两条。"""
    out: dict[str, tuple[float, ...]] = {}
    for f in forecasts.forecasts:
        if f.forecast_id not in request.forecast_ids or f.quantity not in (BASE_LOAD, PV_AVAILABLE):
            continue
        if f.quantity in out:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"请求的预测里 {f.quantity} 不止一条")
        out[f.quantity] = _aligned(f, request, as_of)
    if BASE_LOAD not in out:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "请求的预测里没有 base_load（不可平移负荷），不能把总负荷当 0 规划")
    return out


def _aligned(forecast: Forecast, request: PlanningRequest, as_of: datetime) -> tuple[float, ...]:
    """取预测在规划区间上的值：步长一致、覆盖完整、无缺测、发布时刻对 as_of 可见。"""
    if forecast.issued_at > as_of:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"预测 {forecast.forecast_id} 在 {as_of.isoformat()} 尚未发布")
    if forecast.step_minutes != request.step_minutes:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"预测 {forecast.forecast_id} 步长 {forecast.step_minutes} 与规划 {request.step_minutes} 不同")
    if forecast.unit != "kW":
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"预测 {forecast.forecast_id} 单位 {forecast.unit} 不是 kW")
    if forecast.target.start > request.horizon.start or forecast.target.end < request.horizon.end:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"预测 {forecast.forecast_id} 未覆盖规划区间")
    shift = (request.horizon.start - forecast.target.start) / timedelta(minutes=forecast.step_minutes)
    if shift != int(shift):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"预测 {forecast.forecast_id} 的时段网格与规划区间错位")
    offset = int(shift)
    n = int((request.horizon.end - request.horizon.start) / timedelta(minutes=request.step_minutes))
    values = forecast.values[offset:offset + n]
    if any(v is None for v in values):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"预测 {forecast.forecast_id} 在规划区间内有缺测，不能当 0 规划")
    if any(v < -ZERO_KW for v in values):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"预测 {forecast.forecast_id} 含负值")
    return tuple(max(float(v), 0.0) for v in values)


def _actions(builder: SharedModelBuilder, receipts: Mapping[str, object], plant_id: str, export_ref: str) -> tuple[list[Action], list[str]]:
    """声明了 action 的变量按非零取值生成动作；依据取约束过该变量的 refs，没有则取包回执的 refs。
    外送非零时也生成 grid_export 动作，B08 计划账本只凭 Plan 就能取到外送量。"""
    actions: list[Action] = []
    errors: list[str] = []
    m = builder.model
    for t in m.T:
        value = float(m.grid_export[t].value or 0.0)
        if not math.isfinite(value):
            errors.append(f"外送变量 grid_export[{t}] 取值 {value} 非有限")
            continue
        if value > ZERO_KW:
            actions.append(Action(action_id=f"grid_export[{t}]", object=f"entity://{plant_id}/grid/main", quantity="grid_export",
                                  unit="kW", value=value, interval=builder.time_set[t], refs=(Ref(kind="config", id=export_ref),)))
    for cname, info in builder.state.vars.items():
        if info.action is None:
            continue
        refs = tuple(sorted(builder.state.refs_by_var.get(cname, set()), key=lambda r: (r.kind, r.id))) or tuple(receipts[info.pack].refs)
        if not refs:
            errors.append(f"变量 {cname} 会生成动作，但没有任何约束或回执给出依据")
            continue
        var = m.component(cname)
        for key in (var.index_set() if var.is_indexed() else [None]):
            raw = var[key].value if key is not None else var.value
            if raw is None:
                errors.append(f"动作变量 {cname}[{key}] 没进任何约束或目标，求解器未给取值")
                continue
            value = float(raw)
            if not math.isfinite(value):
                errors.append(f"动作变量 {cname}[{key}] 取值 {value} 非有限")
                continue
            if abs(value) <= ZERO_KW:
                continue
            interval = builder.time_set[key] if key is not None else Interval(start=builder.time_set[0].start, end=builder.time_set[-1].end)
            object_id, quantity = info.action
            actions.append(Action(action_id=f"{cname}[{'' if key is None else key}]", object=object_id, quantity=quantity,
                                  unit=info.unit, value=value, interval=interval, refs=refs))
    return actions, errors
