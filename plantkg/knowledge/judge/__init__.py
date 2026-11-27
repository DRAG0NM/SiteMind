"""B24 派域题型。只依赖 Judge 端口，不跨平面导入后端。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from plantkg.core.contracts import JudgeResult, OntologyCandidate
from plantkg.core.ports import Judge


DOMAIN_DEFINITIONS = {
    "energy": "能源专家：储能、光伏、电价、照明与配电削峰。",
    "elevator": "电梯专家：人梯货梯群控、候梯与拥堵疏导。",
    "hvac": "暖通专家：区域温湿度、预冷错峰与设定温度。",
    "equipment": "设备监测专家：空压机等设备健康、故障研判与维修。",
}


def dispatch_domains(judge: Judge, event_id: str, content: str, *,
                     definitions: Mapping[str, str] | None = None,
                     signals: Mapping[str, Any] | None = None) -> JudgeResult:
    """返回完整领域概率与独立门控，不直接执行动作、不决定安全标记。"""
    definitions = dict(DOMAIN_DEFINITIONS if definitions is None else definitions)
    question = OntologyCandidate(candidate_id=event_id, layer="concept", subject=event_id,
                                 options=tuple(definitions), recall_source="manual")
    state = dict(signals or {}) | {
        "content": content, "definitions": definitions, "gate_kind": "domain",
        "instructions": "Which expert domain best explains this event? Use the domain definitions.",
    }
    return judge.decide(state, question, tuple(definitions))
