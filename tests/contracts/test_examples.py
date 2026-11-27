"""有效样本与反例：pydantic 契约和生成的 JSON Schema 两边都要一致。"""

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from plantkg.core import contracts as c
from tests.contracts.conftest import CONTRACTS, load_json

VALID = sorted((CONTRACTS / "examples" / "valid").glob("*.json"))
INVALID = sorted((CONTRACTS / "examples" / "invalid").glob("*.json"))


def schema_validator(model: str) -> Draft202012Validator:
    schema = load_json(CONTRACTS / "schemas" / f"{model}.schema.json")
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_examples_exist():
    assert len(VALID) >= 40 and len(INVALID) >= 40


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_example(path):
    sample = load_json(path)
    model = getattr(c, sample["model"])
    model.model_validate(sample["data"])
    errors = list(schema_validator(sample["model"]).iter_errors(sample["data"]))
    assert not errors, [e.message for e in errors]


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_example(path):
    sample = load_json(path)
    model = getattr(c, sample["model"])
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate(sample["data"])
    assert sample["expect_error"] in str(excinfo.value)


def test_counterexamples_cover_required_semantics():
    """任务卡要求：缺单位/时间/来源/审核字段的反例都在。"""
    names = {p.stem for p in INVALID}
    for required in ("source_record_no_unit", "source_record_naive_time", "rule_no_source", "rule_no_review_status",
                     "rule_approved_no_reviewer", "rule_no_available_at", "tool_result_echo_ctx", "evidence_echo_ctx"):
        assert required in names


def test_every_record_has_valid_example_or_is_embedded():
    covered = {load_json(p)["model"] for p in VALID}
    embedded = {"Actor", "Interval", "Ref", "Locator", "TransformStep", "Quality", "ProposalOrigin", "JobBudget", "SourceBundle",
                "TimeMap", "EvidenceRef", "OrderResult", "TopK", "Bounds", "InputSlice", "ForecastBundle", "SolverBudget",
                "SolverInfo", "ExecutedAction", "ComponentRef", "EvidencePage", "NexentEventRef", "ToolError", "PageInfo",
                "ParamSpec", "ErrorResponse", "AgentRunAccepted", "RouteSpec", "StorageParams",
                # v0.8（B46）：嵌在提案、试算与策略样例里
                "DataSourceRef", "RunBinding", "AgentTiming", "ProposedAction", "ProposalCost", "TrialViolation", "ResponseBudget",
                # #69（B04 只读接口）：端口返回的队列快照与执行器健康，没有独立样例
                "JobQueueSnapshot", "JobExecutorHealth"}
    # “一天”的各部分由 contracts/examples/day.sample.json 整体覆盖（tests/contracts/test_v08.py）
    day = {"DayRecord", "DayEvent", "TicketSubscription", "Ticket", "DecisionConflict", "DecisionConversion", "DecisionResult",
           "DayDecision", "WorkOrder", "TempRule", "KnowledgeVersionEntry", "QaEntry", "AgentRunRecord", "AgentRunNode", "AgentToolCall", "DayMetric",
           "RunChainEntry", "StateChange", "StateFrame", "StatusChange", "DayContext", "AssetStateCounts"}
    missing = set(c.record_models()) - covered - embedded - day
    assert not missing, f"缺少有效样本：{sorted(missing)}"


def test_day_metric_variant_is_optional_and_unique_per_group_metric(tmp_path):
    """#235：variant 可选；(group, metric, variant) 在一份日导出里唯一，不再用 @ 后缀。"""
    import json
    from pathlib import Path
    from pydantic import ValidationError
    from plantkg.core.contracts import DayMetric, DayRecord
    day = json.loads((Path(__file__).resolve().parents[2] / "contracts/examples/day.sample.json").read_text(encoding="utf-8"))
    assert DayMetric(metric="harness_pass_rate", group="coordinated", value=None).variant is None
    day["metrics"] = [{"metric": "harness_pass_rate", "group": "coordinated", "value": None, "variant": v} for v in ("基线", "修复后")]
    DayRecord.model_validate(day)
    day["metrics"].append(dict(day["metrics"][0]))
    with pytest.raises(ValidationError, match="唯一"):
        DayRecord.model_validate(day)
