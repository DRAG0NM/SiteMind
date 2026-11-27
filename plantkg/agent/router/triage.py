"""研判（B27）：先走 Jev 派域与门控，领域概率即本次检索与采纳权重；门控未放行按 D53 并入慢通道调查（kind=investigate）。

kind=triage 只剩结论合成（合批汇报），不在本模块直接调网关：
- live：B23 网关发起一次独立的 Nexent 运行，积压事项合批注入；
- trial/evaluation：取 B33 缓存，缺失时回退到本模块登记的确定性实现（register_mode(mode=deterministic, key=triage)），
  把待汇报结论拼成一句并记“覆盖不足”。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from plantkg.core.contracts import AgentResultKind, JudgeResult, PlantkgError, RunBinding
from plantkg.knowledge.judge import DOMAIN_DEFINITIONS, dispatch_domains

TRIAGE_KEY = "triage"  # register_mode 的键，与 bootstrap.MODE_REQUIREMENTS 一致

log = logging.getLogger(__name__)


def domains_of(event: Any) -> dict[str, str]:
    """派域候选：事件实体所属领域由图谱给（domain_of 注入），这里只放定义。"""
    return dict(DOMAIN_DEFINITIONS)


def judge_event(judge: Any, event: Any, *, definitions: Mapping[str, str] | None = None) -> JudgeResult | None:
    """Jev 派域：按事件逐条请求（不合批，合批会降一致率），返回完整领域概率与独立门控。"""
    content = f"{event.event_type}：{event.summary}"
    signals = {"at": event.at.isoformat(), "entity_id": event.entity_id, "severity": event.severity,
               "payload": event.payload}
    try:
        return dispatch_domains(judge, event.event_id, content, definitions=definitions or domains_of(event), signals=signals)
    except (PlantkgError, ValueError) as exc:  # 后端不可用或派域契约不完整时不伪造概率，交给确定性权重
        log.warning("判别后端 %s 派域失败，退化为确定性权重：%s",
                    getattr(judge, "backend", type(judge).__name__), exc)
        return None


def weights(result: JudgeResult | None, fallback: Mapping[str, float]) -> dict[str, float]:
    if result is None:
        return dict(fallback)
    return {k: round(float(v), 4) for k, v in result.distribution.items()}


def experts_from(weights_: Mapping[str, float], located: Sequence[str], *, threshold: float, limit: int) -> list[str]:
    """图谱定位的领域就是归属；图谱没给时才用 Jev 概率达到阈值的领域。两处都按 Jev 概率排序。"""
    picked = [d for d in located if d in DOMAIN_DEFINITIONS]
    if not picked:
        picked = [d for d, p in sorted(weights_.items(), key=lambda kv: (-kv[1], kv[0])) if p >= threshold]
    if not picked and weights_:
        picked = [max(weights_, key=lambda d: (weights_[d], d))]
    order = sorted(dict.fromkeys(picked), key=lambda d: (-weights_.get(d, 0.0), d))
    return order[:limit]


class DeterministicTriage:
    """AgentResultSource（deterministic，键 triage）：把待汇报结论拼成一句。

    payload：{"query", "as_of", "items": [{"kind": "report", "ticket_id", "summary"}]}
    返回：{"status", "agent_run_id", "final_answer"}
    """

    name = "triage"

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if AgentResultKind(kind) != AgentResultKind.triage:
            raise ValueError(f"确定性研判只实现 triage，不实现 {kind}")
        lines = [f"{item.get('ticket_id', '')}：{item.get('summary', '')}" for item in payload.get("items") or ()]
        return {"status": "finished", "agent_run_id": None, "final_answer": "；".join(lines)[:500] or "无待决事项"}


def compose_query(items: Sequence[Mapping[str, Any]]) -> str:
    """一次运行注入全部积压事项；Jev 仍按事件逐条请求，只有结论合成合批。"""
    lines = [f"- {i.get('kind')} {i.get('ticket_id')}：{i.get('summary', '')}" for i in items]
    return "值守层待汇报结论：\n" + "\n".join(lines)


# 调查输出契约：写进每次调查的 query；主 Agent 提示词与 triage Skill 同步说明
INVESTIGATION_FORMAT = (
    "只输出一个 JSON 对象：{\"diagnosis\": 根因与依据（一到三句中文）, \"experts\": [要问的专家，取自 energy、elevator、hvac、equipment], "
    "\"evidence_refs\": [图谱路径、读数或事件单 ID], \"probe\": null 或 {\"actions\": [{\"entity_id\": 对象, \"command\": 命令, \"params\": {}}], "
    "\"window_min\": 观察分钟, \"hypothesis\": 待检验假设, \"expectations\": [各假设下的预期读数反应]}}。"
    "给 probe 时 experts 可为空数组。"
)
QUERY_CONTEXT_CHARS = 6000  # Gateway 的 query 上限 8000 字符，留出事件描述与输出契约


def _last_object(text: str) -> dict[str, Any]:
    """取回答里最后一个带 diagnosis 的 JSON 对象：容忍代码块围栏、前后说明、模型重复作答，结论后附带的小对象不顶替结论。"""
    decoder, found, at = json.JSONDecoder(), None, text.find("{")
    while at >= 0:
        try:
            item, end = decoder.raw_decode(text, at)
        except json.JSONDecodeError:
            end = at + 1
        else:
            if found is None or "diagnosis" in item:
                found = item
        at = text.find("{", end)
    if found is None:
        raise ValueError("调查回答里没有 JSON 对象")
    return found


def parse_investigation(result: Mapping[str, Any]) -> dict[str, Any]:
    """把调查来源的返回统一成 {diagnosis, experts, evidence_refs, probe}；不符合契约抛 ValueError，由调用方降级。

    live 来源只给 {status, agent_run_id, terminal_reason, final_answer}，字段在 final_answer 的 JSON 里；
    确定性与缓存实现没有 final_answer，直接就是解析后的格式。
    """
    if "final_answer" not in result:
        return dict(result)
    if result.get("status") != "finished":
        raise ValueError(f"调查运行未完成：{result.get('status')}（{result.get('terminal_reason')}）")
    text = str(result.get("final_answer") or "")
    answer = _last_object(text)
    diagnosis, experts, refs, probe = (answer.get(k) for k in ("diagnosis", "experts", "evidence_refs", "probe"))
    if not (isinstance(diagnosis, str) and diagnosis.strip() and isinstance(experts, list) and isinstance(refs, list)
            and (probe is None or isinstance(probe, dict)) and (experts or probe)):
        raise ValueError("调查回答不符合输出契约")
    return dict(result) | {"diagnosis": diagnosis, "experts": [str(e) for e in experts], "evidence_refs": refs, "probe": probe}


def context_text(context: Mapping[str, Any]) -> str:
    """本轮注入内容序列化进 query；放不下时先丢最早的读数与事件，最新读数和试探结论留下。"""
    context = dict(context)
    while True:
        text = json.dumps(context, ensure_ascii=False, default=str)
        longest = max(("readings_before", "readings_after", "events_during_probe"), key=lambda k: len(context[k]))
        if len(text) <= QUERY_CONTEXT_CHARS or not context[longest]:
            return text
        context[longest] = context[longest][1:]
        context["truncated"] = True


class DeterministicInvestigation:
    """AgentResultSource（deterministic，键 investigate）：慢通道的确定性替身。

    沿 find_sources 取首个候选来源（sources 由装配注入），按 Jev 最高领域指定专家；返回 {status, diagnosis, experts, evidence_refs,
    coverage_gap}，与 live 回答解析后的格式一致。
    """

    def __init__(self, sources: Callable[[Mapping[str, Any]], Sequence[str]] | None = None) -> None:
        self.sources = sources

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if AgentResultKind(kind) != AgentResultKind.investigate:
            raise ValueError(f"确定性调查只实现 investigate，不实现 {kind}")
        event = dict(payload.get("event") or {})
        domains = dict(payload.get("domains") or {})
        located = [str(d) for d in (payload.get("located") or ()) if str(d) in domains]
        candidates = located or list(domains)
        top = max(candidates, key=lambda d: (domains.get(d, 0.0), d)) if candidates else "equipment"
        entity = str(event.get("entity_id") or payload.get("ticket_id") or "")
        candidates_src = list(self.sources(payload)) if self.sources else ([entity] if entity else [])
        source = candidates_src[0] if candidates_src else entity
        diagnosis = f"沿图谱定位：{source or '未知对象'}，归属 {top} 领域；确定性实现按图谱定位与 Jev 派域指定专家"
        return {"status": "finished", "diagnosis": diagnosis, "experts": [top],
                "evidence_refs": [r for r in (source,) if r], "coverage_gap": True, "agent_run_id": None}
