"""B17 CLI / live 权威入口带 B19 能源分钟层（#188 Plan A）。

同一 `factory_demo_day` 08:00～08:30：08:05 的负荷突增在响应预算后从 08:06 起由分钟层充放，
08:00/08:15 槽汇总在带分钟层的入口可见；事件级能源提案不再下发电池充放。
"""
from datetime import datetime
from pathlib import Path

from plantkg.experts.energy.minute import install_minute_layer
from plantkg.world.scenario.script import load_scenario

from .conftest import local

ROOT = Path(__file__).resolve().parents[3]
SCENARIO = ROOT / "configs/scenarios/factory_demo_day.yaml"


def _clock(value: str, tz) -> str:
    return datetime.fromisoformat(value).astimezone(tz).strftime("%H:%M")


def test_cli_entry_discharges_from_0806_with_minute_layer(make):
    runner = make(load_scenario(SCENARIO), name="minute.db")
    install_minute_layer(runner)
    trace = runner.run_until(local("08:30"))
    tz = runner.world.scenario.tz
    battery = runner.world.index.battery_id
    minutes = [a for a in trace.actions if a["source"].get("layer") == "minute"]
    assert minutes and all(a["action"]["entity_id"] == battery for a in minutes)
    assert _clock(minutes[0]["action"]["interval"]["start"], tz) == "08:06"
    # 储能由分钟层独占：事件级能源提案里的电池充放不再下发
    stored = [a for a in trace.actions if a["action"]["entity_id"] == battery
              and a["action"]["command"] in ("charge", "discharge")]
    assert stored == minutes
    # 权威入口能看到 08:00 与 08:15 的槽汇总
    slots = {s.interval.start for s in trace.slots}
    assert local("08:00") in slots and local("08:15") in slots
    assert any(n["kind"] == "energy_response" for n in trace.notes)
