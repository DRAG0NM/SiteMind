"""B47 分级执行策略（D53/D55）：动作后果 × 自主等级 × 置信度 → 执行 / 抽查 / 先做后审 / 等人批准。

模型读带版本的 PolicyConfig（阈值、自主等级表由 policy 组件发布，可进化）；本模块只做确定性判断，
不调用模型。置信度合成用 Jev 一致性、校准概率、试算可行性与数据可信度四项，权重是本包的 configured 值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 执行结果
EXECUTE = "execute"                    # 自动执行，留记录
EXECUTE_SAMPLING = "execute_sampling"  # 执行并进抽查队列
EXECUTE_REVIEW = "execute_review"      # 先做后审（可回滚的动作，事后逐条复核）
HUMAN_APPROVAL = "human_approval"      # 出方案等人批准（人工待办）
PROPOSE_ONLY = "propose_only"          # L0：只提议，等人一键批准
CORRECTION = "correction"              # 置信度极低：不执行，转人工纠错

REVERSIBLE = "reversible_small"
HARD = "hard_to_reverse"
_LEVELS = ("L0", "L1", "L2", "L3")
# 置信度合成（D59）：校准概率为底，Jev 不一致与读数可疑向下打折；试算不可行直接 0（由调用方先行拦下）。
JEV_DISCOUNT = 0.85
LOW_CREDIBILITY = 0.7
STALE_CREDIBILITY = 0.85


@dataclass(frozen=True)
class PolicyDecision:
    outcome: str
    level: str
    consequence: str
    confidence: float
    band: str
    reason: str


def category_of(event_type: str, entity_type: str | None) -> str:
    return f"{event_type}|{entity_type}" if entity_type else event_type


def autonomy_level(policy: Any, category: str, seen: bool) -> str:
    """自主等级：显式表优先（先精确类别，再事件类型），其次见过的类别取默认 L2，未见类别 L0。"""
    table = getattr(policy, "autonomy_levels", {}) or {}
    if category in table:
        return table[category]
    if "|" in category and category.split("|", 1)[0] in table:
        return table[category.split("|", 1)[0]]
    return getattr(policy, "autonomy_default_level", "L2") if seen else getattr(policy, "autonomy_unseen_level", "L0")


def confidence_band(confidence: float, policy: Any) -> str:
    if confidence >= float(getattr(policy, "confidence_high", 0.7)):
        return "high"
    if confidence >= float(getattr(policy, "confidence_low", 0.3)):
        return "medium"
    return "extreme_low"


def compose_confidence(proposal: Any, *, jev_consistent: bool = True, trial_feasible: bool = True,
                       credibility: float = 1.0) -> tuple[float, dict[str, float]]:
    """置信度合成（D59 数据可信度含新鲜度、Quality 与残差状态）：以校准概率为底，分项向下打折。"""
    calibrated = max(0.0, min(1.0, float(proposal.confidence)))
    jev = 1.0 if jev_consistent else JEV_DISCOUNT
    solver = 1.0 if trial_feasible else 0.0
    fresh = max(0.0, min(1.0, float(credibility)))
    score = calibrated * jev * solver * fresh
    return round(max(0.0, min(1.0, score)), 4), {"calibrated": calibrated, "jev": jev, "solver": solver, "credibility": fresh}


def decide(level: str, consequence: str, confidence: float, policy: Any) -> PolicyDecision:
    """愿景“什么时候自己动手”的矩阵；阈值与等级都从 policy 组件来。"""
    band = confidence_band(confidence, policy)
    level = level if level in _LEVELS else "L0"
    reversible = consequence == REVERSIBLE
    if band == "extreme_low":
        return PolicyDecision(CORRECTION, level, consequence, confidence, band,
                              f"置信度 {confidence:.2f} 低于 {getattr(policy, 'confidence_low', 0.3)}：不执行，转人工纠错")
    if level == "L0":
        return PolicyDecision(PROPOSE_ONLY, level, consequence, confidence, band, "L0 只提议，等人一键批准")
    if level == "L1":
        if reversible and band in ("high", "medium"):
            return PolicyDecision(EXECUTE_REVIEW, level, consequence, confidence, band, "L1 可逆动作先做后审")
        return PolicyDecision(HUMAN_APPROVAL, level, consequence, confidence, band, "L1 难逆动作等人批准")
    if level == "L2":
        if reversible:
            return (PolicyDecision(EXECUTE, level, consequence, confidence, band, "L2 可逆动作、置信度高：自动执行")
                    if band == "high" else
                    PolicyDecision(EXECUTE_SAMPLING, level, consequence, confidence, band, "L2 可逆动作、置信度中：执行并抽查"))
        return (PolicyDecision(EXECUTE, level, consequence, confidence, band, "L2 难逆动作、置信度高：执行并留记录")
                if band == "high" else
                PolicyDecision(HUMAN_APPROVAL, level, consequence, confidence, band, "L2 难逆动作、置信度中：等人批准"))
    # L3
    if reversible:
        return PolicyDecision(EXECUTE, level, consequence, confidence, band, "L3 可逆动作：自动执行，只留记录")
    return (PolicyDecision(EXECUTE, level, consequence, confidence, band, "L3 难逆动作、置信度高：自动执行")
            if band == "high" else
            PolicyDecision(EXECUTE_SAMPLING, level, consequence, confidence, band, "L3 难逆动作、置信度中：执行并抽查"))


def queue_kind(outcome: str) -> str | None:
    if outcome == CORRECTION:
        return "correction"
    if outcome == EXECUTE_SAMPLING:
        return "sampling"
    if outcome == EXECUTE_REVIEW:
        return "review"
    return None

