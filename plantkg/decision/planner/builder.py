"""共享规划模型（B09）：唯一的 ConcreteModel、时间集合、公共购/售电、功率平衡与目标汇总。

包只经 SharedModelBuilder（实现 ports.ModelBuilder）贡献变量、约束、平衡项与成本项（ADR-0001）：
- 变量与约束名自动加包前缀 `{pack}__{name}`，包之间不会撞名；
- 每条约束、平衡项、成本项登记时检查其中的变量，只允许本包变量与公共 grid_import/grid_export，跨包读写直接拒绝；
- 进入功率平衡的变量必须是 kW 或二进制/整数选择变量（如工序 x[s] × 曲线 kW），单位不对的项登记即拒绝；
- 公共功率平衡与目标在 finalize 时由 B09 唯一生成：Σ供给 + 购电 = Σ负荷 + 外送，目标 = Σ成本项。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import pyomo.environ as pyo
from pyomo.core.expr.visitor import identify_variables

from plantkg.core.contracts import EntityId, ErrorCode, Interval, PlantkgError, Ref, Unit

PUBLIC_VARS = ("grid_import", "grid_export")
PLANNER_SCOPE = "planner"  # B09 自己登记的项（不可平移基础负荷）用这个前缀
_DOMAINS = {"continuous": pyo.Reals, "binary": pyo.Binary, "integer": pyo.Integers}
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTITY = re.compile(r"^entity://[A-Za-z0-9_-]+/[a-z0-9_-]+/[A-Za-z0-9_.-]+$")  # 同契约 EntityId


@dataclass(frozen=True)
class VarInfo:
    pack: str
    name: str
    domain: str
    unit: Unit
    index: tuple[int, ...] | None  # None = 时间集合或标量（见 scalar）；否则为时段下标子集（如工序可行开始时段）
    action: tuple[EntityId, str] | None  # (对象, 动作量)；给了就按非零值生成 Action


@dataclass
class _Term:
    pack: str
    label: str
    exprs: list[Any]


@dataclass
class ConstraintInfo:
    pack: str
    name: str
    refs: tuple[Ref, ...]


@dataclass
class _State:
    vars: dict[str, VarInfo] = field(default_factory=dict)  # 组件名 → 信息
    constraints: dict[str, ConstraintInfo] = field(default_factory=dict)
    supply: list[_Term] = field(default_factory=list)
    demand: list[_Term] = field(default_factory=list)
    costs: list[_Term] = field(default_factory=list)
    refs_by_var: dict[str, set[Ref]] = field(default_factory=dict)  # 变量组件名 → 约束它的 refs，供动作依据


class SharedModelBuilder:
    """一次规划一个实例。Planner 用 scope(pack) 包住 pack.contribute，包内调用自动带该包前缀。"""

    def __init__(self, time_set: Sequence[Interval], export_limit_kw: float = 0.0, pv_available_kw: Sequence[float] | None = None) -> None:
        if not time_set:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, "时间集合不能为空")
        if export_limit_kw < 0:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"外送上限 {export_limit_kw} 不能为负")
        self._time_set = tuple(time_set)
        self._pv = None if pv_available_kw is None else tuple(pv_available_kw)
        m = self.model = pyo.ConcreteModel("plantkg_shared")
        m.T = pyo.Set(initialize=range(len(time_set)), ordered=True)
        m.grid_import = pyo.Var(m.T, domain=pyo.NonNegativeReals)
        m.grid_export = pyo.Var(m.T, domain=pyo.NonNegativeReals, bounds=(0.0, export_limit_kw))
        self.state = _State()
        self._public_bounds = self._public_snapshot()
        self._pack: str | None = None
        self._finalized = False

    # ------------------------------------------------------------ ports.ModelBuilder

    @property
    def time_set(self) -> Sequence[Interval]:
        return self._time_set

    @property
    def grid_import(self) -> Any:
        return self.model.grid_import

    @property
    def grid_export(self) -> Any:
        return self.model.grid_export

    @property
    def pv_available(self) -> tuple[float, ...] | None:
        """光伏可发预测（kW，每时段一个），只读；光伏包据此加 使用 + 弃光 = 可发。"""
        return self._pv

    @property
    def total_load(self) -> Any:
        """总负荷表达式 total_load[t]（全部 demand 项之和）。所有包贡献完、finalize 之后才完整，之前访问报错。"""
        if not self._finalized:
            raise PlantkgError(ErrorCode.INTERNAL, "total_load 在所有包贡献完之后才可用")
        return self.model.total_load

    def add_var(
        self, name: str, domain: str, bounds: tuple[float | None, float | None], unit: Unit, *,
        index: Sequence[int] | None = None, scalar: bool = False, action: tuple[EntityId, str] | None = None,
    ) -> Any:
        """默认按时间集合建索引变量；scalar=True 建标量；index=[...] 只为给定时段下标建（ADR-0001 工序只建可行开始时段）。

        index 为空集合表示没有任何候选（如工序没有可行开始时段），直接拒绝，绝不退化成标量。
        """
        pack, cname = self._component_name(name)
        if domain not in _DOMAINS:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"变量 {cname} 的 domain {domain!r} 只能是 continuous/binary/integer")
        try:
            unit = Unit(unit)
        except ValueError as exc:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"变量 {cname} 的单位 {unit!r} 不是契约单位") from exc
        if action is not None and not _ENTITY.match(action[0]):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"变量 {cname} 的动作对象 {action[0]!r} 不是 entity:// 标识")
        if scalar and index is not None:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"变量 {cname} 不能同时是标量又给 index")
        if index is not None and len(index) == 0:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"变量 {cname} 的候选下标为空（例如工序没有可行开始时段），无法建模")
        if scalar:
            var = pyo.Var(domain=_DOMAINS[domain], bounds=bounds)
        elif index is None:
            var = pyo.Var(self.model.T, domain=_DOMAINS[domain], bounds=bounds)
        else:
            idx = sorted(set(index))
            if idx[0] < 0 or idx[-1] >= len(self._time_set):
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"变量 {cname} 的下标超出 {len(self._time_set)} 个时段")
            var = pyo.Var(idx, domain=_DOMAINS[domain], bounds=bounds)
        self.model.add_component(cname, var)
        self.state.vars[cname] = VarInfo(pack, name, domain, unit, None if index is None else tuple(sorted(set(index))), action)
        return var

    def add_constraint(self, name: str, expr: Any, refs: Sequence[Ref]) -> None:
        """expr 可为单个关系式、按时段 t 返回关系式的函数（可返回 Constraint.Skip），或关系式序列。refs 必填。"""
        pack, cname = self._component_name(name)
        refs = tuple(refs)
        if not refs or not all(isinstance(r, Ref) for r in refs):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"约束 {cname} 必须带至少一个 Ref（规则、配置或资产版本）")
        if _is_function(expr):
            con = pyo.Constraint(self.model.T, rule=lambda _m, t: expr(t))
        elif isinstance(expr, (list, tuple)):
            con = pyo.Constraint(range(len(expr)), rule=lambda _m, k: expr[k])
        else:
            con = pyo.Constraint(expr=expr)
        self.model.add_component(cname, con)
        touched = self._check_vars(pack, [c.body for c in con.values()], cname)
        for var_name in touched:
            self.state.refs_by_var.setdefault(var_name, set()).update(refs)
        self.state.constraints[cname] = ConstraintInfo(pack, name, refs)

    def register_balance(self, side: Literal["supply", "demand"], expr: Any, label: str) -> None:
        pack = self._require_scope()
        if side not in ("supply", "demand"):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"平衡项 side {side!r} 只能是 supply 或 demand")
        exprs = self._per_slot(expr, label)
        self._check_vars(pack, exprs, label, power=True)
        getattr(self.state, side).append(_Term(pack, label, exprs))

    def register_cost(self, expr: Any, label: str) -> None:
        """成本项（元）：标量表达式或按时段序列/函数（求和）。目标只由 finalize 汇总，包不设目标。"""
        pack = self._require_scope()
        exprs = self._per_slot(expr, label) if _is_series(expr) else [expr]
        self._check_vars(pack, exprs, label)
        self.state.costs.append(_Term(pack, label, exprs))

    # ------------------------------------------------------------ B09 内部

    @contextmanager
    def scope(self, pack: str) -> Iterator[None]:
        if self._finalized:
            raise PlantkgError(ErrorCode.INTERNAL, "模型已汇总，不能再贡献")
        if not _NAME.match(pack):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"包名 {pack!r} 不合法")
        self._pack = pack
        try:
            yield
        finally:
            self._pack = None

    def finalize(self) -> None:
        """生成唯一的公共功率平衡与唯一目标。只调用一次。"""
        if self._finalized:
            raise PlantkgError(ErrorCode.INTERNAL, "模型只能汇总一次")
        m, st = self.model, self.state
        m.total_load = pyo.Expression(m.T, rule=lambda _m, t: sum(term.exprs[t] for term in st.demand))
        m.power_balance = pyo.Constraint(
            m.T, rule=lambda _m, t: sum(term.exprs[t] for term in st.supply) + m.grid_import[t] == m.total_load[t] + m.grid_export[t])
        m.objective = pyo.Objective(expr=sum(sum(term.exprs) for term in st.costs) if st.costs else 0.0, sense=pyo.minimize)
        self._finalized = True

    @property
    def primary_cost(self) -> Any:
        """原费用目标（各成本项之和），与求解时实际激活哪个目标无关。"""
        return self.model.objective.expr

    def has_integer_vars(self) -> bool:
        return any(info.domain != "continuous" for info in self.state.vars.values())

    def objective_terms(self) -> dict[str, float]:
        """分项目标：键为 {pack}.{label}，值为该项当前解下的金额。"""
        terms: dict[str, float] = {}
        for term in self.state.costs:
            terms[f"{term.pack}.{term.label}"] = terms.get(f"{term.pack}.{term.label}", 0.0) + float(sum(pyo.value(e) for e in term.exprs))
        return terms

    def audit(self, pack: str) -> None:
        """包贡献完后核对：模型上只多了经 builder 登记的组件，公共购/售电的上下界与固定状态没被改。
        包能拿到 Pyomo 对象，直接 model.add_component 或 grid_export.setub 在 Python 里拦不住，所以事后比对。"""
        known = {"T", *PUBLIC_VARS, *self.state.vars, *self.state.constraints}
        extra = [c.name for c in self.model.component_objects(descend_into=False)
                 if c.name not in known and not any(c.name.startswith(k + "_index") for k in known)]
        if extra:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"包 {pack} 绕过 ModelBuilder 直接往模型加了组件 {extra}")
        if self._public_snapshot() != self._public_bounds:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"包 {pack} 改了公共购/售电变量的上下界或固定状态")

    def _public_snapshot(self) -> list[tuple]:
        return [(name, t, v.lb, v.ub, v.fixed) for name in PUBLIC_VARS for t, v in getattr(self.model, name).items()]

    def pack_constraints(self, pack: str) -> list[str]:
        return [cname for cname, info in self.state.constraints.items() if info.pack == pack]

    def _require_scope(self) -> str:
        if self._pack is None:
            raise PlantkgError(ErrorCode.INTERNAL, "只能在 Planner 调用 pack.contribute 期间贡献模型")
        return self._pack

    def _component_name(self, name: str) -> tuple[str, str]:
        pack = self._require_scope()
        if not _NAME.match(name):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"名称 {name!r} 只能用小写字母、数字和下划线")
        cname = f"{pack}__{name}"
        if self.model.find_component(cname) is not None:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{cname} 已存在")
        return pack, cname

    def _per_slot(self, expr: Any, label: str) -> list[Any]:
        n = len(self._time_set)
        if _is_function(expr):
            return [expr(t) for t in range(n)]
        try:
            exprs = [expr[t] for t in range(n)]
        except (KeyError, IndexError, TypeError) as exc:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{label} 必须按 {n} 个时段逐一给出") from exc
        if hasattr(expr, "__len__") and len(expr) != n:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{label} 有 {len(expr)} 项，时段数为 {n}")
        return exprs

    def _check_vars(self, pack: str, exprs: Sequence[Any], label: str, power: bool = False) -> set[str]:
        """只许本包变量与公共购售电；power=True 时还要求是 kW 或选择变量。返回涉及的变量组件名。"""
        touched: set[str] = set()
        for e in exprs:
            if isinstance(e, (int, float)):
                continue
            for v in identify_variables(e, include_fixed=True):
                cname = v.parent_component().name
                touched.add(cname)
                if cname in PUBLIC_VARS:
                    continue
                info = self.state.vars.get(cname)
                if info is None or info.pack != pack:
                    raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{pack} 的 {label} 引用了不属于本包的变量 {cname}")
                if power and info.unit != Unit.kW and info.domain == "continuous":
                    raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{label} 进入功率平衡，但变量 {cname} 单位是 {info.unit.value} 而非 kW")
        return touched


def _is_function(expr: Any) -> bool:
    """普通函数 t → 表达式。Pyomo 表达式与组件本身也可调用（求值），不能当函数。"""
    return callable(expr) and not hasattr(expr, "is_expression_type") and not hasattr(expr, "is_indexed")


def _is_series(expr: Any) -> bool:
    """按时段给出的项：普通函数、列表/元组/数组，或 Pyomo 索引组件。生成器只能读一次，直接拒绝。"""
    if isinstance(expr, Iterator):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, "按时段的项请给列表或函数，不要给生成器")
    if hasattr(expr, "is_indexed"):
        return expr.is_indexed()
    if hasattr(expr, "__len__") and hasattr(expr, "__getitem__"):
        return True
    return _is_function(expr)
