"""测试用 Fake 领域包：只经 ModelBuilder 贡献，口径与 B10/B11/B12 卡一致，但不是那几个包的实现。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from plantkg.core import contracts as c

PLANT = "demo-electronics"
BAT = f"entity://{PLANT}/battery/BAT1"
PV = f"entity://{PLANT}/pv/PV1"


def cfg(key: str) -> c.Ref:
    return c.Ref(kind="config", id=f"config://fixtures/planner/day_96.yaml#{key}")


def receipt(pack: str, builder, refs=()) -> c.ContributionReceipt:
    st = builder.state
    return c.ContributionReceipt(
        pack=pack, variables=tuple(k for k, v in st.vars.items() if v.pack == pack),
        constraints=tuple(builder.pack_constraints(pack)),
        balance_terms=tuple(t.label for t in st.supply + st.demand if t.pack == pack),
        cost_terms=tuple(t.label for t in st.costs if t.pack == pack), refs=tuple(refs),
    )


@dataclass
class FakePv:
    name: str = "pv"
    rule_kinds: tuple = ()
    tool_specs: tuple = ()

    def contribute(self, mb, state, rules, forecasts):
        avail = mb.pv_available
        use = mb.add_var("use", "continuous", (0, None), c.Unit.kW)
        cut = mb.add_var("curtail", "continuous", (0, None), c.Unit.kW, action=(PV, "pv_curtail"))
        mb.add_constraint("conservation", lambda t: use[t] + cut[t] == avail[t], [cfg("pv_available_kw")])
        mb.register_balance("supply", use, "pv_used")
        return receipt(self.name, mb, [cfg("pv_available_kw")])


class SocFloorKind:
    """Fake 规则类型：SOC 下限（bounds.lower，0～1）。"""

    kind = "fake_soc_floor"
    compiler_version = "fake-1"

    def compile(self, rule, context):
        if rule.bounds is None or rule.bounds.lower is None or not 0 <= rule.bounds.lower <= 1:
            return c.PendingIssue(issue_id=f"soc-{rule.rule_id}", reason="config_invalid", rule_id=rule.rule_id, detail="SOC 下限须在 0～1")
        return c.ConstraintContribution(rule_id=rule.rule_id, pack="storage", constraint_names=(_floor_name(rule),),
                                        refs=(c.Ref(kind="rule", id=rule.rule_id),), compiler_version=self.compiler_version)


def _floor_name(rule) -> str:
    return "soc_floor_" + rule.rule_id.replace("-", "_")


@dataclass
class FakeStorage:
    params: dict
    name: str = "storage"
    rule_kinds: tuple = (SocFloorKind(),)
    tool_specs: tuple = ()
    end_min_kwh: float | None = None  # None = 回到初始电量
    skip_rule_constraints: bool = False

    def contribute(self, mb, state, rules, forecasts):
        p = self.params
        n, h = len(mb.time_set), (mb.time_set[0].end - mb.time_set[0].start).total_seconds() / 3600
        e0 = state.pack_states["storage"]["energy_kwh"]
        ref = [cfg("storage")]
        pc = mb.add_var("charge", "continuous", (0, p["power_kw"]), c.Unit.kW, action=(BAT, "charge_power"))
        pd = mb.add_var("discharge", "continuous", (0, p["power_kw"]), c.Unit.kW, action=(BAT, "discharge_power"))
        u = mb.add_var("is_charging", "binary", (0, 1), c.Unit.fraction)
        e = mb.add_var("energy", "continuous", (p["soc_min"] * p["capacity_kwh"], p["soc_max"] * p["capacity_kwh"]), c.Unit.kWh)
        mb.add_constraint("charge_excl", lambda t: pc[t] <= p["power_kw"] * u[t], ref)
        mb.add_constraint("discharge_excl", lambda t: pd[t] <= p["power_kw"] * (1 - u[t]), ref)
        mb.add_constraint("energy_balance", lambda t: e[t] == (e0 if t == 0 else e[t - 1]) + p["eta_c"] * pc[t] * h - pd[t] * h / p["eta_d"], ref)
        mb.add_constraint("energy_end", e[n - 1] >= (e0 if self.end_min_kwh is None else self.end_min_kwh), ref)
        for rule in rules:
            if not self.skip_rule_constraints:
                floor = rule.bounds.lower * p["capacity_kwh"]
                mb.add_constraint(_floor_name(rule), lambda t, f=floor: e[t] >= f, [c.Ref(kind="rule", id=rule.rule_id)])
        mb.register_balance("supply", pd, "discharge")
        mb.register_balance("demand", pc, "charge")
        return receipt(self.name, mb, ref)


@dataclass
class FakeTariff:
    prices: list[float]
    name: str = "tariff"
    rule_kinds: tuple = ()
    tool_specs: tuple = ()

    def contribute(self, mb, state, rules, forecasts):
        h = (mb.time_set[0].end - mb.time_set[0].start).total_seconds() / 3600
        gi = mb.grid_import
        mb.register_cost(lambda t: self.prices[t] * gi[t] * h, "energy_cost")
        return receipt(self.name, mb, [cfg("price_by_hour")])


@dataclass
class FakeHvac:
    """“新增测试包”：只在测试目录定义，给共享模型加一条固定负荷，不改规划器任何代码。"""

    load_kw: float = 40.0
    name: str = "hvac"
    rule_kinds: tuple = ()
    tool_specs: tuple = ()

    def contribute(self, mb, state, rules, forecasts):
        mb.register_balance("demand", [self.load_kw] * len(mb.time_set), "hvac_load")
        return receipt(self.name, mb, [cfg("hvac")])


@dataclass
class HardKnapsack:
    """难解的多维背包，用来真实触发 HiGHS 时间预算（与功率平衡无关）。"""

    n: int = 300
    k: int = 8
    name: str = "hard"
    rule_kinds: tuple = ()
    tool_specs: tuple = ()
    seed: int = 3

    def contribute(self, mb, state, rules, forecasts):
        rng = random.Random(self.seed)
        pick = [mb.add_var(f"item_{i}", "binary", (0, 1), c.Unit.fraction, scalar=True) for i in range(self.n)]
        for j in range(self.k):
            w = [rng.randint(100, 999) for _ in range(self.n)]
            mb.add_constraint(f"cap_{j}", sum(w[i] * pick[i] for i in range(self.n)) <= sum(w) // 2, [cfg("hard")])
        mb.register_cost(-sum(rng.randint(100, 999) * pick[i] for i in range(self.n)) / 1000.0, "neg_value")
        return receipt(self.name, mb, [cfg("hard")])


@dataclass
class Snooper:
    """违规包：读另一个包的变量。"""

    name: str = "snoop"
    rule_kinds: tuple = ()
    tool_specs: tuple = ()
    seen: list = field(default_factory=list)

    def contribute(self, mb, state, rules, forecasts):
        other = mb.model.find_component("storage__charge")
        mb.add_constraint("steal", lambda t: other[t] <= 10, [cfg("snoop")])
        return receipt(self.name, mb)

