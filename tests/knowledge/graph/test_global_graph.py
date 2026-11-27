"""B25 全局图谱：介质拓扑查询、来源定位、安全标记、优先级、预案，以及经 MCP 的调用。"""
import asyncio
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.knowledge.graph import GlobalGraph
from plantkg.mcp_server import create_mcp_server


@pytest.fixture(scope="module")
def g():
    return GlobalGraph.load()


def test_compressor_reaches_the_lines_it_feeds(g):
    r = g.trace_impact("AC-1", ("compressed_air",))
    lines = {a["code"] for a in r["affected"] if a["type"] == "production_line"}
    assert lines == {"LINE-1", "LINE-2", "LINE-3"}
    assert next(a for a in r["affected"] if a["code"] == "LINE-1")["path"] == ["AC-1", "AIR-HDR", "AIR-BR-P", "LINE-1"]


def test_air_header_pressure_drop_names_zones_with_paths(g):
    r = g.trace_impact("entity://demo-electronics/air_header/AIR-HDR")
    zones = {z["zone"]: z["path"] for z in r["zones"]}
    assert zones == {"production": ["AIR-HDR", "AIR-BR-P", "LINE-1", "production"],
                     "cleanroom": ["AIR-HDR", "AIR-BR-CR", "CR-PNEU", "cleanroom"]}
    assert {t["id"] for t in r["safety"]} == {"hard_climate"}


def test_cooling_water_failure_propagates_across_media(g):
    paths = {a["code"]: a["path"] for a in g.trace_impact("UTIL-P1")["affected"]}
    assert paths["CR-PNEU"] == ["UTIL-P1", "CW-LOOP", "AC-1", "AIR-HDR", "AIR-BR-CR", "CR-PNEU"]


def test_load_surge_candidates_are_loads_grouped_by_domain(g):
    r = g.find_sources("TX1", "load_surge")
    codes = {c["code"] for c in r["candidates"]}
    assert {"AC-1", "FE-1", "LINE-1", "AHU-P1"} <= codes and not codes & {"PV1", "BAT1", "PE-1"}
    assert set(r["domains"]) == {"energy", "equipment", "hvac", "elevator"}
    assert r["domains"]["elevator"] == {"count": 4, "rated_kw": 120.0}


def test_pressure_drop_lists_suppliers_and_leak_points(g):
    roles = {c["code"]: c["role"] for c in g.find_sources("AIR-HDR", "pressure_drop")["candidates"]}
    assert roles["AC-2"] == roles["UTIL-P1"] == "supply"
    assert roles["AIR-BR-CR"] == roles["LINE-3"] == "leak"


def test_zone_symptom_searches_from_serving_devices(g):
    cands = {c["code"]: c for c in g.find_sources("cleanroom", "temperature_deviation")["candidates"]}
    assert {"CR-AHU1", "CR-AHU2", "P-CR", "TX2"} <= set(cands)
    assert cands["P-CR"]["path"] == ["P-CR", "CR-AHU1", "cleanroom"]
    assert "AC-1" not in cands  # 温度偏离不沿压缩空气找
    lost = {c["code"] for c in g.find_sources("admin", "power_loss")["candidates"]}
    assert {"P-ADMIN-HVAC", "P-ADMIN-LIFT", "P-ADMIN-LT", "TX2"} <= lost  # 服务楼层的电梯、照明支路也算


def test_occupied_elevator_action_is_tagged(g):
    stuck = g.safety_tags("FE-2", "stop")
    assert stuck["verdict"] == "work_order" and stuck["tags"][0]["id"] == "occupied_car"
    assert stuck["tags"][0]["clause"] == "SR-1.1" and "轿厢内无人" in stuck["tags"][0]["clause_text"]
    assert g.safety_tags("FE-2", "stop", occupied=False)["verdict"] == "auto_ok"
    assert g.safety_tags("PE-1", "dispatch")["verdict"] == "auto_ok"


def test_cutting_power_upstream_hits_tagged_areas(g):
    r = g.safety_tags("TX2", "power_off")
    by_id = {t["id"]: t for t in r["tags"]}
    assert {"no_power_cut", "hazmat_area", "hard_climate", "life_safety_circuit", "occupied_car"} <= set(by_id)
    assert by_id["no_power_cut"]["via"] == "supply" and by_id["no_power_cut"]["path"][:3] == ["TX2", "LV2", "P-DC"]
    assert g.safety_tags("P-ADMIN-LT", "shed")["tags"][0]["nodes"] == ["EL-A"]
    assert g.safety_tags("P-LINE", "shed")["verdict"] == "auto_ok"
    assert g.safety_tags("P-DC", "power_off")["tags"][0]["via"] == "direct"  # 同一标记只报一条


def test_stopping_a_supply_node_hits_tags_downstream_of_any_medium(g):
    r = g.safety_tags("AIR-HDR", "stop")
    assert r["verdict"] == "work_order" and r["tags"][0]["id"] == "hard_climate"
    assert r["tags"][0]["path"] == ["AIR-HDR", "AIR-BR-CR"]
    assert g.safety_tags("UTIL-P1", "stop")["verdict"] == "work_order"
    assert g.safety_tags("AIR-HDR", "setpoint_change")["verdict"] == "auto_ok"


def test_zone_as_target(g):
    assert g.safety_tags("hazmat", "stop")["tags"][0]["id"] == "hazmat_area"
    assert g.safety_tags("datacenter", "observe")["verdict"] == "auto_ok"


def test_unclear_cases_count_as_involved(g):
    assert g.safety_tags("NO-SUCH", "stop")["verdict"] == "work_order"
    r = g.safety_tags("DC-IT1", "reboot_everything")
    assert r["verdict"] == "work_order" and r["notes"]
    assert g.safety_tags("HZ-EXF1", "observe")["verdict"] == "auto_ok"


def test_maintenance_counts_as_cutting_supply(g):
    dc = g.safety_tags("P-DC", "maintenance")
    assert dc["verdict"] == "work_order" and {t["id"] for t in dc["tags"]} >= {"no_power_cut"}
    hdr = g.safety_tags("AIR-HDR", "maintenance")
    assert hdr["verdict"] == "work_order" and hdr["tags"][0]["id"] == "hard_climate" and hdr["tags"][0]["via"] == "supply"
    assert g.safety_tags("TX2", "maintenance")["verdict"] == "work_order"
    assert g.safety_tags("FE-1", "curtail")["verdict"] == "auto_ok"  # 限负荷不等价于停梯


def test_unregistered_action_follows_explicit_rule(g):
    """未登记动作按会改变状态处理（保守按切断类传播），不再伪造 unknown_action；目标查不清才整体按涉及。"""
    r = g.safety_tags("LINE-1", "reboot_everything")
    assert r["verdict"] == "auto_ok" and r["notes"] and r["tags"] == []
    tagged = g.safety_tags("DC-IT1", "reboot_everything")
    assert tagged["verdict"] == "work_order" and "no_power_cut" in {t["id"] for t in tagged["tags"]}
    assert "unknown_action" not in {t["id"] for t in tagged["tags"]}
    assert g.safety_tags("LINE-1", "setpoint_change")["verdict"] == "auto_ok"
    assert g.safety_tags("NO-SUCH-ANYTHING", "setpoint_change")["tags"][0]["id"] == "unknown_target"


# 实际命令 × 对象类型矩阵：区域 / 设备 / 聚合对象。期望与 B47 `_safety_veto` 一致：
# 判定 work_order 的动作进操作工单，auto_ok 的动作可自动执行。
REAL_COMMAND_MATRIX = (
    ("set_mode", "entity://demo-electronics/zone/admin", "auto_ok"),
    ("set_mode", "entity://demo-electronics/zone/cleanroom", "work_order"),
    ("set_mode", "entity://demo-electronics/zone/datacenter", "auto_ok"),
    ("set_setpoint", "entity://demo-electronics/ahu/AHU-A01", "auto_ok"),
    ("setpoint_change", "entity://demo-electronics/zone/cleanroom", "work_order"),
    ("switch_circuit", "entity://demo-electronics/panel/P-ADMIN-LT", "work_order"),
    ("power_off", "entity://demo-electronics/transformer/TX2", "work_order"),
    ("discharge", "entity://demo-electronics/battery/BAT1", "auto_ok"),
    ("maintenance", "entity://demo-electronics/freight_elevator/FE-2", "work_order"),
    ("stop", "entity://demo-electronics/elevator_group/admin_low", "work_order"),
    ("maintenance", "entity://demo-electronics/elevator_group/admin_low", "work_order"),
    ("stop", "entity://demo-electronics/air_system/AIR", "auto_ok"),
    ("maintenance", "entity://demo-electronics/air_system/AIR", "auto_ok"),
    ("isolate_leak", "entity://demo-electronics/air_system/AIR", "auto_ok"),
    ("repair", "entity://demo-electronics/air_system/AIR", "auto_ok"),
    ("power_off", "entity://demo-electronics/elevator_group/admin_low", "work_order"),
    ("power_off", "entity://demo-electronics/air_system/AIR", "work_order"),
    ("shed", "entity://demo-electronics/elevator_group/admin_low", "work_order"),
    ("shed", "entity://demo-electronics/air_system/AIR", "work_order"),
    ("observe", "entity://demo-electronics/zone/admin", "auto_ok"),
)


@pytest.mark.parametrize(("action", "target", "verdict"), REAL_COMMAND_MATRIX)
def test_real_commands_hit_expected_rule(g, action, target, verdict):
    r = g.safety_tags(target, action)
    assert r["verdict"] == verdict
    assert "unknown_action" not in {t["id"] for t in r["tags"]}
    if verdict == "work_order":
        assert r["requires_work_order"] and r["tags"]
    else:
        assert not r["requires_work_order"]


def test_real_command_matrix_matches_b47_safety_veto():
    """同一矩阵经 B47 `_safety_veto` 走默认装配的图谱钩子：B25 判定与执行层否决一致。"""
    from types import SimpleNamespace

    from plantkg.agent.execution_policy.service import ExecutionPolicyService
    from plantkg.core.contracts import Interval, ProposedAction
    from plantkg.world.scenario.runner import default_registry, pack_registry

    router = SimpleNamespace(registry=default_registry(plugins=pack_registry()))
    at = datetime(2026, 7, 1, 8, 5, tzinfo=timezone.utc)
    for action, target, verdict in REAL_COMMAND_MATRIX:
        item = ProposedAction(entity_id=target, command=action, params={},
                              interval=Interval(start=at, end=at + timedelta(minutes=1)))
        assert ExecutionPolicyService._safety_veto(router, item) is (verdict == "work_order"), (action, target)


def test_zone_setpoint_does_not_hit_emergency_lighting(g):
    """区域级非切断动作只作用于目标领域：HVAC 设定值不碰应急照明回路；洁净车间的温度硬约束仍保留。"""
    admin = g.safety_tags("zone:admin", "set_mode")
    assert admin["verdict"] == "auto_ok" and admin["tags"] == []
    assert g.safety_tags("admin", "precool_delay")["verdict"] == "auto_ok"
    clean = g.safety_tags("zone:cleanroom", "set_mode")
    assert clean["verdict"] == "work_order" and {t["id"] for t in clean["tags"]} == {"hard_climate"}
    assert clean["tags"][0]["via"] == "direct"
    # 切断类动作仍把区内全部节点当直接受害：admin 的应急照明回路（EL-A）照样命中
    assert {t["id"] for t in g.safety_tags("zone:admin", "power_off")["tags"]} >= {"life_safety_circuit"}


def test_aggregate_objects_are_not_unclear(g):
    """电梯群、空压系统是聚合对象，按具体设备判定，不算查不清（与 B25 router.safety_hit 同口径）。"""
    from plantkg.knowledge.graph.tools import router_hooks

    hooks = router_hooks(g)
    group = "entity://demo-electronics/elevator_group/admin_low"
    air = "entity://demo-electronics/air_system/AIR"
    for entity in (group, air):
        result = g.safety_tags(entity, "set_mode")
        assert result["verdict"] == "auto_ok" and result["notes"] and "unknown_target" not in {t["id"] for t in result["tags"]}
        assert hooks["router.safety_hit"](entity) is False
    assert hooks["router.safety_hit"]("entity://demo-electronics/does_not_exist/NOPE") is True


def test_aggregate_cut_actions_expand_member_safety_tags(g):
    """聚合对象按 aggregate_policies 展开成员设备逐台判定（#201、#223、#240），展不开成员时保守转工单。"""
    group = "entity://demo-electronics/elevator_group/admin_low"
    air = "entity://demo-electronics/air_system/AIR"
    assert "occupied_car" in {t["id"] for t in g.safety_tags(group, "power_off")["tags"]}
    assert "hard_climate" in {t["id"] for t in g.safety_tags(air, "shed")["tags"]}
    # 电梯群 stop/检修逐台查轿厢：有人或无法确认时转工单，明确无人时自动（#223）
    assert g.safety_tags(group, "stop")["verdict"] == "work_order"
    assert g.safety_tags(group, "stop", occupied=False)["verdict"] == "auto_ok"
    assert g.safety_tags(group, "maintenance")["verdict"] == "work_order"
    assert g.safety_tags("elevator_group:admin_low", "set_mode")["verdict"] == "auto_ok"
    # 空压隔离/检修保留群级自动（工厂演示 10:30 空压泄漏链路动作数不变），power_off/shed 仍展开
    for command in ("stop", "maintenance", "isolate_leak", "repair"):
        assert g.safety_tags(air, command)["verdict"] == "auto_ok"
    # 展不开成员：按涉及转工单
    for target in ("elevator_group:unknown_group", "air_system:NO-SUCH", "entity://demo-electronics/elevator_group/nope"):
        result = g.safety_tags(target, "power_off")
        assert result["verdict"] == "work_order" and result["requires_work_order"] and result["notes"]


def test_aggregate_without_policy_keeps_cut_expansion(tmp_path):
    """没有 aggregate_policies 的站点沿用 #201：供电切断类仍展开成员，不能整类 auto_ok（#240 复审）。"""
    source = Path(__file__).resolve().parents[3] / "configs" / "sites" / "factory"
    shutil.copytree(source, tmp_path / "factory")
    graph_yaml = tmp_path / "factory" / "graph" / "graph.yaml"
    config = yaml.safe_load(graph_yaml.read_text(encoding="utf-8"))
    config.pop("aggregate_policies")
    graph_yaml.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    graph = GlobalGraph.load(graph_yaml)

    group = "entity://demo-electronics/elevator_group/admin_low"
    air = "entity://demo-electronics/air_system/AIR"
    assert graph.safety_tags(group, "power_off")["verdict"] == "work_order"
    assert graph.safety_tags(air, "shed")["verdict"] == "work_order"
    # 未配置群级策略时 stop 维持 #201 的群级自动判定
    assert graph.safety_tags(group, "stop")["verdict"] == "auto_ok"


def test_zone_load_surge_keeps_loads_in_zone(g):
    for symptom in ("load_surge", "overload"):
        r = g.find_sources("production", symptom)
        assert {"LINE-1", "LINE-2", "LINE-3"} <= {c["code"] for c in r["candidates"]} and r["domains"]
    assert {"CH-A1", "PE-1", "LT-A01"} <= {c["code"] for c in g.find_sources("admin", "load_surge")["candidates"]}


def test_end_device_impact_includes_what_it_serves(g):
    assert g.trace_impact("PE-1")["floors"] == [f"admin:{f}" for f in range(1, 7)]
    assert {z["zone"]: z["path"] for z in g.trace_impact("CR-AHU1")["zones"]}["cleanroom"] == ["CR-AHU1", "cleanroom"]
    assert g.trace_impact("AIR-HDR", ("power",))["zones"] == []  # 按介质过滤时管网不被当成末端


def test_supplied_empty_store_is_kept(tmp_path):
    from pyoxigraph import Store
    store = Store(str(tmp_path / "kg"))
    graph = GlobalGraph.load(store=store)
    assert graph.store is store and len(store) > 0


def test_priority_safety_first_then_by_zone_and_time(g):
    assert g.priority_rules("admin", "2026-07-01T08:05:00")["order"] == ["safety", "comfort", "production", "cost"]
    assert g.priority_rules("production", "2026-07-01T10:30:00")["order"][:2] == ["safety", "production"]
    assert g.priority_rules("production", "2026-07-01T23:00:00")["order"][:2] == ["safety", "cost"]
    assert g.priority_rules("admin", "2026-07-01T00:05:00Z")["window"] == "admin_peak"  # UTC 00:05 = 厂区 08:05
    assert g.priority_rules("admin", "2026-07-04T08:05:00")["window"] == "default"  # 周六
    assert len(g.priority_rules()["windows"]) == 4


def test_priority_rules_feed_router_hook_with_knowledge_version(g):
    """B25 只读优先级钩子：输入（区域, 时刻）返回当时顺序与知识版本，供 B28 第 3 层裁决。"""
    from plantkg.knowledge.graph.tools import router_hooks

    hook = router_hooks(g)["router.priority"]
    found = hook("admin", "2026-07-01T00:05:00Z")
    expected = g.priority_rules("admin", "2026-07-01T00:05:00Z")
    assert found["order"] == expected["order"] and found["order"][:2] == ["safety", "comfort"]
    assert found["window"] == "admin_peak" and found["knowledge_version"] == g.version
    assert hook(None, "2026-07-01T14:00:00Z")["order"][:2] == ["safety", "cost"]  # 夜间全厂窗口


def _playbook_turtle(spec: dict) -> str:
    body = json.dumps(json.dumps(spec, ensure_ascii=False), ensure_ascii=False)
    return (f'@prefix pk: <https://plantkg.dev/ns/graph#> . '
            f'<entity://demo-electronics/playbook/{spec["id"]}> a pk:Playbook ; '
            f'<http://www.w3.org/2000/01/rdf-schema#label> "{spec["label"]}" ; '
            f'pk:clause <https://plantkg.dev/ns/graph#clause/SR-1.2> ; pk:spec {body} .')


def test_playbook_actions_are_structured_and_versioned(g):
    """预案的可执行动作模板随图谱写，人看的 steps 保留；版本取图谱知识版本。"""
    pb = next(item for item in g.playbooks(event="elevator_stuck") if item["id"] == "elevator_entrapment")
    assert pb["knowledge_version"] == g.version
    assert [action["command"] for action in pb["actions"]["disposal"]] == ["set_mode", "ventilate", "broadcast"]
    assert pb["actions"]["disposal"][1]["minutes"] == 45  # 图谱模板值（代码缺省映射是 60）
    assert pb["actions"]["protection"] == []
    for kind in ("protection", "disposal"):
        assert set(pb["actions"][kind][0] if pb["actions"][kind] else {"role", "command", "params", "minutes"}) <= {
            "role", "command", "params", "minutes", "readiness"}


def test_b30_subgraph_playbook_template_is_schema_checked(g):
    """B30 子图写入的动作模板按同一 schema 校验：合法模板可被同一入口读到，坏模板整体拒绝。"""
    other = GlobalGraph.load()
    good = {"id": "b30_elevator", "label": "B30 新电梯预案", "trigger": {"event": "elevator_stuck"},
            "actions": {"protection": [],
                        "disposal": [{"role": "$event", "command": "ventilate", "params": {"kw": 2.0},
                                      "minutes": 10, "readiness": "needs_human"}]}}
    other.add_subgraph("b30-good", _playbook_turtle(good))
    found = next(item for item in other.playbooks(event="elevator_stuck") if item["id"] == "b30_elevator")
    assert found["actions"]["disposal"][0]["command"] == "ventilate" and found["knowledge_version"] == other.version
    for bad in ({"id": "b30_missing_command", "label": "缺命令", "actions": {"disposal": [{"role": "$event", "minutes": 5}]}},
                {"id": "b30_bad_command", "label": "词表外命令",
                 "actions": {"disposal": [{"role": "$event", "command": "reboot_everything", "minutes": 5}]}},
                {"id": "b30_bad_kind", "label": "坏类别", "actions": {"protect": []}}):
        fresh = GlobalGraph.load()
        with pytest.raises(Exception, match="预案动作模板"):
            fresh.add_subgraph("b30-bad", _playbook_turtle(bad))


def test_playbooks_filter_and_cite_clauses(g):
    pb = g.playbooks(target="FE-2")
    assert [p["id"] for p in pb] == ["elevator_entrapment"] and pb[0]["work_order"] and pb[0]["clause_text"]
    assert [p["id"] for p in g.playbooks(event="pressure_drop")] == ["air_leak"]
    assert len(g.playbooks()) == 5


def test_subgraph_is_queried_together_with_base(g):
    other = GlobalGraph.load()
    other.add_subgraph("b30-demo", "@prefix pk: <https://plantkg.dev/ns/graph#> . "
                       "<entity://demo-electronics/air_header/AIR-HDR> pk:feeds_compressed_air <entity://demo-electronics/production_line/LINE-9> . "
                       "<entity://demo-electronics/production_line/LINE-9> pk:code \"LINE-9\" ; "
                       "<http://www.w3.org/2000/01/rdf-schema#label> \"9# 线\" ; pk:type \"production_line\" ; pk:domain \"energy\" .")
    assert "LINE-9" in {a["code"] for a in other.trace_impact("AIR-HDR")["affected"]}
    assert "LINE-9" not in {a["code"] for a in g.trace_impact("AIR-HDR")["affected"]}
    with pytest.raises(Exception):
        other.add_subgraph("base", "")


def test_tools_are_exposed_over_mcp():
    from fastmcp import Client

    services = build_services(load_settings(profile="mock"))

    async def run():
        async with Client(create_mcp_server(services)) as client:
            names = {t.name for t in await client.list_tools()}
            impact = await client.call_tool("plantkg_trace_impact", {"entity": "AIR-HDR"})
            tags = await client.call_tool("plantkg_safety_tags", {"target": "TX2", "action": "power_off"})
            grid = await client.call_tool("plantkg_trace_impact", {"entity": "GRID"})
            return names, impact.content[0].text, tags.content[0].text, grid.content[0].text

    names, impact, tags, grid = asyncio.run(run())
    assert {"plantkg_trace_impact", "plantkg_find_sources", "plantkg_safety_tags", "plantkg_priority_rules", "plantkg_playbooks"} <= names
    data = json.loads(impact.split("\n", 1)[1])["data"]  # 无 ctx：只读降级，首行是提示
    assert "AIR-HDR → AIR-BR-CR → CR-PNEU → cleanroom" in [z["path"] for z in data["zones"]]
    assert json.loads(tags.split("\n", 1)[1])["data"]["verdict"] == "work_order"
    assert json.loads(grid.split("\n", 1)[1])["data"]["affected_total"] > 30  # 全厂结果截断但不超 8KB


UPSTREAM = {"GRID", "TX2", "LV2", "P-ADMIN", "P-ADMIN-HVAC", "P-ADMIN-LIFT", "P-ADMIN-LT"}


def test_zone_power_loss_returns_only_upstream_supply(g):
    r = g.find_sources("admin", "power_loss")
    ranked = r["candidates"]
    assert {c["code"] for c in ranked} == UPSTREAM and {c["role"] for c in ranked} == {"supply"}
    assert ranked[0]["code"] == "GRID"  # 从源头往下
    assert set(r["domains"]) == {"energy"}  # 电梯、暖通负荷不算供给方


def test_zone_pressure_drop_finds_leaks_on_the_zone_branch(g):
    roles = {c["code"]: c for c in g.find_sources("cleanroom", "pressure_drop")["candidates"]}
    assert roles["AIR-BR-CR"]["role"] == roles["CR-PNEU"]["role"] == "leak"
    assert roles["CR-PNEU"]["path"] == ["AIR-BR-CR", "CR-PNEU", "cleanroom"]
    assert roles["AIR-HDR"]["role"] == roles["AC-1"]["role"] == "supply"
    prod = {c["code"]: c["role"] for c in g.find_sources("production", "pressure_drop")["candidates"]}
    assert prod["AIR-BR-P"] == prod["LINE-1"] == "leak" and prod["AC-1"] == "supply"
    none = g.find_sources("admin", "pressure_drop")
    assert none["candidates"] == [] and none["notes"]


def test_zone_load_surge_paths_start_from_supply_root(g):
    paths = {c["code"]: c["path"] for c in g.find_sources("production", "load_surge")["candidates"]}
    assert paths["LINE-1"] == ["GRID", "TX1", "LV1", "P-PROD", "P-LINE", "LINE-1"]
    assert g.find_sources("admin", "load_surge")["candidates"][0]["path"][:3] == ["GRID", "TX2", "LV2"]
    assert [c["path"] for c in g.find_sources("LINE-1", "load_surge")["candidates"]] == [paths["LINE-1"]]
    park = g.find_sources("park", "load_surge")  # 园区只有光伏、储能
    assert park["candidates"] == [] and park["notes"]


def test_zone_temperature_lists_supply_then_serving_hvac(g):
    cands = g.find_sources("admin", "temperature_deviation")["candidates"]
    roles = {c["code"]: c["role"] for c in cands}
    assert roles["CH-A1"] == roles["CHW-A"] == "supply" and roles["AHU-A01"] == "serving"
    assert not {"PE-1", "LT-A01"} & set(roles)  # 温度偏离只从暖通设备起步
    assert [c["role"] for c in cands] == sorted((c["role"] for c in cands), key=["supply", "serving"].index)


def test_unknown_symptom_is_noted(g):
    assert g.find_sources("AIR-HDR", "vibration")["notes"]
    assert "notes" not in g.find_sources("AIR-HDR", "pressure_drop")


def test_zone_cut_spreads_downstream_across_zones(g):
    tags = {t["id"]: t for t in g.safety_tags("production", "power_off")["tags"]}
    assert tags["hard_climate"]["via"] == "supply" and tags["hard_climate"]["path"][0] == "production"
    assert tags["hard_climate"]["path"] == ["production", "P-PROD", "P-AIR", "AC-1", "AIR-HDR", "AIR-BR-CR"]  # 沿真实供给链
    assert "hard_climate" not in {t["id"] for t in g.safety_tags("production", "setpoint_change")["tags"]}


def test_floor_service_counts_its_zone_and_misc_lookups(g):
    assert {z["zone"]: z["path"] for z in g.trace_impact("EL-A")["zones"]} == {"admin": ["EL-A", "admin"]}
    assert [p["id"] for p in g.playbooks("power_loss", "datacenter")] == ["datacenter_power_loss"]
    with pytest.raises(Exception, match="nope"):
        g.priority_rules("nope", "2026-07-01T08:05:00")
    with pytest.raises(Exception, match="NOPE"):
        g.playbooks(target="NOPE")


def test_subgraph_changes_knowledge_version():
    other = GlobalGraph.load()
    before = other.version
    other.add_subgraph("b30-ver", "<urn:a> <urn:b> <urn:c> .")
    assert other.version != before and other.version.startswith("v1@")
    assert GlobalGraph.load().version == before


def _long_chain(n=40):
    pk = "https://plantkg.dev/ns/graph#"
    lines, prev = [], "<entity://demo-electronics/pneumatic_group/CR-PNEU>"
    for i in range(n):
        node = f"<entity://demo-electronics/pneumatic_group/CHAIN-{i:02d}>"
        lines.append(f"{prev} <{pk}feeds_compressed_air> {node} . {node} <{pk}code> \"CHAIN-{i:02d}\" ; "
                     f"<http://www.w3.org/2000/01/rdf-schema#label> \"长链气动节点 {i:02d}\" ; <{pk}type> \"pneumatic_group\" ; "
                     f"<{pk}domain> \"equipment\" .")
        prev = node
    return "\n".join(lines)


def test_many_safety_tags_are_truncated_not_failing():
    from types import SimpleNamespace

    from plantkg.knowledge.graph import tools

    pk = "https://plantkg.dev/ns/graph#"
    node = "<entity://demo-electronics/pneumatic_group/CR-PNEU>"
    turtle = "\n".join(
        f"{node} <{pk}safetyTag> <{pk}tag/H{i}> . <{pk}tag/H{i}> <{pk}code> \"H{i}\" ; "
        f"<http://www.w3.org/2000/01/rdf-schema#label> \"批量安全标记 {i}\" ; <{pk}actions> \"[\\\"power_off\\\"]\" ; "
        f"<{pk}viaPower> true ; <{pk}occupancy> false ; <{pk}clause> <{pk}clause/SR-3.3> ." for i in range(120))
    big = GlobalGraph.load()
    big.add_subgraph("huge-tags", turtle)
    handlers = tools.make_tools(big)
    ctx = SimpleNamespace(plant_id=big.plant_id)
    impact = handlers["trace_impact"](ctx, "CR-PNEU")
    tags = handlers["safety_tags"](ctx, "CR-PNEU", "power_off")
    for r, key in ((impact, "safety"), (tags, "tags")):
        assert len(r.to_agent_text().encode("utf-8")) <= 8192  # 不再抛 RESULT_TOO_LARGE
        assert r.data[f"{key}_total"] == 121 and r.data["truncated"] is True and 0 < len(r.data[key]) < 121
    assert tags.data["verdict"] == "work_order" and tags.data["requires_work_order"] is True


def test_trace_impact_over_mcp_truncates_long_subgraph_chain(monkeypatch):
    from fastmcp import Client

    from plantkg.knowledge.graph import tools

    big = GlobalGraph.load()
    big.add_subgraph("b30-chain", _long_chain())
    monkeypatch.setattr(tools.GlobalGraph, "load", classmethod(lambda cls, *a, **k: big))
    services = build_services(load_settings(profile="mock"))

    async def run():
        async with Client(create_mcp_server(services)) as client:
            return (await client.call_tool("plantkg_trace_impact", {"entity": "CR-PNEU"})).content[0].text

    text = asyncio.run(run())
    data = json.loads(text.split("\n", 1)[1])["data"]
    assert len(text.encode("utf-8")) <= 8192
    assert data["affected_total"] == 40 and data["truncated"] is True and 0 < len(data["affected"]) < 40
    assert [z["zone"] for z in data["zones"]] == ["cleanroom"]


def _find_sources_over_mcp(location="admin", symptom="power_loss"):
    from fastmcp import Client

    services = build_services(load_settings(profile="mock"))

    async def run():
        async with Client(create_mcp_server(services)) as client:
            return (await client.call_tool("plantkg_find_sources", {"location": location, "symptom": symptom})).content[0].text

    text = asyncio.run(run())
    return text, json.loads(text.split("\n", 1)[1])["data"]


def test_find_sources_over_mcp_keeps_upstream_paths_within_budget():
    text, data = _find_sources_over_mcp()
    assert len(text.encode("utf-8")) <= 8192
    assert data["candidates_total"] == 7 and data["truncated"] is False
    assert {c["code"] for c in data["candidates"]} == UPSTREAM


def test_find_sources_trims_serving_not_supply_when_budget_is_small(monkeypatch):
    from plantkg.knowledge.graph import tools

    monkeypatch.setattr(tools, "TOOL_TEXT_MAX_BYTES", 2600)
    text, data = _find_sources_over_mcp("admin", "temperature_deviation")
    assert len(text.encode("utf-8")) <= 2600
    assert data["truncated"] is True and data["candidates_total"] == 19 and len(data["candidates"]) < 19
    assert {c["code"] for c in data["candidates"] if c["role"] == "supply"} == UPSTREAM - {"P-ADMIN-LIFT", "P-ADMIN-LT"} | {"CH-A1", "CHW-A"}


def test_find_sources_over_mcp_truncates_instead_of_failing_past_real_8kb():
    text, data = _find_sources_over_mcp("GRID", "load_surge")  # 52 项候选，完整输出超过真实 8192 字节上限
    assert len(text.encode("utf-8")) <= 8192
    assert data["truncated"] is True and data["candidates_total"] == 52 and 0 < len(data["candidates"]) < 52
