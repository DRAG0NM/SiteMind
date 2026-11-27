"""B22 观测层按运行隔离与试算不写未来读数（#186 / #138）。

同厂区两个运行各自一份观测；ctx 带 run_id 时只读自己的；试算/临时规则代价不把未来读数写进总线。
"""
from datetime import datetime
from types import SimpleNamespace

from plantkg.core.contracts import Actor, Context, Interval, SimAction
from plantkg.core.registry import Registry
from plantkg.world.scenario import TrialRunner
from plantkg.world.sensors import ObservationBus
from plantkg.world.sensors.query import install

from .conftest import local, scenario

ADMIN = "entity://demo-electronics/zone/admin"


def _settings(plant_id: str) -> SimpleNamespace:
    return SimpleNamespace(plant_id=plant_id)


def _ctx(plant_id: str, run_id: str) -> Context:
    return Context(plant_id=plant_id, industry_pack="manufacturing", actor=Actor(actor_id="tester", roles=("operator",)),
                   channel="offline", run_id=run_id, trace_id="trace-186", as_of=local("08:00"))


def _tools(plant_id: str) -> dict:
    registry = Registry()
    install(registry, _settings(plant_id))
    return registry.tools


def test_sensor_query_reads_the_run_named_by_ctx(make):
    """同厂区跑完 A、B 两个运行后，A 的 ctx 仍拿到 A 的 at/run_id，不被后跑的 B 覆盖。"""
    ObservationBus.clear()
    a = make(scenario(), name="a.db")
    a.run(until=local("08:05"))
    plant = a.world.site.plant_id
    a_at = ObservationBus.latest(plant, a.run_info.run_id)["snapshot"]["at"]
    b = make(scenario(date="2026-07-02"), name="b.db")
    b.run(until=local("09:00", day=2))
    handler = _tools(plant)["sensor_query"].handler
    result = handler(context=_ctx(plant, a.run_info.run_id))
    assert result.ok and result.run_id == a.run_info.run_id
    assert result.data["at"] == a_at == local("08:04").isoformat()
    other = handler(context=_ctx(plant, b.run_info.run_id))
    assert other.data["at"] == local("08:59", day=2).isoformat()


def test_sensor_query_without_snapshot_falls_back_with_notice(make):
    """旧调用/未知运行：回退最近一次发布并显式说明可能来自其他运行，不冒充。"""
    ObservationBus.clear()
    a = make(scenario(), name="only.db")
    a.run(until=local("08:05"))
    plant = a.world.site.plant_id
    result = _tools(plant)["sensor_query"].handler(context=_ctx(plant, "run-unknown"))
    assert result.ok and result.notice and "可能属于其他运行" in result.notice


def test_router_readings_are_scoped_by_run(make):
    """router.readings 用运行号取数：A 的窗口不返回 B 的读数行（B 在另一天，时间过滤即可暴露串读）。"""
    ObservationBus.clear()
    a = make(scenario(), name="ra.db")
    a.run(until=local("08:05"))
    plant = a.world.site.plant_id
    b = make(scenario(date="2026-07-02"), name="rb.db")
    b.run(until=local("09:00", day=2))
    hook = _hook(plant)
    rows_a = hook("AC-2", local("07:00"), local("08:05"), a.run_info.run_id)
    assert rows_a and all(local("07:00") <= _at(row) <= local("08:05") for row in rows_a)
    rows_b = hook("AC-2", local("08:00", day=2), local("09:00", day=2), b.run_info.run_id)
    assert rows_b and all(row["at"].startswith("2026-07-02") for row in rows_b)


def _hook(plant_id: str):
    registry = Registry()
    install(registry, _settings(plant_id))
    return registry.hooks_for("router.readings")[-1]


def _at(row: dict) -> datetime:
    return datetime.fromisoformat(row["at"])


def test_trial_and_estimate_cost_do_not_publish_future_readings(make):
    """试算与临时规则代价不写 ObservationBus：试算前后总线最新时刻不变。"""
    ObservationBus.clear()
    runner = make(scenario(), name="trial.db")
    runner.run(until=local("08:00"))
    plant = runner.world.site.plant_id
    run_id = runner.run_info.run_id
    before = ObservationBus.latest(plant, run_id)["snapshot"]["at"]
    assert before == local("07:59").isoformat()
    TrialRunner(runner.world).trial(runner.snapshot(), [], Interval(start=local("08:00"), end=local("08:05")))
    assert ObservationBus.latest(plant, run_id)["snapshot"]["at"] == before
    action = SimAction(entity_id=ADMIN, command="set_setpoint", params={"setpoint_c": 24.0},
                       interval=Interval(start=local("08:00"), end=local("08:10")))
    assert runner.estimate_cost([action], local("08:30")) is not None
    assert ObservationBus.latest(plant, run_id)["snapshot"]["at"] == before
