"""B19 演示输入适配：性质闸门、接收与结算、来源性质保留。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from plantkg.core.contracts import ErrorCode, Interval, Locator, PlantkgError, SourceRecord
from plantkg.decision.usecases.demo_inputs import DemoScenarioInputProvider, accepted_natures, accept_records

from .conftest import local

NATURES = ("derived", "simulated", "synthetic", "configured")


def record(nature: str, meter: str = "AC-2", kw: float = 40.0, at=None) -> SourceRecord:
    at = at or local("08:10")
    return SourceRecord(source_id="b22-leak", source_version="v1", content_hash="sha256:" + "a" * 64,
                        locator=Locator(meter=meter, time_range=Interval(start=at, end=at + timedelta(minutes=1))),
                        nature=nature, event_time=at, interval=Interval(start=at, end=at + timedelta(minutes=1)),
                        available_at=at + timedelta(minutes=1), source_tz="Asia/Shanghai", source_unit="kW",
                        quantity="active_power", unit="kW", value=kw)


def test_dev_accepts_four_natures_and_settles_them(inputs):
    """derived / simulated / synthetic / configured 在 dev 下被接收，分钟输入叠加并按来源保留性质。"""
    records = [record(n, kw=10.0 * (i + 1)) for i, n in enumerate(NATURES)]
    provider = DemoScenarioInputProvider(inputs.base, profile="dev", records=records)
    assert not any(r.nature == "measured" for r in provider.extra_records)
    assert {r.nature for r in provider.extra_records} == set(NATURES)
    # 接收后分钟内叠加：AC-2 基础负荷 + 四条记录
    base = inputs.base.minute(local("08:10")).equipment_kw["AC-2"]
    merged = provider.minute(local("08:10")).equipment_kw["AC-2"]
    assert merged == pytest.approx(base + 100.0)
    assert provider.minute(local("08:11")).equipment_kw["AC-2"] == pytest.approx(base)  # 其它分钟不变
    # 来源表保留原性质
    rows = {row["meter"]: row for row in provider.source_table()}
    assert rows["AC-2"]["received"][0]["nature"] in NATURES
    assert [r["nature"] for r in rows["AC-2"]["received"]] == list(NATURES)
    # 1 分钟记录展开：原记录的 source_id 与 nature 不改
    minute_records = provider.minute_records("AC-2", local("08:10"), 2)
    extra = [r for r in minute_records if r.source_id == "b22-leak"]
    assert len(extra) == len(NATURES) and {r.nature for r in extra} == set(NATURES)
    assert all(r.interval.start == local("08:10") for r in extra)
    assert provider.provenance()["extra_records"]["by_meter"]["AC-2"][0]["nature"] in NATURES


def test_measured_is_rejected_in_scenario_and_only_measured_in_production():
    """场景输入不含 measured（dev 拒绝）；production profile 反过来只收 measured。"""
    with pytest.raises(PlantkgError) as exc:
        accept_records([record("measured")], "dev")
    assert exc.value.code == ErrorCode.PRECONDITION
    assert accepted_natures("dev") == frozenset(NATURES)
    assert accepted_natures("production") == frozenset({"measured"})
    for nature in NATURES:
        with pytest.raises(PlantkgError):
            accept_records([record(nature)], "prod")
    assert accept_records([record("measured")], "production")[0].nature == "measured"


def test_received_record_is_settled_in_scenario_ledger_and_sources(scene, make_assembly):
    """接收的 derived 记录进分钟账本并结算；场景执行记录的 sources 保留其性质。"""
    from plantkg.experts.energy.day import run_day

    day = run_day(scenario=scene, assembly=make_assembly("settle.db"), date="2026-07-01")
    extra = record("derived", kw=60.0, at=local("09:10"))
    day.inputs.accept([extra])
    day.execute(local("09:12"), start_at=local("09:09"))
    minute = next(m for m in day.minutes(local("09:10"), local("09:11")))
    merged = day.inputs.minute(local("09:10")).equipment_kw
    # 电梯、暖通的功率由各自仿真器算，这里只对照接收记录所在的空压机
    air_kw = sum(kw for code, kw in merged.items() if code.startswith("AC-"))
    assert minute.entry.load_breakdown["air_compressor"] * 60 == pytest.approx(air_kw, abs=1.0)
    assert minute.balanced
    sources = day.db.store.records(day.trace.runs[0].run_id, "sources")
    received = next(row for row in sources[0]["items"] if row["meter"] == "AC-2")["received"]
    assert received[0]["nature"] == "derived" and received[0]["source_id"] == "b22-leak"
