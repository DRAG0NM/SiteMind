"""tariff 领域包（B12）：分时电价规则 → 96 时段价格 → 购电费用目标项，附缺段/重叠/类别冲突诊断。

价格 = 计价基础（元/kWh，按月）× 规则系数。江苏 2025 新版以“用户购电价格”为平段、峰谷按比例浮动，用户购电价格按月公布，
不在政策原文里，所以两者分开登记：
- 规则 tariff_tou 是一份完整的分时时段表（某用电类别、某版本）：value 为平段相对计价基础的系数（新版为 1），
  condition.periods 给每个时段的月份、当地时间窗口（可跨午夜，如 22:00-02:00）、浮动比例与原文定位；
  尖峰/深谷是叠加时段，比例相对峰/谷段价格；尖峰按月份、须落在峰段内；深谷按节日日期（dates，国家公布的放假日），
  原文写“在谷段电价基础上下浮”，春节在冬季、10:00-14:00 跨平段，所以深谷不要求落在谷段内；
  节日日期只登记到 through（已公布的最后一年年末），之后的时段拒绝定价，不按普通谷段计；
- 计价基础是厂区的受信配置（TariffPack.base_prices，键为当地年月 YYYY-MM），缺月份即缺价；
- 编译时整表校验：每个月 24 小时被峰/平/谷恰好覆盖一次，尖峰落在峰段内，系数非负；
- 建模：每时段一个费用变量 cost[t] = 价格 × 购电 × Δt，约束名带规则 ID，目标项为 tariff.energy_cost；
- 价格只来自已审核、可见的规则（B09 筛选），任何时段缺价或被两版规则同时覆盖都拒绝建模，不当 0、不任取其一；
- 只核算已定义的分时电度电费；峰值只作指标单独报告，不当作月度需量电费。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyomo.environ as pyo

from plantkg.core.contracts import (
    ConstraintContribution,
    Context,
    ContributionReceipt,
    ErrorCode,
    ForecastBundle,
    Interval,
    PendingIssue,
    PlantkgError,
    Ref,
    Rule,
    StateSnapshot,
    Unit,
)
from plantkg.core.ports import ModelBuilder
from plantkg.packs._validation import is_number, read_yaml

PACK = "tariff"
TOU_KIND = "tariff_tou"
QUANTITY = "tou_factor"  # 规则给的是相对计价基础的系数，价格由计价基础配置乘出
COMPILER_VERSION = "b12-tariff-2"
BASE_PERIODS = ("peak", "flat", "valley")
OVERLAYS = {"sharp": "peak", "deep_valley": "valley"}  # 叠加时段 → 计价所依的基础时段
DATED = {"deep_valley"}  # 按节日日期给出、不要求落在所依基础时段内的叠加时段；其余叠加时段按月份给出且须落在所依时段内
DAY_MIN = 24 * 60
# 工程上界（configured）：现行峰谷浮动不到 2 倍，系数上界取 10；逐时段价格上界远高于任何真实电价。
MAX_FACTOR = 10.0
MAX_PRICE_CNY_PER_KWH = 100.0
_HHMM = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _minute(text: Any) -> int | None:
    m = _HHMM.match(str(text))
    if not m or (m.group(1) == "24" and m.group(2) != "00"):
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _date(text: Any) -> date | None:
    if not isinstance(text, str) or not _DATE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class Schedule:
    """编译后的时段表：月份 / 节日日期 → 每分钟（当地）的 (时段名, 系数)。through：节日日历登记到哪天，之后无定义。"""

    rule_id: str
    tz: ZoneInfo
    by_month: Mapping[int, tuple[tuple[str, float], ...]]
    by_date: Mapping[date, tuple[tuple[str, float], ...]] = field(default_factory=lambda: MappingProxyType({}))
    through: date | None = None

    def at(self, local: datetime) -> tuple[str, float] | None:
        if self.through is not None and local.date() > self.through:
            return None
        table = self.by_date.get(local.date()) or self.by_month.get(local.month)
        return None if table is None else table[local.hour * 60 + local.minute]


class _Bad(Exception):
    """parse_schedule 内部：(待处理原因, 描述)。"""


def parse_schedule(rule: Rule) -> Schedule | tuple[str, str]:
    """整表校验并展开到分钟。不合格返回 (待处理原因, 描述)：缺段 missing_price、负系数 negative_price、其余 conflict。"""
    cond = rule.condition
    try:
        if not isinstance(cond.get("timezone"), str):
            raise ValueError
        tz = ZoneInfo(cond["timezone"])
    except (ZoneInfoNotFoundError, ValueError, OSError):  # Windows 上 "Asia" 等目录名、超长名会抛 OSError
        return "conflict", f"时区 {cond.get('timezone')!r} 无法识别"
    flat = rule.value
    if not is_number(flat) or flat <= 0:
        return "negative_price", f"平段系数 value={flat!r} 须为正有限数"
    periods = cond.get("periods")
    if not isinstance(periods, list) or not periods:
        return "missing_price", "condition.periods 为空（缺段）"
    grid: dict[int, list[list[str]]] = {}  # 月 → 每分钟命中的时段名
    holidays: dict[date, list[list[str]]] = {}  # 节日日期 → 每分钟命中的叠加时段名
    throughs: list[date] = []
    ratios: dict[str, float] = {}
    for k, p in enumerate(periods):
        # 先校验类型再做集合/字典操作：B25 抽取可能给出 name: ["peak"]、months: [[7]]，须转待处理而不是抛 TypeError
        if not isinstance(p, dict) or set(p) - {"name", "months", "dates", "through", "windows", "ratio", "locator"}:
            return "conflict", f"periods[{k}] 只能有 name/months/dates/through/windows/ratio/locator，实际 {p!r}"
        name, months, windows, ratio = p.get("name"), p.get("months"), p.get("windows"), p.get("ratio")
        if not isinstance(name, str) or (name not in BASE_PERIODS and name not in OVERLAYS):
            return "conflict", f"periods[{k}] 时段名 {name!r} 不是 {BASE_PERIODS + tuple(OVERLAYS)} 之一"
        if not is_number(ratio) or ratios.get(name, ratio) != ratio:
            return "conflict", f"periods[{k}] {name} 的浮动比例 {ratio!r} 非有限数或与同名时段不一致"
        ratios[name] = ratio
        if not p.get("locator"):
            return "conflict", f"periods[{k}] {name} 没有原文定位 locator"
        if "dates" in p:
            if name not in DATED or "months" in p:
                return "conflict", f"periods[{k}] 只有深谷按节日日期 dates 给出，且不能同时给 months"
            dates, through = p["dates"], _date(p.get("through"))
            days = [_date(d) for d in dates] if isinstance(dates, (list, tuple)) else [None]
            if not days or None in days or len(set(days)) != len(days) or through is None or max(days) > through:
                return "conflict", (f"periods[{k}] {name} 的 dates {dates!r} 须是不重复的 \"YYYY-MM-DD\" 字符串列表（YAML 里须加引号），"
                                    f"through {p.get('through')!r} 须为不早于最后一天的 \"YYYY-MM-DD\"")
            throughs.append(through)
            targets = [holidays.setdefault(d, [[] for _ in range(DAY_MIN)]) for d in days]
        else:
            if name in DATED or "through" in p:
                return "conflict", f"periods[{k}] {name} 须按节日日期 dates 给出；through 只能随 dates 出现" if name in DATED                     else f"periods[{k}] {name} 没有 dates，不能给 through"
            if (not isinstance(months, (list, tuple)) or not months
                    or any(isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12 for m in months)
                    or len(set(months)) != len(months)):
                return "conflict", f"periods[{k}] {name} 的月份 {months!r} 须是 1～12 的不重复整数列表"
            targets = [grid.setdefault(m, [[] for _ in range(DAY_MIN)]) for m in months]
        if not isinstance(windows, (list, tuple)) or not windows:
            return "conflict", f"periods[{k}] {name} 没有时间窗口"
        for w in windows:
            lo, hi = (_minute(w[0]), _minute(w[1])) if isinstance(w, (list, tuple)) and len(w) == 2 else (None, None)
            if lo is None or hi is None or lo == hi or lo == DAY_MIN:
                return "conflict", f"periods[{k}] {name} 的窗口 {w!r} 须为 [\"HH:MM\", \"HH:MM\"] 且起止不同（YAML 里须加引号，否则 22:00 会读成整数）"
            span = range(lo, hi) if lo < hi else [*range(lo, DAY_MIN), *range(0, hi)]  # 起点晚于终点即跨午夜
            for minutes in targets:
                for t in span:
                    minutes[t].append(name)

    def build(label: str, minutes: list[list[str]]) -> tuple[tuple[str, float], ...]:
        table = []
        for t, names in enumerate(minutes):
            hhmm = f"{label} {t // 60:02d}:{t % 60:02d}"
            bases = [n for n in names if n in BASE_PERIODS]
            overlays = [n for n in names if n in OVERLAYS]
            if not bases:
                raise _Bad("missing_price", f"{hhmm} 没有峰/平/谷时段（缺段）")
            if len(bases) > 1 or len(overlays) > 1:
                raise _Bad("conflict", f"{hhmm} 同时落在 {names}（重叠）")
            factor, name = flat * (1 + ratios[bases[0]]), bases[0]
            if overlays:
                over, parent = overlays[0], OVERLAYS[overlays[0]]
                if over not in DATED and parent != bases[0]:
                    raise _Bad("conflict", f"{hhmm} 的 {over} 须落在 {parent} 段内，实际在 {bases[0]}")
                if parent not in ratios:
                    raise _Bad("conflict", f"{hhmm} 的 {over} 以 {parent} 段电价为基础，但规则没有 {parent} 段")
                factor, name = flat * (1 + ratios[parent]) * (1 + ratios[over]), over
            if factor < 0:
                raise _Bad("negative_price", f"{hhmm} {name} 系数 {factor:.6g} 为负")
            if not math.isfinite(factor) or factor > MAX_FACTOR:
                raise _Bad("conflict", f"{hhmm} {name} 系数 {factor!r} 超出工程范围 [0, {MAX_FACTOR}]：检查 flat_factor 与各段 ratio")
            table.append((name, factor))
        return tuple(table)

    try:
        by_month = {month: build(f"{month} 月", minutes) for month, minutes in sorted(grid.items())}
        by_date = {}
        for day, extra in sorted(holidays.items()):
            if day.month not in grid:
                raise _Bad("missing_price", f"节日 {day} 所在月份没有峰/平/谷时段（缺段）")
            by_date[day] = build(str(day), [a + b for a, b in zip(grid[day.month], extra)])
    except _Bad as bad:
        return bad.args
    return Schedule(rule_id=rule.rule_id, tz=tz, by_month=by_month, by_date=MappingProxyType(by_date),
                    through=min(throughs, default=None))


# ---------------------------------------------------------------- 规则


class TouScheduleKind:
    """tariff_tou：对象为本厂区电网接入点，quantity=tou_factor、单位 1，用电类别须与本厂区一致。"""

    kind = TOU_KIND
    compiler_version = COMPILER_VERSION

    def __init__(self, grid_entity: str, category: str) -> None:
        self._grid, self._category = grid_entity, category

    def compile(self, rule: Rule, context: Context) -> ConstraintContribution | PendingIssue:
        def issue(reason: str, detail: str) -> PendingIssue:
            return PendingIssue(issue_id=f"tariff-{rule.rule_id}-{reason}", reason=reason, rule_id=rule.rule_id, detail=detail,
                                refs=(Ref(kind="rule", id=rule.rule_id),))

        if rule.subject_ids != (self._grid,):
            return issue("subject_unknown", f"电价规则只能作用于电网接入点 {self._grid}，实际 {list(rule.subject_ids)}")
        if rule.quantity != QUANTITY or rule.unit != Unit.fraction.value:
            return issue("unit_unknown", f"电价规则须为 {QUANTITY}、单位 1（相对计价基础的系数），实际 {rule.quantity} / {rule.unit}")
        category = rule.condition.get("category")
        if not isinstance(category, str) or not category:
            return issue("subject_unknown", "电价规则没有注明用电类别 condition.category")
        if category != self._category:
            return issue("conflict", f"电价规则适用类别 {category}，本厂区执行 {self._category}")
        parsed = parse_schedule(rule)
        if isinstance(parsed, tuple):
            return issue(*parsed)
        return ConstraintContribution(rule_id=rule.rule_id, pack=PACK, constraint_names=(rule_constraint(rule),),
                                      refs=(Ref(kind="rule", id=rule.rule_id),), compiler_version=COMPILER_VERSION)


def rule_constraint(rule: Rule) -> str:
    return "price_" + re.sub(r"[^a-z0-9_]", "_", rule.rule_id.lower())


# ---------------------------------------------------------------- 价格序列


@dataclass(frozen=True)
class SlotPrice:
    price_cny_per_kwh: float
    rule_id: str
    period: str
    base_month: str  # 计价基础取的当地年月


def price_schedule(rules: Sequence[Rule], base_prices: Mapping[str, float], time_set: Sequence[Interval]) -> list[SlotPrice]:
    """逐时段定价（与模型无关，B08/B16/B17 组账本也用它）。时段按开始时刻定生效版本，时段内的当地分钟须同属一个时段名。
    任一时段无规则覆盖、被两条规则同时覆盖、所在月份未定义或缺计价基础、或跨越时段边界，汇总后一次性拒绝。
    这里不再检查对象、单位与用电类别：调用方只能传 TouScheduleKind.compile 通过的规则（规划时即 B09 筛选后的 by_pack）。"""
    schedules = []
    for r in rules:
        parsed = parse_schedule(r)
        if isinstance(parsed, tuple):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"电价规则 {r.rule_id} 无效：{parsed[1]}")
        schedules.append((r, parsed))
    out, problems = [], []
    for k, slot in enumerate(time_set):
        hit = [(r, s) for r, s in schedules if r.applies_to(slot.start)]
        if len(hit) != 1:
            why = "没有电价规则覆盖" if not hit else f"被多条电价规则同时覆盖 {[r.rule_id for r, _ in hit]}"
            problems.append(f"时段 {k}（{slot.start.isoformat()}）{why}")
            continue
        rule, sched = hit[0]
        local = slot.start.astimezone(sched.tz)
        month = f"{local.year:04d}-{local.month:02d}"
        minutes = int((slot.end - slot.start) / timedelta(minutes=1))
        seen = {sched.at((slot.start + timedelta(minutes=i)).astimezone(sched.tz)) for i in range(minutes)}
        if None in seen:
            last = (slot.end - timedelta(minutes=1)).astimezone(sched.tz).date()
            beyond = f"；节日日历只登记到 {sched.through}，请补登国家公布的节日日期" if sched.through and last > sched.through else ""
            problems.append(f"时段 {k} 所在月份或日期不在规则 {rule.rule_id} 的定义内（缺段{beyond}）")
        elif len(seen) > 1:
            problems.append(f"时段 {k} 跨越电价时段边界 {sorted(seen)}：{minutes} 分钟步长与电价表的时段边界不对齐，请减小规划步长")
        elif month not in base_prices:
            problems.append(f"时段 {k} 缺 {month} 的计价基础（用户购电价格）")
        else:
            name, factor = seen.pop()
            price = base_prices[month] * factor
            if not math.isfinite(price) or price > MAX_PRICE_CNY_PER_KWH:
                problems.append(f"时段 {k} 的 {month} 计价基础 × 系数超出工程上界 {MAX_PRICE_CNY_PER_KWH} 元/kWh（得到 {price!r}）：检查 base_prices 与规则系数")
            else:
                out.append(SlotPrice(price_cny_per_kwh=price, rule_id=rule.rule_id, period=name, base_month=month))
    if problems:
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"电价不完整，拒绝按缺价建模（共 {len(problems)} 处）：" + "；".join(problems[:5]))
    return out


def cost_report(prices: Sequence[SlotPrice], time_set: Sequence[Interval], grid_import_kw: Sequence[float]) -> dict:
    """按时段名汇总电量与电度电费；峰值购电单列为指标（非月度需量电费）。"""
    by_period: dict[str, dict[str, float]] = {}
    for p, slot, kw in zip(prices, time_set, grid_import_kw, strict=True):
        kwh = kw * ((slot.end - slot.start) / timedelta(hours=1))
        row = by_period.setdefault(p.period, {"kwh": 0.0, "cny": 0.0})
        row["kwh"] += kwh
        row["cny"] += kwh * p.price_cny_per_kwh
    return {"energy_cost_cny": sum(r["cny"] for r in by_period.values()), "by_period": by_period,
            "rule_ids": sorted({p.rule_id for p in prices}),
            "peak_import_kw": max(grid_import_kw, default=0.0), "peak_note": "日内最大购电功率，仅作指标；需量/容量电费不在本包核算"}


# ---------------------------------------------------------------- 包


@dataclass
class TariffPack:
    """grid_entity：电网接入点；category：本厂区执行的用电类别；base_prices：当地年月 → 计价基础（元/kWh）；
    config_ref：这些配置的受信来源。"""

    grid_entity: str
    category: str
    base_prices: Mapping[str, float]
    config_ref: str
    name: str = PACK
    tool_specs: tuple = ()
    rule_kinds: tuple = field(init=False)

    def __post_init__(self) -> None:
        if not self.config_ref.startswith("config://"):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"电价包配置来源须为 config://…，实际 {self.config_ref!r}")
        for month, price in self.base_prices.items():
            if not _MONTH.match(str(month)) or not (is_number(price) and price > 0):
                raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"计价基础 {month!r}: {price!r} 须为 YYYY-MM → 正有限数（元/kWh）")
        self.base_prices = MappingProxyType(dict(self.base_prices))
        self.rule_kinds = (TouScheduleKind(self.grid_entity, self.category),)

    def prices(self, rules: Sequence[Rule], time_set: Sequence[Interval]) -> list[SlotPrice]:
        return price_schedule(rules, self.base_prices, time_set)

    def contribute(self, mb: ModelBuilder, state: StateSnapshot, rules: Sequence[Rule], forecasts: ForecastBundle) -> ContributionReceipt:
        prices = self.prices(rules, mb.time_set)
        hours = [(slot.end - slot.start) / timedelta(hours=1) for slot in mb.time_set]
        gi = mb.grid_import
        cost = mb.add_var("energy_cost", "continuous", (None, None), Unit.CNY)
        cfg = Ref(kind="config", id=self.config_ref)
        names = []
        for rule in rules:
            def expr(t: int, rid: str = rule.rule_id) -> Any:
                p = prices[t]
                return cost[t] == p.price_cny_per_kwh * gi[t] * hours[t] if p.rule_id == rid else pyo.Constraint.Skip
            mb.add_constraint(rule_constraint(rule), expr, [Ref(kind="rule", id=rule.rule_id), cfg])
            names.append(rule_constraint(rule))
        mb.register_cost(cost, "energy_cost")
        refs = {cfg, *(Ref(kind="rule", id=r.rule_id) for r in rules)}
        return ContributionReceipt(pack=PACK, variables=(f"{PACK}__energy_cost",), constraints=tuple(f"{PACK}__{n}" for n in names),
                                   cost_terms=("energy_cost",), refs=tuple(sorted(refs, key=lambda r: (r.kind, r.id))))


def acceptance_blockers(rules: Sequence[Rule], demo_base_price: bool) -> list[str]:
    """正式闭环验收的拦截项：演示配置、未经人复核（approved）的规则、或计价基础是演示值，都不能作为正式验收的电价依据。"""
    out = [f"{r.rule_id} 是演示配置，不是已核实的真实政策" for r in rules if r.condition.get("demo")]
    out += [f"{r.rule_id} 复核状态 {r.review_status}，正式验收须为 approved" for r in rules
            if not r.condition.get("demo") and r.review_status != "approved"]
    if demo_base_price:
        out.append("计价基础（用户购电价格）是演示值，未取自当月代理购电价格公告")
    return out if rules else ["没有任何电价规则", *out]


# ---------------------------------------------------------------- 受信 YAML


_FILE_KEYS = {"demo", "source", "rules"}
_RULE_KEYS = {"rule_id", "grid_entity", "category", "timezone", "flat_factor", "effective_from", "effective_to", "available_at", "nature",
              "review_status", "reviewer", "review_note", "locator", "source_asset_version", "periods"}


def load_tariff_rules(path: Path, config_id: str) -> list[Rule]:
    """trusted_yaml_rules：读人工整理的分时时段表 YAML，生成 Rule。

    文件顶层必须显式写 demo: true/false；demo 文件的每条规则都在 condition 里带 demo 标记，不能冒充真实政策。
    审核人与审核说明随规则保存；未知键直接拒绝。来源记录（文号、网址、核对情况）写进 condition.source 供追溯。
    """
    data = read_yaml(path, config_id)
    if set(data) - _FILE_KEYS or not isinstance(data.get("demo"), bool):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{config_id} 顶层只能有 {sorted(_FILE_KEYS)}，且必须显式给出 demo: true/false")
    rules = []
    for k, item in enumerate(data.get("rules") or []):
        if not isinstance(item, dict):
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{config_id} rules[{k}] 须是映射，实际 {item!r}")
        unknown = sorted(set(item) - _RULE_KEYS)
        if unknown:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{config_id} rules[{k}] 有未知键 {unknown}")
        condition = {"category": item.get("category"), "timezone": item.get("timezone"), "periods": item.get("periods"),
                     "demo": data["demo"], "source": data.get("source"), "review_note": item.get("review_note")}
        try:
            rules.append(Rule(
                rule_id=item["rule_id"], kind=TOU_KIND, subject_ids=(item["grid_entity"],), condition=condition, quantity=QUANTITY,
                unit=Unit.fraction, value=item["flat_factor"], effective_from=item["effective_from"], effective_to=item.get("effective_to"),
                available_at=item["available_at"], source_asset_version=item.get("source_asset_version"),
                config_ref=f"config://{config_id}#rules.{item['rule_id']}", locator=item.get("locator"), nature=item["nature"],
                review_status=item["review_status"], reviewer=item.get("reviewer"), compiler_version=COMPILER_VERSION))
        except (KeyError, ValueError, TypeError) as exc:
            raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{config_id} rules[{k}] 无效：{exc}") from exc
    return rules


def load_tariff_config(path: Path, config_id: str) -> tuple[TariffPack, bool]:
    """厂区电价配置（接入点、用电类别、按月计价基础）。返回 (包, 计价基础是否为演示值)。"""
    data = read_yaml(path, config_id).get("tariff")
    keys = {"grid_entity", "category", "base_prices", "demo_base_price", "base_price_note"}
    if (not isinstance(data, dict) or set(data) != keys or not isinstance(data["demo_base_price"], bool)
            or not isinstance(data["base_prices"] or {}, dict)):
        raise PlantkgError(ErrorCode.DOMAIN_INVALID, f"{config_id} 的 tariff 段须恰好有 {sorted(keys)}，demo_base_price 显式 true/false")
    pack = TariffPack(data["grid_entity"], data["category"], data["base_prices"] or {}, f"config://{config_id}#tariff")
    return pack, data["demo_base_price"]
