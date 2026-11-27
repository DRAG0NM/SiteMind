"""从 B26 文本片段提取可追溯的规则草稿。

只识别明确模板；部分政策候选保留缺失项，不能当作已审核规则。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from plantkg.core.contracts import AssetFragment, Context, Locator, RuleCandidate


_PRESSURE = re.compile(
    r"主管压力\s*(?P<minutes>\d+(?:\.\d+)?)\s*分钟内下降超过\s*"
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>MPa|kPa|bar)\b"
)
_TO_BAR = {"MPa": 10.0, "kPa": 0.01, "bar": 1.0}
_TEMPERATURE = re.compile(r"(?P<zone>办公室|行政楼|洁净车间|生产车间)温度\s*(?P<center>\d+(?:\.\d+)?)\s*±\s*(?P<delta>\d+(?:\.\d+)?)\s*℃")
_ELEVATOR = re.compile(r"平均候梯(?:超过|大于)\s*(?P<wait>\d+(?:\.\d+)?)\s*秒或排队(?:超过|大于)\s*(?P<queue>\d+)\s*人时(?P<action>[^。；\n]+)")
_TARIFF_SEASON = re.compile(r"(?P<season>夏冬季|春秋季)（(?P<months>[^）]+)）：(?P<periods>[^。]+)。")
_TARIFF_PERIOD = re.compile(r"(?P<name>峰段|平段|谷段)\s*(?P<times>\d{2}:\d{2}-\d{2}:\d{2}(?:、\d{2}:\d{2}-\d{2}:\d{2})*)")


class DocumentRuleExtractor:
    """实现 RuleExtractor：输入资产片段，输出保留原文与来源的未审核候选。"""

    def extract(self, context: Context, fragments: Sequence[AssetFragment]) -> list[RuleCandidate]:
        candidates: list[RuleCandidate] = []
        for fragment in fragments:
            if fragment.kind != "text" or fragment.text is None or fragment.available_at > context.as_of:
                continue
            try:
                workorder = json.loads(fragment.text)
            except (ValueError, TypeError):
                workorder = None
            if isinstance(workorder, dict):
                reading, original, citations = workorder.get("reading"), workorder.get("candidate"), workorder.get("citations")
                if (isinstance(reading, dict) and isinstance(original, dict) and isinstance(citations, list)
                        and str(original.get("candidate_id", "")).startswith("cand-b26-") and reading.get("rule")):
                    key = fragment.asset_version_id + ":" + original["candidate_id"]
                    candidates.append(RuleCandidate(
                        candidate_id="rule-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
                        draft={"kind": "workorder_rule_partial", "source_text": str(reading["rule"]),
                               "original_candidate_id": original["candidate_id"], "workorder_citations": citations,
                               "missing": ["结构化条件与阈值", "适用实体与标准核对"],
                               "fragment_id": fragment.fragment_id, "available_at": fragment.available_at.isoformat()},
                        extractor_version="b26-workorder-partial-v1", source_asset_version=fragment.asset_version_id,
                        locator=fragment.locator, confidence=None))
                    continue
            for match in _PRESSURE.finditer(fragment.text):
                minutes = float(match.group("minutes"))
                if minutes <= 0:
                    continue
                amount = float(match.group("amount"))
                rate = -amount * _TO_BAR[match.group("unit")] * 60 / minutes
                before = fragment.text[:match.start()]
                line = fragment.locator.line_start
                locator = (Locator(line_start=line + before.count("\n"), line_end=line + before.count("\n"))
                           if line is not None else fragment.locator)
                line_start = fragment.text.rfind("\n", 0, match.start()) + 1
                line_end = fragment.text.find("\n", match.end())
                source_line = fragment.text[line_start:line_end if line_end >= 0 else None].strip()
                key = f"{fragment.asset_version_id}:{fragment.fragment_id}:{match.start()}:{match.group()}"
                candidate_id = "rule-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
                candidates.append(RuleCandidate(
                    candidate_id=candidate_id,
                    draft={"kind": "compressor_pressure_drop", "quantity": "pressure_drop_rate",
                           "unit": "bar/h", "value": rate, "window_minutes": minutes,
                           "source_text": source_line, "comparison": "strictly_below",
                           "fragment_id": fragment.fragment_id,
                           "available_at": fragment.available_at.isoformat()},
                    extractor_version="pressure-template-v1",
                    source_asset_version=fragment.asset_version_id,
                    locator=locator,
                    confidence=None,
                ))
            for match in _TEMPERATURE.finditer(fragment.text):
                center, delta = float(match.group("center")), float(match.group("delta"))
                if delta <= 0:
                    continue
                offset = fragment.text[:match.start()].count("\n")
                start = fragment.locator.line_start
                locator = Locator(line_start=start + offset, line_end=start + offset) if start is not None else fragment.locator
                line_start = fragment.text.rfind("\n", 0, match.start()) + 1
                line_end = fragment.text.find("\n", match.end())
                source_line = fragment.text[line_start:line_end if line_end >= 0 else None].strip()
                key = f"{fragment.asset_version_id}:{fragment.fragment_id}:{match.start()}:{match.group()}"
                candidates.append(RuleCandidate(
                    candidate_id="rule-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
                    draft={"kind": "zone_temperature_limit", "quantity": "temperature", "unit": "degC",
                           "bounds": {"lower": center - delta, "upper": center + delta},
                           "zone_label": match.group("zone"), "source_text": source_line,
                           "fragment_id": fragment.fragment_id, "available_at": fragment.available_at.isoformat()},
                    extractor_version="temperature-template-v1", source_asset_version=fragment.asset_version_id,
                    locator=locator, confidence=None,
                ))
            for match in _ELEVATOR.finditer(fragment.text):
                offset = fragment.text[:match.start()].count("\n")
                start = fragment.locator.line_start
                locator = Locator(line_start=start + offset, line_end=start + offset) if start is not None else fragment.locator
                line_start = fragment.text.rfind("\n", 0, match.start()) + 1
                line_end = fragment.text.find("\n", match.end())
                source_line = fragment.text[line_start:line_end if line_end >= 0 else None].strip()
                time_range = re.search(r"早高峰\s*(\d{2}:\d{2})-(\d{2}:\d{2})（北京时间）", source_line)
                zones = re.search(r"低区服务\s*(\d+)\s*至\s*(\d+)\s*层，高区服务\s*(\d+)\s*至\s*(\d+)\s*层", fragment.text[match.end():match.end() + 200])
                if zones:
                    source_line += "\n" + zones.group()
                    if start is not None:
                        zone_end = match.end() + zones.end()
                        locator = Locator(line_start=start + offset, line_end=start + fragment.text[:zone_end].count("\n"))
                key = f"{fragment.asset_version_id}:{fragment.fragment_id}:{match.start()}:{match.group()}:elevator-v2"
                candidates.append(RuleCandidate(
                    candidate_id="rule-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
                    draft={"kind": "elevator_congestion_rule", "quantity": "wait_time", "unit": "s",
                           "bounds": {"upper": float(match.group("wait"))}, "queue_threshold": int(match.group("queue")),
                           "action": match.group("action"), "source_text": source_line,
                           "time_range": list(time_range.groups()) if time_range else None,
                           "zones": {"low": [int(zones.group(1)), int(zones.group(2))],
                                     "high": [int(zones.group(3)), int(zones.group(4))]} if zones else None,
                           "fragment_id": fragment.fragment_id, "available_at": fragment.available_at.isoformat()},
                    extractor_version="elevator-congestion-v2", source_asset_version=fragment.asset_version_id,
                    locator=locator, confidence=None,
                ))
            seasons = list(_TARIFF_SEASON.finditer(fragment.text))
            if seasons:
                start, end = seasons[0].start(), seasons[-1].end()
                line = fragment.locator.line_start
                locator = (Locator(line_start=line + fragment.text[:start].count("\n"),
                                   line_end=line + fragment.text[:end].count("\n")) if line is not None else fragment.locator)
                seasonal = [{"season": m.group("season"), "months_text": m.group("months"),
                             "periods": [{"category": p.group("name"), "ranges": p.group("times").split("、")}
                                         for p in _TARIFF_PERIOD.finditer(m.group("periods"))]} for m in seasons]
                sharp = re.search(r"尖峰：([^。]+)。", fragment.text)
                deep = re.search(r"春节、五一、国庆假期\s*(\d{2}:\d{2}-\d{2}:\d{2})\s*执行深谷[^。]*。", fragment.text)
                ratios = {}
                for name, pattern, sign in (
                    ("peak", r"峰段上浮\s*(\d+(?:\.\d+)?)%", 1),
                    ("valley", r"谷段下浮\s*(\d+(?:\.\d+)?)%", -1),
                    ("sharp", r"尖峰在峰段电价基础上再上浮\s*(\d+(?:\.\d+)?)%", 1),
                    ("deep_valley", r"深谷在谷段电价基础上再下浮\s*(\d+(?:\.\d+)?)%", -1),
                ):
                    ratio = re.search(pattern, fragment.text)
                    if ratio:
                        ratios[name] = {"value": sign * float(ratio.group(1)) / 100,
                                        "source_text": ratio.group(),
                                        "line": (line + fragment.text[:ratio.start()].count("\n")) if line is not None else None}
                suggested_periods = []
                names = {"峰段": "peak", "平段": "flat", "谷段": "valley"}
                def location(number):
                    return f"摘要第 {number} 行" if number is not None else str(fragment.locator.model_dump(mode="json", exclude_none=True))
                for season_match, season in zip(seasons, seasonal):
                    months = [int(month) for month in re.findall(r"\d+", season["months_text"])]
                    season_line = line + fragment.text[:season_match.start()].count("\n") if line is not None else None
                    for period in season["periods"]:
                        name = names[period["category"]]
                        if name != "flat" and name not in ratios:
                            continue
                        suggested_periods.append({"name": name, "months": months,
                                                  "windows": [window.split("-") for window in period["ranges"]],
                                                  "ratio": ratios[name]["value"] if name != "flat" else 0.0,
                                                  "locator": (location(season_line) + "；浮动比例见" + location(ratios[name]["line"]))
                                                             if name != "flat" else location(season_line)})
                if sharp and "sharp" in ratios:
                    sharp_patterns = ((r"7-8\s*月\s*([\d:、-]+)", [7, 8]),
                                      (r"12\s*月与次年\s*1\s*月\s*([\d:、-]+)", [12, 1]))
                    for pattern, months in sharp_patterns:
                        part = re.search(pattern, sharp.group())
                        if part:
                            sharp_line = line + fragment.text[:sharp.start()].count("\n") if line is not None else None
                            suggested_periods.append({"name": "sharp", "months": months,
                                "windows": [window.split("-") for window in part.group(1).split("、")],
                                "ratio": ratios["sharp"]["value"],
                                "locator": location(sharp_line) + "；浮动比例见" + location(ratios["sharp"]["line"])})
                key = f"{fragment.asset_version_id}:{fragment.fragment_id}:{start}:tariff-v4"
                missing = ["年度假期日期", "逐时段来源及适用类别核对"]
                if len(seasonal) < 2 or any(len(s["periods"]) < 3 for s in seasonal):
                    missing.append("季节时段")
                if not sharp or not deep:
                    missing.append("尖峰或深谷条款")
                if len(ratios) != 4:
                    missing.append("电价浮动比例")
                candidates.append(RuleCandidate(
                    candidate_id="rule-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
                    draft={"kind": "tariff_tou_partial", "seasonal_periods": seasonal,
                           "sharp_text": sharp.group() if sharp else None,
                           "deep_valley_text": deep.group() if deep else None,
                           "deep_valley_line": (line + fragment.text[:deep.start()].count("\n")) if deep and line is not None else None,
                           "ratios": ratios,
                           "suggested_periods": suggested_periods,
                           "missing": missing,
                           "source_text": fragment.text[start:end], "fragment_id": fragment.fragment_id,
                           "available_at": fragment.available_at.isoformat()},
                    extractor_version="tariff-seasonal-partial-v4", source_asset_version=fragment.asset_version_id,
                    locator=locator, confidence=None))
        return candidates
