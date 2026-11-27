"""用独立的可见节日日历补齐政策候选，仍需人工复核与发布确认。"""
from datetime import date
import re

from plantkg.core.contracts import ErrorCode, PlantkgError, ToolResult
from plantkg.core.ports import AssetContentReader, AssetRepository


def install_tariff_review(registry, candidates):
    assets = registry.resolve(AssetRepository)
    reader = registry.resolve(AssetContentReader)

    def prepare(context, candidate_id, calendar_asset_version, subject_id, category, effective_from):
        candidate = candidates.get(candidate_id)
        assets.get(context, candidate.source_asset_version)
        assets.get(context, calendar_asset_version)
        draft = candidate.draft
        periods = draft.get("suggested_periods") or []
        deep_text = draft.get("deep_valley_text") or ""
        deep_window = re.search(r"(\d{2}:\d{2})-(\d{2}:\d{2})", deep_text)
        ratio = draft.get("ratios", {}).get("deep_valley")
        if (draft.get("kind") != "tariff_tou_partial" or len(periods) != 8 or not deep_window or not ratio):
            raise PlantkgError(ErrorCode.PRECONDITION, "政策时段或深谷条款尚未完整抽取")
        try:
            text = reader.read_content(context, calendar_asset_version).decode("utf-8")
        except UnicodeDecodeError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "节日日历须为 UTF-8 文本") from None
        dates, through, labels = [], None, set()
        for number, line in enumerate(text.splitlines(), 1):
            match = re.fullmatch(r"\s*(春节|五一|国庆)\s*[:：]\s*(\d{4}-\d{2}-\d{2}(?:\s*[、,，]\s*\d{4}-\d{2}-\d{2})*)\s*", line)
            if match:
                labels.add(match.group(1))
                dates.extend((value, number) for value in re.findall(r"\d{4}-\d{2}-\d{2}", match.group(2)))
            end = re.fullmatch(r"\s*登记至\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s*", line)
            if end:
                if through is not None:
                    raise PlantkgError(ErrorCode.INVALID_REQUEST, "节日日历只能给一个登记截止日")
                through = end.group(1)
        try:
            parsed = [date.fromisoformat(value) for value, _ in dates]
            end_date = date.fromisoformat(through) if through else None
        except ValueError:
            raise PlantkgError(ErrorCode.INVALID_REQUEST, "节日日历含无效日期") from None
        if labels != {"春节", "五一", "国庆"} or not dates or len(set(parsed)) != len(parsed) or not end_date or max(parsed) > end_date:
            raise PlantkgError(ErrorCode.PRECONDITION, "日历须列明春节、五一、国庆日期与覆盖截止日，日期不得重复")
        calendar_lines = sorted({number for _, number in dates})
        policy_locator = (f"政策摘要第 {draft['deep_valley_line']} 行" if draft.get("deep_valley_line") is not None
                          else str(candidate.locator.model_dump(mode="json", exclude_none=True)))
        periods = list(periods) + [{"name": "deep_valley", "dates": sorted(value for value, _ in dates), "through": through,
                  "windows": [list(deep_window.groups())], "ratio": ratio["value"],
                  "locator": f"{policy_locator}；日历 {calendar_asset_version} 第 {calendar_lines} 行"}]
        revision = {"subject_id": subject_id, "effective_from": effective_from, "calendar_refs": [calendar_asset_version],
                    "flat_factor": 1.0, "tariff_condition": {"timezone": "Asia/Shanghai", "category": category, "periods": periods}}
        return ToolResult(ok=True, business_status="done", data={"candidate_id": candidate_id, "revision": revision},
                          notice="这是待复核建议；须核对类别、日期、覆盖范围和原文后再确认")

    registry.register_tool("prepare_tariff_review", prepare)
