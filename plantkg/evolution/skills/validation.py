"""B43 Skill 候选评估：样本工单重放命中率、硬约束不退化、步骤不含安全禁区命令。

判定只看候选的 skill 体与 B32 回测报告，不调用模型，可重复。样本重放用生成候选时记下的
evidence（工单动作），每条工单的动作都能在 steps 的 (entity_type, command) 里找到才算命中。
B32 既有 investigation 体没有 evidence、步骤是纯文本：跳过样本重放，只做适用的安全校验
（命令词按 B28 词表归一 + 文本关键词）并要求回测通过，兼容既有 Skill 候选。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from plantkg.core.contracts import BacktestReport, Candidate, ManifestComponent, PlantkgError
from plantkg.evolution.skills import miner
from plantkg.evolution.skills.miner import EXECUTION_RECORDS_HOOK

HIT_RATE_THRESHOLD = 0.8
# B28 第 1 层安全否决 / B47 L0 口径的禁区命令（configured）：Skill 步骤永远不能包含这些动作
FORBIDDEN_COMMANDS = frozenset({"stop", "power_off", "curtail"})
FORBIDDEN_COMMANDS_HOOK = "b43.skill_forbidden_commands"
# 实际命令 → 概念动作与切断类动作集合由装配层经钩子提供（B43 不 import 别的平面）
VOCABULARY_HOOK = "b43.action_vocabulary"
# B28 的 CUT_SUPPLY 把“检修”（repair/rescue 归一后的 maintenance）也算切断类；检修是设备作业，不等于切断供给，
# 发布门单独放行：含 stop/power_off/shed/curtail 等切断动作的步骤仍然拒绝
MAINTENANCE_CONCEPTS = frozenset({"maintenance"})
# 步骤文本里出现这些词也按禁区处理（步骤会渲染进 SKILL.md，人来读的执行指引同样不能出现）；
# 含 B28 概念动作的中文近义说法，避免只换一种写法就绕过（#242 第 5 轮复审 P2）
TEXT_FORBIDDEN_KEYWORDS = ("停机", "断电", "切断", "关闭", "停止", "停运", "拉闸", "断供")
# investigation 的步骤是纯文本，按其中的命令词（英文标识符）做 B28 词表归一判定
COMMAND_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class SkillValidationPolicy:
    """B43 评估口径（version=b43-v1）：实现 ValidationPolicy 端口，evaluate 给出判定明细。"""

    version = "b43-v1"

    def __init__(self, artifacts: Any, *, registry: Any = None,
                 hit_rate_threshold: float = HIT_RATE_THRESHOLD,
                 forbidden_commands: Iterable[str] | None = None) -> None:
        self.artifacts = artifacts
        self.registry = registry
        self.hit_rate_threshold = float(hit_rate_threshold)
        # 常量与显式参数在构造时确定；装配层钩子（B28 词表、追加禁区命令）在 evaluate 时再读，
        # 因为注册表在领域包加载完之后才由装配层补钩子
        commands = FORBIDDEN_COMMANDS if forbidden_commands is None else forbidden_commands
        self.forbidden = frozenset(_normalize_command(command) for command in commands)

    def evaluate(self, candidate: Candidate, report: BacktestReport) -> dict[str, Any]:
        """按“skill 体可读 → 禁区命令 → 格式分流（重放/安全校验）→ 硬约束 → 回测结论”判定。

        B43 procedure 体有样本工单，走完整评估；B32 investigation 体没有 evidence、步骤是
        纯文本，跳过样本重放，只做适用的安全校验并要求回测报告通过。
        """
        body, error = self._load_body(candidate)
        if error is not None:
            return self._result("rejected", (error,), 0.0)
        skill_format = _skill_format(body)
        forbidden, aliases, cut_supply = self._rules()
        hits = _forbidden_hits(body, forbidden, aliases, cut_supply)
        if hits:
            return self._result("rejected", (f"步骤含安全禁区命令：{'、'.join(hits)}",), 0.0, skill_format)
        if skill_format == "investigation":
            return self._investigation_result(report, skill_format)
        hit_rate, replay_error = self._replay(body, _step_commands(body))
        if replay_error is not None:
            return self._result("rejected", (replay_error,), hit_rate, skill_format)
        forged = self._forged_evidence(body)
        if forged:
            return self._result("rejected", (f"evidence 与 B47 真实执行记录不符：{'、'.join(forged)}",), hit_rate, skill_format)
        base = int(report.constraint_violations.get("base", 0) or 0)
        current = int(report.constraint_violations.get("candidate", 0) or 0)
        if current > base:
            return self._result("rejected", (f"硬约束退化：违例 {base} → {current}",), hit_rate, skill_format)
        if report.verdict != "pass":
            detail = "；".join(report.failures)
            reason = f"回测结论 {report.verdict}" + (f"：{detail}" if detail else "")
            return self._result("rejected", (reason,), hit_rate, skill_format)
        return self._result("validated", (f"通过：样本重放命中率 {hit_rate:.0%} "
                                          f"不低于阈值 {self.hit_rate_threshold:.0%}",), hit_rate, skill_format)

    def _investigation_result(self, report: BacktestReport, skill_format: str) -> dict[str, Any]:
        """B32 investigation 体：没有样本工单与 procedure 步骤命令，跳过重放，只要求回测通过。

        硬约束违例不据此改判（没有重放无法归因），但 base/candidate 变化要写进 reasons 和
        skill_evaluated，避免评估结论看起来比实际更强。
        """
        base = int(report.constraint_violations.get("base", 0) or 0)
        current = int(report.constraint_violations.get("candidate", 0) or 0)
        violations = {"base": base, "candidate": current}
        change = f"硬约束违例 {base} → {current}（investigation 体未重放，未据此改判）"
        if report.verdict != "pass":
            detail = "；".join(report.failures)
            reason = f"回测结论 {report.verdict}" + (f"：{detail}" if detail else "")
            return self._result("rejected", (reason, change), None, skill_format, violations)
        return self._result("validated", ("通过：B32 investigation 体无样本工单，已跳过重放；"
                                          "步骤未含安全禁区", change), None, skill_format, violations)

    def judge(self, candidate: Candidate, report: BacktestReport) -> Literal["validated", "rejected"]:
        """ValidationPolicy 端口只需要判定结论（明细用 evaluate）。"""
        return self.evaluate(candidate, report)["verdict"]

    def _rules(self) -> tuple[frozenset[str], dict[str, tuple[str, ...]], frozenset[str]]:
        """生效的禁区集合：常量/显式参数 + 装配层钩子追加命令 + B28 词表归一。"""
        forbidden = set(self.forbidden)
        if self.registry is not None:
            for hook in self.registry.hooks_for(FORBIDDEN_COMMANDS_HOOK):
                forbidden.update(_normalize_command(command) for command in _as_commands(hook()))
        aliases, cut_supply = _vocabulary(self.registry)
        return frozenset(forbidden), aliases, cut_supply

    def _load_body(self, candidate: Candidate) -> tuple[dict[str, Any] | None, str | None]:
        ref = candidate.changes.get(ManifestComponent.skills)
        if ref is None:
            return None, "候选没有 skills 组件"
        try:
            return self.artifacts.load(ref), None
        except PlantkgError as exc:
            return None, f"skill 体读取失败：{exc.message}"

    def _replay(self, body: Mapping[str, Any], commands: set[tuple[str, str]]) -> tuple[float, str | None]:
        """样本工单重放：命中工单数 / 样本数；没有样本或命中率低于阈值都算不通过。"""
        evidence = [item for item in body.get("evidence") or () if isinstance(item, Mapping)]
        if not evidence:
            return 0.0, "没有样本工单，无法重放验证"
        hits = 0
        for ticket in evidence:
            actions = [(str(action[0]), str(action[1])) for action in ticket.get("actions") or ()
                       if isinstance(action, (list, tuple)) and len(action) >= 2]
            if actions and all(action in commands for action in actions):
                hits += 1
        rate = hits / len(evidence)
        if rate < self.hit_rate_threshold:
            return rate, (f"样本工单重放命中率 {rate:.0%} 低于阈值 {self.hit_rate_threshold:.0%}"
                          f"（{hits}/{len(evidence)}）")
        return rate, None

    def _forged_evidence(self, body: Mapping[str, Any]) -> list[str]:
        """evidence 的工单与动作须能在 B47 真实执行记录里找到；伪造的 evidence 不能 validated。

        只在登记了 b47.execution_records 钩子、且 skill 体不是合成数据（nature=synthetic 的演示 fixtures）时核对。"""
        hooks = self.registry.hooks_for(EXECUTION_RECORDS_HOOK) if self.registry is not None else ()
        if not hooks or str(body.get("nature") or "") == "synthetic":
            return []
        try:
            records = list(hooks[-1](run_id=None) or ())
        except Exception:  # noqa: BLE001 — 记录读取故障按“无法核对”处理，不据此拒绝
            return []
        actual = miner.ticket_signatures([miner._record_dict(record) for record in records])  # 与挖掘同一归一/过滤/排序
        forged: list[str] = []
        for item in body.get("evidence") or ():
            if not isinstance(item, Mapping):
                continue
            ticket_id = str(item.get("ticket_id") or "")
            claimed = tuple((str(action[0]), str(action[1])) for action in item.get("actions") or ()
                            if isinstance(action, (list, tuple)) and len(action) >= 2)
            if actual.get(ticket_id) != claimed:
                forged.append(ticket_id or "(无工单号)")
        return forged

    def _result(self, verdict: Literal["validated", "rejected"], reasons: Iterable[str],
                hit_rate: float | None, skill_format: str = "procedure",
                constraint_violations: Mapping[str, int] | None = None) -> dict[str, Any]:
        result = {"verdict": verdict, "reasons": tuple(reasons), "hit_rate": hit_rate,
                  "forbidden": tuple(sorted(self.forbidden)), "format": skill_format}
        if constraint_violations is not None:
            result["constraint_violations"] = dict(constraint_violations)
        return result


def evaluate_skill_candidate(service: Any, policy: SkillValidationPolicy, candidate_id: str) -> dict[str, Any]:
    """快回测 → 策略判定 → 候选状态落为 validated/rejected，并写一条 skill_evaluated 时间线。"""
    report = service.fast_backtest(candidate_id)
    candidate = service.store.candidate(candidate_id)
    result = policy.evaluate(candidate, report)
    updated = candidate.model_copy(update={"status": result["verdict"]})
    service.store.update_candidate(updated)
    record_evaluation(service.store, candidate_id=candidate_id, report=report, result=result,
                      run_id=service.context.run_id or service.context.trace_id)
    return {"candidate": updated, "report": report, **result}


def record_evaluation(store: Any, *, candidate_id: str, report: BacktestReport,
                      result: Mapping[str, Any], run_id: str | None) -> None:
    """写一条 skill_evaluated 时间线；单独评估入口与发布门共用同一事件格式。"""
    event = {"event": "skill_evaluated", "candidate_id": candidate_id,
             "verdict": result["verdict"], "reasons": list(result["reasons"]),
             "hit_rate": result["hit_rate"], "format": result.get("format", "procedure"),
             "report_id": report.report_id,
             "run_id": run_id, "at": datetime.now(timezone.utc).isoformat()}
    if result.get("constraint_violations") is not None:
        event["constraint_violations"] = dict(result["constraint_violations"])
    store.event(event)


def _skill_format(body: Mapping[str, Any]) -> str:
    """skill 体格式：B43 是 procedure，B32 既有产物是 investigation；未知格式按完整评估（失败关闭）。"""
    kind = str(body.get("kind") or "procedure").strip().lower()
    return "investigation" if kind == "investigation" else "procedure"


def _as_commands(value: Any) -> list[Any]:
    """钩子返回值归一成命令列表：单个字符串/字典算一条（不能按字符迭代），其他可迭代按元素，不可迭代的忽略。"""
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value.decode() if isinstance(value, bytes) else value]
    try:
        return list(value)
    except TypeError:
        return []


def _normalize_command(value: Any) -> str:
    return str(value).strip().lower() if not isinstance(value, dict) else str(value.get("name") or value).strip().lower()


def _vocabulary(registry: Any) -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    """装配层经钩子给的 B28 词表：实际命令 → 概念动作，以及会切断供给的概念动作集合。"""
    aliases: dict[str, tuple[str, ...]] = {}
    cut_supply: set[str] = set()
    if registry is None:
        return aliases, frozenset()
    for hook in registry.hooks_for(VOCABULARY_HOOK):
        try:
            data = hook() or {}
        except Exception:  # noqa: BLE001 — 词表缺失时退回常量禁区，不挡评估
            continue
        if not isinstance(data, Mapping):
            continue
        for command, concepts in (data.get("aliases") or {}).items():
            aliases[_normalize_command(command)] = tuple(_normalize_command(concept) for concept in concepts or ())
        cut_supply.update(_normalize_command(item) for item in data.get("cut_supply") or ())
    return aliases, frozenset(cut_supply - MAINTENANCE_CONCEPTS)


def _step_commands(body: Mapping[str, Any]) -> set[tuple[str, str]]:
    """steps 的 (entity_type, command) 集合；缺 command 的旧 investigation 步骤不计入。"""
    commands: set[tuple[str, str]] = set()
    for step in body.get("steps") or ():
        if isinstance(step, Mapping) and step.get("command"):
            commands.add((str(step.get("entity_type") or ""), str(step["command"])))
    return commands


def _forbidden_hits(body: Mapping[str, Any], forbidden: frozenset[str],
                    aliases: Mapping[str, tuple[str, ...]] | None = None,
                    cut_supply: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """步骤命令按 B28 词表归一后命中禁区/切断类，或步骤文本出现停机/断电/切断，都算命中。

    B43 procedure 的步骤是对象（command + text）；B32 investigation 的步骤是纯文本，没有
    command 字段，按文本里的命令词归一后判定，再查文本关键词。
    """
    hits: list[str] = []
    for step in body.get("steps") or ():
        if isinstance(step, Mapping):
            command = step.get("command")
            if command is not None:
                hits.extend(_command_hits(_normalize_command(command), forbidden, aliases, cut_supply))
            text = str(step.get("text") or "")
        else:
            text = str(step)
            hits.extend(_command_hits(text, forbidden, aliases, cut_supply))
        hits.extend(f"text:{keyword}" for keyword in TEXT_FORBIDDEN_KEYWORDS if keyword in text)
    return tuple(dict.fromkeys(hits))


def _command_hits(value: str, forbidden: frozenset[str],
                  aliases: Mapping[str, tuple[str, ...]] | None,
                  cut_supply: frozenset[str]) -> list[str]:
    """命令（或文本里的命令词）归一后命中禁区/切断类动作集合。"""
    hits: list[str] = []
    for token in COMMAND_TOKEN.findall(value):
        key = _normalize_command(token)
        concepts = aliases.get(key, (key,)) if aliases else (key,)
        if key in forbidden or any(concept in forbidden or concept in cut_supply for concept in concepts):
            hits.append(key)
    return hits
