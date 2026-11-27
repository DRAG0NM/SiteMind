"""会诊（B29）：跨域会诊——专家并行研判、提案落库、预演与一个声音。

对外入口：
- `install(registry, router=...)`：登记确定性结果来源、`router.experts` / `router.step` 等钩子，返回会诊实例；
- `python -m plantkg.agent.consultation`：第一幕演示（四位专家并行、预演只预判、事件到达现场算并与预演对比）。
"""

from plantkg.agent.consultation.consilium import (
    SERVICE_HOOK, Consultation, Lane, install, register,
)
from plantkg.agent.consultation.preview import (
    PREVIEW_CHECK_KEY, PREVIEW_SIGNALS_HOOK, PREVIEW_STATE_HOOK, Preview, PreviewCheck, PreviewMatch, PreviewSignal, morning_peak,
    scenario_signals, scenario_state,
)
from plantkg.agent.consultation.replies import parse_reply, stale_reason
from plantkg.agent.consultation.sources import EXPERT_KEY, FOLLOW_UP_KEY, DeterministicExpertSource, SkipFollowUp, install_sources

__all__ = [
    "EXPERT_KEY", "FOLLOW_UP_KEY", "PREVIEW_CHECK_KEY", "PREVIEW_SIGNALS_HOOK", "PREVIEW_STATE_HOOK", "SERVICE_HOOK",
    "Consultation", "DeterministicExpertSource", "Lane", "Preview", "PreviewCheck", "PreviewMatch", "PreviewSignal", "SkipFollowUp",
    "install", "install_sources", "morning_peak", "parse_reply", "register", "scenario_signals", "scenario_state",
    "stale_reason",
]
