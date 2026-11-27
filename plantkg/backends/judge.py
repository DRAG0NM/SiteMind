"""三顺序判别与门控。网络仅存在于 JevTransport，默认测试从不联网。"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from plantkg.core.config import ConfigError, require_jev_workspace
from plantkg.core.contracts import (
    ErrorCode, JudgeResult, OntologyCandidate, OrderResult, PlantkgError,
    TopK, UNDECIDABLE_CHOICES,
)

log = logging.getLogger(__name__)


def normalize(values: Mapping[str, float], options: Sequence[str]) -> dict[str, float]:
    if set(values) != set(options):
        raise ValueError("后端分布必须完整覆盖候选；不能补零")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           (isinstance(v, float) and not math.isfinite(v)) or v < 0 for v in values.values()):
        raise ValueError("后端分数必须为有限非负数")
    maximum = max(values.values())
    if maximum <= 0:
        raise ValueError("后端分布总和必须大于零")
    # 缩放后再求和：1e308 的总和不会溢出，超大整数也无需转换为 float。
    scaled = {key: float(Decimal(values[key]) / Decimal(maximum)) for key in options}
    total = math.fsum(scaled.values())
    return {key: scaled[key] / total for key in options}


def unique_top(values: Mapping[str, float]) -> str | None:
    maximum = max(values.values())
    winners = [key for key, value in values.items() if value == maximum]
    return winners[0] if len(winners) == 1 else None


def option_orders(options: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """三种确定性排列；一、两个候选只能产生一、两种，保守转人工。"""
    original = tuple(options)
    if len(original) < 3:
        return tuple(itertools.permutations(original))
    return original, original[1:] + original[:1], tuple(reversed(original))


class BaseJudge:
    backend: str
    model: str

    def __init__(self, *, calibrator=None):
        self.calibrator = calibrator

    def scores(self, state, instructions, definitions, order, index):
        raise NotImplementedError

    def decide(self, state: Mapping[str, Any], question: OntologyCandidate, options: Sequence[str]) -> JudgeResult:
        options = tuple(options)
        if not options or len(options) > 255 or len(set(options)) != len(options):
            raise ValueError("需要 1～255 个互不重复的候选")
        if set(options) != set(question.options):
            raise ValueError("options 必须与题目候选集相同")
        if any(o.strip().lower() in UNDECIDABLE_CHOICES for o in options):
            raise ValueError("不能提供 Unknown/其他候选")
        definitions = state.get("definitions", {})
        if set(definitions) != set(options) or any(not isinstance(v, str) or not v.strip() for v in definitions.values()):
            raise ValueError("每个候选都必须提供一句定义")
        gate_kind = state.get("gate_kind", "ontology")
        if gate_kind not in ("ontology", "domain"):
            raise ValueError("未知门控题型")
        vector_top = state.get("vector_top")
        if vector_top is not None and vector_top not in options:
            raise ValueError("vector_top 必须属于候选集")
        if gate_kind == "domain" and (question.recall_source == "vector" or vector_top is not None):
            raise ValueError("派域题不使用向量召回或 vector_top")
        coverage = state.get("coverage", 1.0 if gate_kind == "domain" else 0.0)
        if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(coverage) or not 0 <= coverage <= 1:
            raise ValueError("coverage 必须在 0～1；不从概率推断召回覆盖")
        instructions = state.get("instructions", "Classify this item using the supplied definitions.")
        orders = []
        for index, order in enumerate(option_orders(options)):
            values = normalize(self.scores(state, instructions, definitions, order, index), options)
            orders.append(OrderResult(order=order, distribution=values, choice=unique_top(values)))
        # 每个分布先归一，平均后再次归一；不让响应舍入质量改变各顺序的权重。
        distribution = normalize({o: sum(r.distribution[o] for r in orders) / len(orders) for o in options}, options)
        ranked = sorted(distribution, key=lambda o: (-distribution[o], o))
        choice = ranked[0]  # 平分时仅作展示；任何平分顺序都会令门控失败。
        agree = len(orders) >= 3 and all(r.choice == choice for r in orders)
        cross = vector_top == choice if vector_top is not None else None
        accept = agree and (gate_kind == "domain" or (question.recall_source == "vector" and cross is True))
        result = JudgeResult(
            candidate_id=question.candidate_id, choice=choice, score=distribution[choice],
            coverage=coverage, backend=self.backend, model=self.model,
            orders=tuple(orders), distribution=distribution, order_agree=agree,
            cross_backend_agree=cross, candidate_recall_source=question.recall_source,
            vector_top=vector_top, gate_accept=accept, gate_kind=gate_kind,
            top_k=tuple(TopK(option=o, prob=distribution[o]) for o in ranked[:3]),
        )
        if self.calibrator is not None:
            prob = self.calibrator.predict(result)
            if prob is not None:
                result = JudgeResult.model_validate(result.model_dump() | {
                    "prob": prob, "calibration_version": self.calibrator.version,
                })
        return result


class FakeJudge(BaseJudge):
    """合成桩。显式分布可构造顺序不一致；缺省分数由输入散列生成。"""

    backend = "fake"
    model = "synthetic-sha256-v1"

    def scores(self, state, instructions, definitions, order, index):
        fixtures = state.get("fake_distributions")
        if fixtures is not None:
            if len(fixtures) != len(option_orders(order)):
                raise ValueError("fake_distributions 必须逐一对应实际顺序数")
            return fixtures[index]
        seed = json.dumps(state.get("content", ""), ensure_ascii=False, sort_keys=True)
        return {o: int(hashlib.sha256((seed + o).encode()).hexdigest()[:8], 16) + 1 for o in order}


class FallbackJudge(BaseJudge):
    """实施选择：向量相似度 softmax 保底；派域由上游显式提供 domain_scores。

    团队保底算法未在任务卡写定。缺分数明确报错，不伪造向量、不隐式改用 fake。
    """

    backend = "fallback"
    model = "vector-softmax-v1"

    def scores(self, state, instructions, definitions, order, index):
        key = "domain_scores" if state.get("gate_kind") == "domain" else "vector_scores"
        values = state.get(key, {})
        if set(values) != set(order) or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in values.values()):
            raise ValueError(f"保底后端需要完整有限的 {key}")
        if key == "vector_scores" and state.get("vector_top") != unique_top(values):
            raise ValueError("vector_top 与 vector_scores 的唯一首选不一致")
        maximum = max(values.values())
        return {o: math.exp(values[o] - maximum) for o in order}


class JevTransport:
    """北京 System One 专用传输；密钥仅从环境变量读取，错误不回显响应体。"""

    def __init__(self, *, workspace_env="BAILIAN_WS", api_key_env="DASHSCOPE_API_KEY", timeout_s=10, region="cn-beijing"):
        if region != "cn-beijing":
            raise ConfigError("Jev 当前仅接入 cn-beijing")
        if not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 10:
            raise ConfigError("Jev timeout_s 必须在 (0,10]，三顺序总预算不超过 30 秒")
        self.workspace_env, self.api_key_env, self.timeout_s = workspace_env, api_key_env, timeout_s

    def __call__(self, body):
        workspace = require_jev_workspace(self.workspace_env)
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise ConfigError(f"缺少环境变量 {self.api_key_env}")
        url = f"https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone"
        request = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(),
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.load(response)
        except (OSError, ValueError) as exc:
            status = f" HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else ""
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, f"Jev 请求失败{status}；未改用其他后端") from None


class JevJudge(BaseJudge):
    backend = "jev"

    def __init__(self, *, model="decision-model-preview", transport=None, calibrator=None):
        super().__init__(calibrator=calibrator)
        self.model = model
        self.transport = transport if transport is not None else JevTransport()

    def scores(self, state, instructions, definitions, order, index):
        # 使用位置键重新映射，确保顺序变化实际送达模型，而非只改变本地列表。
        keys = {f"o{i}": option for i, option in enumerate(order)}
        try:
            response = self.transport({"model": self.model, "state": state.get("content", ""), "questions": {
                "decision": {"type": "choice", "instructions": instructions,
                             "criteria": {key: f"{option}: {definitions[option]}" for key, option in keys.items()}},
            }})
        except ConfigError as exc:
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, str(exc)) from None
        try:
            answer = response["answers"]["decision"]
            probabilities = answer["probabilities"]
            if answer["type"] != "choice" or set(probabilities) != set(keys):
                raise ValueError
            values = {option: probabilities[key] for key, option in keys.items()}
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in values.values()):
                raise ValueError
            if sum(values.values()) <= 0:
                raise ValueError
            return values
        except (KeyError, TypeError, ValueError):
            raise PlantkgError(ErrorCode.DEPENDENCY_UNAVAILABLE, "Jev 返回无效或不完整的 choice 概率分布") from None


def create_judge(config, *, transport=None, calibrator=None):
    """由配置切换，不静默降级；依赖传入便于离线验证真实协议。"""
    if config.backend == "fake":
        return FakeJudge(calibrator=calibrator)
    if config.backend == "fallback":
        log.warning("judge.backend=fallback：该后端不提供派域分数，B27 派域将退化为确定性权重")
        return FallbackJudge(calibrator=calibrator)
    if config.backend == "jev":
        jev = config.jev
        if jev.get("orders", 3) != 3:
            raise ConfigError("B24 固定使用三种顺序")
        transport = transport if transport is not None else JevTransport(
            workspace_env=jev.get("workspace_env", "BAILIAN_WS"),
            api_key_env=jev.get("api_key_env", "DASHSCOPE_API_KEY"),
            region=jev.get("region", "cn-beijing"), timeout_s=jev.get("timeout_s", 10),
        )
        return JevJudge(model=jev.get("model", "decision-model-preview"), transport=transport, calibrator=calibrator)
    raise ConfigError("未知 judge.backend")
