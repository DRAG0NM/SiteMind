"""B16：默认服务装配下经真实 MCP 工具与 HTTP 路由读取非空执行账本。

任务卡要求 `execution ledger provider` 以真实请求验证：分页、知识截止可见性、服务重启后仍可读。
这里用的是明确标记的合成材料（``synthetic/test_only``）：求解回执由测试构造并经
``SolveReceiptStore`` 持久化，执行配置为合成 ``config://synthetic/*``，正式规则、电价与校准待业务验收。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

from plantkg.api import create_app
from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Action, ErrorCode, Interval, PlantkgError, Ref, SourceBundle, SourceRecord
from plantkg.decision.gate.execution_basis import StoredExecutionBasisProvider
from plantkg.decision.gate.policy import RuleRequirement
from plantkg.decision.planner.receipt import SolveReceipt, SolveReceiptStore
from plantkg.mcp_server import create_mcp_server
from plantkg.world.replay.series import online_record_hash
from tests.gate.test_execution_snapshots import _pack_inputs
from tests.gate.test_policy import complete_materials
from tests.packs.pv.test_pv import cap_rule
from tests.versions.conftest import PLANT, REVIEWER, context

SLOT = timedelta(minutes=15)
CAP = 8.0
EXECUTED = 8


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _tool(services, name: str, arguments: dict) -> str:
    async def call() -> str:
        async with Client(create_mcp_server(services)) as client:
            return (await client.call_tool(name, arguments)).content[0].text
    return asyncio.run(call())


def _pages(client, run_id: str, page_size: int) -> tuple[list[dict], dict]:
    items: list[dict] = []
    cursor = None
    while True:
        params = {"kind": "execution", "page_size": str(page_size)}
        if cursor:
            params["cursor"] = cursor
        response = client.get(f"/api/v1/runs/{run_id}/ledger", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        items.extend(body["items"])
        cursor = body.get("next_cursor")
        if not cursor:
            return items, body


def _starts(items: list[dict]) -> list[datetime]:
    return [_instant(item["entries"][0]["interval"]["start"]) for item in items]


def test_default_assembly_serves_non_empty_ledger_over_http_and_mcp(tmp_path):
    original, materials = complete_materials()
    start = original.valid_interval.start
    settings = load_settings(profile="dev", overrides={
        "storage.enabled": True,
        "storage.sqlite": str(tmp_path / "catalog.db"),
        "storage.graph": str(tmp_path / "graph"),
        "storage.series": str(tmp_path / "series"),
        "world.default_as_of": (start + timedelta(hours=4)).isoformat()})

    services = build_services(settings)
    try:
        versions, runs, series = services.version_run.versions, services.version_run.runs, services.storage.series
        artifacts, writer = services.version_run.integration_artifacts, services.storage.writer

        # 合成 96 槽 load/pv 观测：与 test_record_executor_integration 相同的槽型。
        records = tuple(SourceRecord(source_id=name, source_version="synthetic-v1", content_hash="sha256:" + char * 64,
            locator={"line_start": i + 1}, nature="measured", event_time=start + SLOT * i,
            interval=Interval(start=start + SLOT * i, end=start + SLOT * (i + 1)),
            available_at=start + SLOT * (i + 1), source_tz="UTC", source_unit="kW", quantity=name, unit="kW",
            value=(1 if name == "load" and i in (1, 3) else 10 if name == "load" else 9 if i == 0 else 20 if i == 3 else 0))
            for name, char in (("load", "a"), ("pv", "b")) for i in range(96))
        entries = []
        for head in (records[0], records[96]):
            bundle = SourceBundle(bundle_id=head.source_id, source_id=head.source_id,
                                  source_version=head.source_version, mapping_version="test",
                                  records=tuple(r for r in records if r.source_id == head.source_id))
            entries.append(dict(quantity=head.quantity, ref=series.write_partition(bundle), source_id=head.source_id,
                                source_version=head.source_version, basis="synthetic/test_only"))
        component = artifacts.prepare_series_map(entries)
        manifest = versions.prepare({"data_sources": component}, "synthetic")
        versions.check_integrity(manifest.version_manifest_id)
        versions.activate(None, manifest.version_manifest_id, REVIEWER, "synthetic")
        base = context(plant_id=PLANT, version_manifest_id=manifest.version_manifest_id)
        run = runs.create_run(base, "replay_day", original.valid_interval, inputs={
            "online_source_ids": [r.source_id for r in records],
            "online_record_hashes": [online_record_hash(r) for r in records],
            "execution_input_mode": "online_observed", "execution_quantities": {"load": "load", "pv": "pv"}})
        ctx = base.model_copy(update={"run_id": run.run_id})

        battery = f"entity://{PLANT}/battery/B1"
        operation = f"entity://{PLANT}/operation/Op1"

        def action(action_id, target, quantity, value, index, unit="kW"):
            span = Interval(start=start + SLOT * index, end=start + SLOT * (index + 1))
            ref = Ref(kind="config", id="config://synthetic/operations" if quantity.startswith("operation")
                      else "config://synthetic/storage")
            return Action(action_id=action_id, object=target, quantity=quantity, unit=unit, value=value,
                          interval=span, refs=(ref,))

        plan = original.model_copy(update={"plan_id": "transport-ledger", "run_id": run.run_id,
                                           "version_manifest_id": manifest.version_manifest_id,
                                           "status": "draft", "revision": 0, "actions": (
            action("discharge", battery, "discharge_power", 2, 1),
            action("charge", battery, "charge_power", 1, 2),
            action("discharge-day", battery, "discharge_power", 2, 3),
            action("op-start", operation, "operation_start", 1, 6, "count"),
            action("op-power-0", operation, "operation_power", 2, 6),
            action("op-power-1", operation, "operation_power", 4, 7))}, deep=True)
        rule = cap_rule(CAP, effective_from=start - timedelta(days=1), available_at=ctx.as_of)
        request = materials.request.model_copy(update={"context": ctx, "rule_ids": (rule.rule_id,)})
        forecasts = tuple(f.model_copy(update={"params": f.params | {
            "run_id": run.run_id, "version_manifest_id": manifest.version_manifest_id}}) for f in materials.forecasts)
        baseline = replace(materials.baseline, plan=plan.model_copy(update={"plan_id": "baseline", "policy": "B0"}))
        provided = [replace(materials, request=request, forecasts=forecasts, baseline=baseline, rules=(rule,),
                            rule_requirements=(RuleRequirement(rule.rule_id, plan.valid_interval),),
                            verified_refs=frozenset(("config", ref) for ref in (
                                rule.config_ref, "config://synthetic/storage", "config://synthetic/export",
                                "config://synthetic/tariff", "config://synthetic/operations")))]
        config = {
            "pv": {"entity_id": rule.subject_ids[0], "capacity_kw": 500, "ref": rule.config_ref},
            "storage": {"entity_id": battery, "capacity_kwh": 100, "charge_kw": 20, "discharge_kw": 20,
                        "eta_c": .9, "eta_d": .9, "soc_min": .1, "soc_max": .9, "reserve_ratio": 0,
                        "ref": "config://synthetic/storage"},
            "export": {"limit_kw": 0, "ref": "config://synthetic/export"},
            "tariff": {"unit": "CNY/kWh", "ref": "config://synthetic/tariff",
                       "slots": [{"start": start.isoformat(), "end": plan.valid_interval.end.isoformat(),
                                  "price_cny_per_kwh": 1}]},
            "operations": {"complete": True, "items": [{"operation_id": "op1", "entity_id": operation,
                                                        "profile_kw": [2, 4],
                                                        "original_start": (start + timedelta(minutes=60)).isoformat()}]}}
        receipt = SolveReceipt.create({
            "request": request.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
            "rules_by_pack": {"pv": [rule.model_dump(mode="json")]},
            "pack_evidence": _pack_inputs(config, plan.version_manifest_id),
            "contributions": {name: {} for name in request.packs},
            "snapshot": {"run_id": run.run_id, "snapshot_id": plan.snapshot_id,
                         "version_manifest_id": plan.version_manifest_id, "as_of": ctx.as_of.isoformat(),
                         "pack_states": {"storage": {"energy_kwh": 50}}},
            "forecasts": {"forecasts": [f.model_dump(mode="json") for f in forecasts]},
            "export_limit_kw": 0, "export_ref": "config://synthetic/export"})
        store = SolveReceiptStore(writer, runs, versions, verifier=lambda _ctx, _receipt: True)
        store.save(ctx, receipt)
        gate = services.gate
        gate.materials_provider = lambda _plan, _materials: provided[0]
        gate.execution_basis_provider = StoredExecutionBasisProvider(
            store, lambda _ctx, _plan, _receipt: (config, "c" * 64), lambda *_args: True)
        gate.register(plan, ctx)
        pending = gate.propose(plan.plan_id, 0, "synthetic", ctx)
        services.version_run.actions.confirm(pending.action_id, REVIEWER, "accept", 0, "synthetic-transport")

        results = [services.executor.execute(gate.executable(plan.plan_id), ctx, index, f"transport-{index}")
                   for index in range(EXECUTED)]
        assert [result.event_seq for result in results] == list(range(1, EXECUTED + 1))

        # 动作只追加执行记录：历史观测不被改写，也不存在业务事实覆盖更新。
        replay = series.query(ctx.model_copy(update={"as_of": original.valid_interval.end}),
                              "load", original.valid_interval)
        assert [online_record_hash(record) for record in replay.records] == [
            online_record_hash(record) for record in records if record.source_id == "load"]
        with writer.read_only() as con:
            assert con.execute("SELECT COUNT(*) FROM b16_executions").fetchone()[0] == EXECUTED
            assert con.execute("SELECT seq FROM replay_clock_history ORDER BY seq DESC LIMIT 1").fetchone()[0] == EXECUTED

        # --- 真实 HTTP：分页读取 8 条已提交执行账本 ---
        client = TestClient(create_app(services))
        try:
            items, last = _pages(client, run.run_id, 3)
            assert len(items) == EXECUTED
            assert _starts(items) == [start + SLOT * index for index in range(EXECUTED)]
            assert all(item["balanced"] for item in items)
            assert all(item["run_id"] == run.run_id and item["kind"] == "execution" for item in items)
            assert _instant(last["as_of"]) == start + SLOT * EXECUTED
            assert last["version_manifest_id"] == manifest.version_manifest_id
            # 计划账本由 B19 提供（b19_plan_ledgers）；这个运行没有规划账本，返回空页而不是失败。
            plan_ledger = client.get(f"/api/v1/runs/{run.run_id}/ledger", params={"kind": "plan"})
            assert plan_ledger.status_code == 200 and plan_ledger.json()["items"] == []
        finally:
            client.close()

        # --- 真实 MCP：绑定运行与清单的会话读到同一非空账本 ---
        def session(as_of):
            return services.version_run.sessions.register(
                REVIEWER, PLANT, as_of, [], 600, "agent-1", "trace-1",
                run_id=run.run_id, version_manifest_id=manifest.version_manifest_id)

        late = session(start + SLOT * EXECUTED)
        body = json.loads(_tool(services, "plantkg_ledger",
                                {"run_id": run.run_id, "kind": "execution", "ctx": late.ctx, "page_size": 5}))
        assert body["ok"] is True and body["business_status"] == "done"
        assert len(body["data"]["items"]) == 5
        assert all(item["balanced"] for item in body["data"]["items"])
        assert all(item["run_id"] == run.run_id and item["kind"] == "execution"
                   for item in body["data"]["items"])
        tail = json.loads(_tool(services, "plantkg_ledger",
                                {"run_id": run.run_id, "kind": "execution", "ctx": late.ctx,
                                 "page_size": 5, "cursor": body["page"]["next_cursor"]}))
        assert len(tail["data"]["items"]) == EXECUTED - 5
        assert tail["page"].get("next_cursor") is None

        # 知识截止：只看得到截止前的槽，且不同截止的游标严格隔离。
        early_at = start + SLOT * 2
        early = session(early_at)
        early_body = json.loads(_tool(services, "plantkg_ledger",
                                      {"run_id": run.run_id, "kind": "execution", "ctx": early.ctx}))
        assert len(early_body["data"]["items"]) == 2
        early_ctx = context(plant_id=PLANT, version_manifest_id=manifest.version_manifest_id,
                            run_id=run.run_id, as_of=early_at)
        with pytest.raises(PlantkgError) as bad_cursor:
            services.registry.tools["ledger"].handler(early_ctx, run.run_id, "execution",
                                                      body["page"]["next_cursor"], 5)
        assert bad_cursor.value.code == ErrorCode.INVALID_REQUEST
        # 未绑定运行的会话读不到任何运行账本。
        unbound_ctx = context(plant_id=PLANT, version_manifest_id=manifest.version_manifest_id,
                              as_of=start + SLOT * EXECUTED)
        with pytest.raises(PlantkgError) as unbound_error:
            services.registry.tools["ledger"].handler(unbound_ctx, run.run_id, "execution", None, 5)
        assert unbound_error.value.code == ErrorCode.PRECONDITION

        # --- 服务重启：同一持久化目录续跑并仍可经传输层读取 ---
        services.close()
        services = build_services(settings)
        assert services.storage.writer.path == writer.path
        resumed = services.executor.execute(services.gate.executable(plan.plan_id), ctx, EXECUTED,
                                            f"transport-{EXECUTED}")
        assert resumed.event_seq == EXECUTED + 1
        client = TestClient(create_app(services))
        try:
            after_restart, _ = _pages(client, run.run_id, 2)
        finally:
            client.close()
        assert len(after_restart) == EXECUTED + 1
        assert _starts(after_restart) == [start + SLOT * index for index in range(EXECUTED + 1)]
    finally:
        services.close()
