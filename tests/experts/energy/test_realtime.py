"""B19 分钟层：响应预算、按计划充电与突增后的放电。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from plantkg.core.contracts import Interval, StorageParams
from plantkg.core.ledger import LedgerMeta, SlotInput, compute_ledger
from plantkg.experts.energy.realtime import RealtimeController, SlotTarget

from .conftest import local

STORAGE = StorageParams(capacity_kwh=2000, eta_c=0.95, eta_d=0.95, soc_min=0.1, soc_max=0.9)
META = LedgerMeta(ledger_id="t", kind="execution", run_id="r", plan_id="p", plan_revision=0,
                  version_manifest_id="vm", source_refs=("t",))


def test_response_budget_keeps_earlier_minutes_and_starts_discharge_at_0806():
    """08:05 突增、响应预算 1 分钟、08:00 槽计划充电 200 kW：08:06 起放电，之前按计划充电。"""
    controller = RealtimeController(battery_id="entity://demo-electronics/battery/BAT1", storage=STORAGE,
                                    charge_kw=500.0, discharge_kw=500.0, respond_from=local("08:06"))
    target = SlotTarget(start=local("08:00"), end=local("08:15"), load_kw=1000.0, pv_kw=0.0, charge_kw=200.0,
                        discharge_kw=0.0, price_cny_per_kwh=0.62)
    energy, records = 900.0, []
    for minute in range(15):
        at = local("08:00") + minute * timedelta(minutes=1)
        load = 1000.0 if minute < 5 else 1550.0  # 08:05 起突增 550 kW
        action = controller.step({"at": at, "load_kw": load, "pv_kw": 0.0, "energy_kwh": energy}, target)
        charge = action.params["power_kw"] / 60 if action.command == "charge" else 0.0
        discharge = action.params["power_kw"] / 60 if action.command == "discharge" else 0.0
        result = compute_ledger([SlotInput(interval=Interval(start=at, end=at + timedelta(minutes=1)), pv_available_kw=0.0,
                                           load_kw=load, charge_kw=charge * 60, discharge_kw=discharge * 60,
                                           price_cny_per_kwh=0.62)], META, STORAGE, energy)
        assert result.ledger.balanced, result.problems
        energy = result.ledger.entries[0].energy_end_kwh
        records.append((at, action.command, action.params["power_kw"], result.ledger.entries[0]))

    # 08:00–08:05：按计划充电 200 kW，突增的 08:05 也不提前响应
    for at, command, power, entry in records[:6]:
        assert command == "charge" and power == pytest.approx(200.0), at
        assert entry.discharge_kwh == 0.0 and entry.charge_kwh == pytest.approx(200 / 60)
    # 08:06 起：把突增压回目标线（1000 + 200 = 1200 kW），放电 350 kW
    at, command, power, entry = records[6]
    assert at == local("08:06") and command == "discharge"
    assert power == pytest.approx(350.0)
    assert entry.grid_import_kwh * 60 == pytest.approx(1200.0, abs=1.0)


def test_charge_only_when_planned_or_pv_surplus():
    controller = RealtimeController(battery_id="entity://demo-electronics/battery/BAT1", storage=STORAGE, charge_kw=500.0, discharge_kw=500.0)
    quiet = SlotTarget(start=local("08:00"), end=local("08:15"), load_kw=1000.0, pv_kw=0.0)
    action = controller.step({"at": local("08:00"), "load_kw": 700.0, "pv_kw": 0.0, "energy_kwh": 900.0}, quiet)
    assert action.command == "idle"  # 计划不充电、无光伏富余：不抢购电
    with_pv = SlotTarget(start=local("12:00"), end=local("12:15"), load_kw=100.0, pv_kw=400.0)
    action = controller.step({"at": local("12:00"), "load_kw": 100.0, "pv_kw": 400.0, "energy_kwh": 900.0}, with_pv)
    assert action.command == "charge" and action.params["power_kw"] > 0  # 光伏富余时吸收


def test_discharge_respects_soc_floor_and_power_limit():
    controller = RealtimeController(battery_id="entity://demo-electronics/battery/BAT1", storage=STORAGE, charge_kw=500.0, discharge_kw=500.0)
    target = SlotTarget(start=local("08:00"), end=local("08:15"), load_kw=3000.0, pv_kw=0.0)
    near_floor = controller.step({"at": local("08:00"), "load_kw": 3500.0, "pv_kw": 0.0, "energy_kwh": 200.05}, target)
    assert near_floor.command == "discharge" and near_floor.params["power_kw"] < 5  # 已到下限，几乎放不出
    normal = controller.step({"at": local("08:01"), "load_kw": 3500.0, "pv_kw": 0.0, "energy_kwh": 1000.0}, target)
    assert normal.params["power_kw"] == pytest.approx(500.0)  # 不超过功率上限
