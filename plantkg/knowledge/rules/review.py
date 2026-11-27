"""对抽取结果记录分流与复核，不在此处发布或修改活动图谱。"""
from __future__ import annotations

from dataclasses import dataclass
from plantkg.core.contracts import JudgeResult, RuleCandidate


@dataclass(frozen=True)
class ReviewItem:
    candidate: RuleCandidate
    reasons: tuple[str, ...]
    probability: float | None
    impact: float

    @property
    def priority(self) -> tuple[int, float]:
        # 非规划类条目可按依赖范围估计影响；规划规则用 scoring.py 的反事实得分。
        # 未校准的概率不可拿来算期望损失，按架构要求排在队首。
        return (1, 0.0) if self.probability is None else (0, (1 - self.probability) * self.impact)


def route_candidate(candidate: RuleCandidate, judge: JudgeResult, *, has_template: bool,
                    entity_exists: bool, standard_aligned: bool, conflicts: bool,
                    impact: float = 0.0) -> ReviewItem:
    """例外进入 Agent/复核队列；放行仅代表候选可供后续复核。"""
    if judge.candidate_id != candidate.candidate_id:
        raise ValueError("判别结果与候选不匹配")
    if impact < 0:
        raise ValueError("影响面不得为负")
    reasons = []
    if not has_template:
        reasons.append("no_template")
    if not judge.gate_accept:
        reasons.append("judge_gate")
    if not entity_exists:
        reasons.append("missing_entity")
    if not standard_aligned:
        reasons.append("unaligned_standard")
    if conflicts:
        reasons.append("conflicting_rule")
    return ReviewItem(candidate, tuple(reasons), judge.prob, impact)


def next_review_batch(items: list[ReviewItem], limit: int) -> list[ReviewItem]:
    """按未校准优先，再按不确定性乘影响面降序。"""
    if limit < 0:
        raise ValueError("limit 不得为负")
    return sorted((item for item in items if item.reasons), key=lambda item: item.priority, reverse=True)[:limit]
