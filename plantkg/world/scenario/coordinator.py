"""时间协调器（B17）：仿真时钟的唯一推进者如何等专家、何时让动作生效、哪些结果算迟到。

- 生效时刻固定为 trigger_at + policy.response_budget（第 1、2 层用 layer12_min），与模型实际快慢无关。
- live：同步等待（仿真时钟停住），墙钟截止 120 秒，超时或出错重试一次，仍失败按失败处理（事件单标受阻）。
- trial / evaluation：不等墙钟，结果经 AgentResultSource 按 run_mode 取得（缺登记回退 deterministic）。
- 墙钟的 requested_at、returned_at、latency_ms、重试次数逐条进 AgentRunRecord，只作报告。
- 迟到：绑定的 run_id 或 round 已过期、state_version 变化超出容差、effective_at 早于当前分钟（所在槽已汇总或分钟已结算）
  的结果只入历史，不执行、不回写。过期判断用本轮 trigger_at，不用 origin_event_at。
"""
from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from plantkg.core.contracts import AgentResultKind, AgentRunRecord, AgentTiming, ExpertProposal, PlantkgError, PolicyConfig, RunBinding
from plantkg.core.ports import AgentResultSource
from plantkg.core.versions import utcnow

WALL_DEADLINE_S = 120.0  # B23：单次运行墙钟截止
RETRIES = 1


@dataclass(frozen=True)
class AgentOutcome:
    proposal: ExpertProposal | None
    record: AgentRunRecord
    error: str | None = None  # 有值即本次运行失败或超时（事件单标受阻）；proposal 可能来自确定性回退


def _call_with_deadline(call: Callable[[], Any], deadline_s: float) -> Any:
    """在守护线程里调用，超过墙钟截止抛 TimeoutError；超时的调用留在后台，不阻塞仿真也不阻塞进程退出。"""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = call()
        except BaseException as exc:  # noqa: BLE001 交回调用方线程再抛
            box["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(deadline_s)
    if thread.is_alive():
        raise TimeoutError
    if "error" in box:
        raise box["error"]
    return box["result"]


def ceil_minute(t: datetime) -> datetime:
    floor = t.replace(second=0, microsecond=0)
    return floor if floor == t else floor + timedelta(minutes=1)


def _require_proposal(result: Any) -> None:
    """结果源的业务状态与提案校验：失败状态或没有可用提案就抛错，交给调用方的失败处理。"""
    if not isinstance(result, Mapping):
        raise ValueError(f"结果源返回 {type(result).__name__}，不是结果映射")
    status = result.get("status")
    if status not in (None, "finished"):
        raise ValueError(f"结果源状态 {status}，没有可执行提案")
    if not result.get("proposal"):
        raise ValueError(f"结果源没有给出提案（status={status or '未标注'}）")
    ExpertProposal.model_validate(result["proposal"])


class TimeCoordinator:
    def __init__(self, registry: Any, run_mode: str, policy: PolicyConfig, wall: Callable[[], datetime] = utcnow,
                 deadline_s: float = WALL_DEADLINE_S, retries: int = RETRIES) -> None:
        self.registry, self.run_mode, self.policy, self.wall = registry, run_mode, policy, wall
        self.deadline_s, self.retries = deadline_s, retries
        self.next_id: Callable[[str], str] = lambda prefix: f"{prefix}-{secrets.token_hex(6)}"  # runner 换成按运行链计数

    def effective_at(self, trigger_at: datetime, layer: int = 12) -> datetime:
        budget = self.policy.response_budget.layer12_min if layer == 12 else self.policy.response_budget.layer3_min
        return trigger_at + timedelta(minutes=budget)

    def request(self, expert: str, binding: RunBinding, payload: Mapping[str, Any], origin_event_at: datetime) -> AgentOutcome:
        effective = self.effective_at(binding.trigger_at)
        live = self.run_mode == "live"
        attempts = 1 + (self.retries if live else 0)
        requested_at = self.wall()
        result, error, status, retries, returned_at = None, None, "failed", 0, None
        source_kind = "deterministic"
        for attempt in range(attempts):
            retries = attempt
            started = self.wall()
            try:
                source = self.registry.resolve_mode(AgentResultSource, self.run_mode, key=expert)
                source_kind = getattr(source, "source_kind", "deterministic")
                call = lambda: source.get(AgentResultKind.expert, binding, dict(payload) | {"expert": expert})  # noqa: E731
                if live:
                    result = _call_with_deadline(call, self.deadline_s)
                else:
                    result = call()
                source_kind = result.get("source", getattr(source, "source_kind", source_kind))
                returned_at = self.wall()  # 先记返回时刻：受阻等失败结果也要留下真实耗时
                _require_proposal(result)  # 受阻、失败或缺提案的结果按失败处理：照常重试、回退与记录
                if live and (returned_at - started).total_seconds() > self.deadline_s:
                    result, error, status = None, f"墙钟 {(returned_at - started).total_seconds():.0f} 秒超过截止 {self.deadline_s:.0f} 秒", "timeout"
                    continue
                status, error = "finished", None
                break
            except TimeoutError:
                result, error, status = None, f"墙钟超过截止 {self.deadline_s:.0f} 秒", "timeout"
            except PlantkgError as exc:
                result, error, status = None, str(exc), "failed"
            except Exception as exc:  # 模型连接中断等：同样重试一次后按失败处理
                result, error, status = None, f"{type(exc).__name__}: {exc}", "failed"
        fallback = None
        if result is None and live:
            # 运行失败只让事件单受阻；ready 的确定性动作照常生效（D46“运行有截止”）
            try:
                det = self.registry.resolve_mode(AgentResultSource, "deterministic", key=expert)
                fallback = det.get(AgentResultKind.expert, binding, dict(payload) | {"expert": expert})
                _require_proposal(fallback)
            except Exception:  # noqa: BLE001 没有确定性实现时只能受阻
                fallback = None
        if returned_at is not None and returned_at < requested_at:  # 墙钟回拨时不让契约校验失败
            returned_at = requested_at
        timing = AgentTiming(origin_event_at=origin_event_at, trigger_at=binding.trigger_at, effective_at=effective, requested_at=requested_at,
                             returned_at=returned_at, retries=retries,
                             latency_ms=int((returned_at - requested_at).total_seconds() * 1000) if returned_at else None)
        proposal = None
        if (result or fallback) is not None:
            proposal = ExpertProposal.model_validate((result or fallback)["proposal"])
            # 只给未绑定的草稿补编号、绑定与时序；结果源带回的已有绑定原样保留，交 stale_reason 判过期（旧运行、旧轮次只入历史）
            proposal = proposal.model_copy(update={"proposal_id": proposal.proposal_id or self.next_id("pr"),
                                                   "binding": proposal.binding or binding, "timing": proposal.timing or timing})
        # 实际执行的是确定性实现：与主 Agent、live 回退同一口径记 deterministic（#166、#243）
        offline_fallback = self.run_mode in ("trial", "evaluation") and source_kind == "deterministic"
        record = AgentRunRecord(agent_run_id=self.next_id("sim-ar"), kind=AgentResultKind.expert, agent_name=expert,
                                run_mode="deterministic" if offline_fallback else self.run_mode, source=source_kind, binding=binding, timing=timing, status=status,
                                coverage_gap=offline_fallback,
                                proposal_ids=(proposal.proposal_id,) if proposal else ())
        if error and fallback is not None:
            error += "；已用确定性提案照常进入执行"
        return AgentOutcome(proposal=proposal, record=record, error=error)

    @staticmethod
    def stale_reason(binding: RunBinding, *, run_id: str, round_: int, state_version: int, tolerance: int,
                     now: datetime, effective_at: datetime) -> str | None:
        if binding.run_id != run_id:
            return f"绑定的运行 {binding.run_id} 已不是当前运行 {run_id}（分叉后父运行的在途结果作废）"
        if binding.round != round_:
            return f"绑定的第 {binding.round} 轮已过期（当前第 {round_} 轮）"
        if state_version - binding.state_version > tolerance:
            return f"状态版本从 {binding.state_version} 变到 {state_version}，超出容差 {tolerance}"
        if effective_at < now:
            return f"生效时刻 {effective_at.isoformat()} 早于当前仿真时刻 {now.isoformat()}，所在时段已结算"
        return None
