"""值守层（B27）：主 Agent 的值守程序——程序准入、事件流订阅、快慢双通道研判、事件单与派发。

值守循环常驻 plantkg：事件单、订阅、通道、批次与试探状态存 SQLite；主 Agent（LLM）按需一次独立 Nexent 运行。
本包只依赖契约与注册表，不 import 会诊（B29）、裁决（B28）与分级执行（B47）。
"""

from plantkg.agent.router.proposals import SqliteProposalSink
from plantkg.agent.router.recall import SCHEMES, Recall
from plantkg.agent.router.router import (
    EVENT_DOMAINS, EXPERTS_HOOK, FORK_HOOK, INSTRUCTION_HOOK, INVESTIGATE_KEY, PROBE_HOOK, PROPOSALS_READY_HOOK,
    REPLIES_READY_HOOK, ROUTER_HOOK, STEP_HOOK, URGENT_HOOK, Admission, Batch, Dispatch, Handled, Router, RouterConfig, install,
)
from plantkg.agent.router.severity import EventTypeRuleMatcher, SeverityEstimator, load_severity_config
from plantkg.agent.router.standin import SyntheticConsilium
from plantkg.agent.router.store import RouterStore
from plantkg.agent.router.tools import make_tools
from plantkg.agent.router.triage import TRIAGE_KEY, DeterministicInvestigation, DeterministicTriage

__all__ = [
    "EVENT_DOMAINS", "EXPERTS_HOOK", "FORK_HOOK", "INSTRUCTION_HOOK", "INVESTIGATE_KEY", "PROBE_HOOK",
    "PROPOSALS_READY_HOOK", "REPLIES_READY_HOOK", "ROUTER_HOOK", "SCHEMES", "STEP_HOOK", "TRIAGE_KEY", "URGENT_HOOK", "Admission",
    "Batch", "DeterministicInvestigation", "DeterministicTriage", "Dispatch", "EventTypeRuleMatcher", "Handled", "Recall",
    "Router", "RouterConfig", "RouterStore", "SeverityEstimator", "SqliteProposalSink", "SyntheticConsilium", "install",
    "load_severity_config", "make_tools",
]
