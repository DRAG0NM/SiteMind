from datetime import datetime, timezone
from pathlib import Path

from plantkg.bootstrap import build_services
from plantkg.core.config import load_settings
from plantkg.core.contracts import Context, ErrorCode, PlantkgError, RunBinding
from plantkg.core.ports import RunStore
from plantkg.core.registry import Registry
from plantkg.integrations.nexent import proposals
from plantkg.experts.hvac.install import SITUATION_HOOK

ROOT = Path(__file__).resolve().parents[3]
AT = datetime(2026, 7, 1, 2, 30, tzinfo=timezone.utc)
BINDING = RunBinding(run_id="run-missing", knowledge_version=1, ticket_id="TK-X", round=1, state_version=0,
                     trigger_at=AT)


class _MissingRuns:
    def create_run(self, context, kind, clock, forked_from=None):
        raise AssertionError("测试不应创建运行")

    def get_run(self, run_id):
        raise PlantkgError(ErrorCode.NOT_FOUND, f"运行 {run_id} 不存在")


def test_equipment_and_elevator_handlers_tolerate_missing_run():
    """评审 #49：运行不存在时设备与电梯入口应像确定性来源一样兜底，而不是报 NOT_FOUND。"""
    services = build_services(load_settings(profile="mock", overrides={"storage.enabled": True}))
    try:
        registry = services.registry
        ctx = Context(plant_id=services.settings.plant_id, industry_pack=services.settings.industry_pack, actor=None,
                      channel="offline", run_id="run-missing", trace_id="t", as_of=AT)
        for name in ("equipment", "elevator"):
            proposal = registry.hooks_for(f"expert_propose:{name}")[-1](ctx, BINDING, None)
            assert proposal.expert == name
    finally:
        services.close()


def test_hvac_handler_tolerates_missing_run():
    registry = Registry()
    registry.load([ROOT / "plantkg/packs/hvac"], ["hvac"])
    registry.register(RunStore, _MissingRuns())
    registry.register_hook(SITUATION_HOOK, lambda context, binding: (
        {"event_type": "hvac_query"}, {"at": AT.isoformat(), "outdoor_c": 30}))
    sink = proposals.MemoryProposalSink()
    proposals.install(registry, lambda: sink, lambda context: (BINDING, None),
                      lambda context, proposal_id: None, placeholders=False)
    ctx = Context(plant_id="demo-electronics", industry_pack="manufacturing", actor=None, channel="offline",
                  run_id="run-missing", trace_id="t", as_of=AT)
    result = registry.tools["hvac_propose"].handler(ctx, focus="午后高温")
    assert result.ok and sink.get(result.data["proposal_id"]).expert == "hvac"
