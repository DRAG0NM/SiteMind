"""plantkg 公共数据契约（B01 冻结）。

所有平面只读写这里定义的记录（03 §1.1 规则 2）。字段名与语义依据 development/04「关键记录与数据契约」、
03 §2～§7 与 09 D17～D31。改动走契约前置变更：先改本文件，再运行 `python contracts/build.py`
重新生成 contracts/ 下的 JSON Schema、OpenAPI 与 MCP 清单，最后跑 tests/contracts。

时间与单位约定（ADR-0003）：
- 内部时间一律 UTC（带时区且偏移为 0），原时区另存；区间左闭右开 [start, end)。
- 功率 kW、能量 kWh、SOC 为 0～1 小数；空值（None）与 0 不同，不能互相替代。
- 可见性 `available_at <= as_of` 与适用性 `effective_from <= 时段开始 < effective_to` 分开判断。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ValidationError,
    ConfigDict,
    Field,
    WithJsonSchema,
    model_validator,
)

CONTRACT_VERSION = "1.2.0"  # 1.2.0：v0.8 契约前置（B46），只加字段与记录；1.1.0：HealthResponse 依赖状态增加 unchecked、placeholder、fake（B02 审查）
TOOL_RESULT_SCHEMA_VERSION = "1"
API_PREFIX = "/api/v1"
TOOL_PREFIX = "plantkg_"

# 平台实测约束（07 Q03～Q05，09 D21/D22）
TOOL_TEXT_MAX_BYTES = 8 * 1024
TOOL_DEADLINE_S = 40
JOB_WAIT_MAX_S = 40
ERROR_PREFIX = "PLANTKG_E_"
NOTICE_NO_CTX_PREFIX = "PLANTKG_NOTICE_NO_CTX:"
# 网关经 tool_params 注入的参数：工具结果、证据事件与日志都不得回显
INJECTED_PARAM_NAMES = frozenset({"ctx", "document_paths"})

# 账本容差（07 Q16，09 D30 已定）；B08/B09 共用，冻结测试前不得改
LEDGER_TOLERANCE = {
    "policy_version": "ledger-tol-1",
    "energy_abs_kwh": 1e-3,
    "energy_rel": 1e-6,
    "soc_abs": 1e-6,
    "power_zero_kw": 1e-6,
}


# ---------------------------------------------------------------- 基础类型


_UTC_TEXT = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]00:?00)$"
_UTC_ERROR = "时间必须带时区且为 UTC（偏移 0）；原时区放 source_tz"


def _utc_input(value: Any) -> Any:
    """与 JSON Schema 同口径：只收 datetime 或 ISO-8601 UTC 文本，不收数字时间戳、小写 z。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and re.fullmatch(_UTC_TEXT, value):
        return value
    raise ValueError(_UTC_ERROR)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(_UTC_ERROR)
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[
    datetime,
    BeforeValidator(_utc_input),
    AfterValidator(_require_utc),
    WithJsonSchema({"type": "string", "format": "date-time", "pattern": _UTC_TEXT}),
]
LocalDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]

ContentHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$", description="完整 sha256，去重与完整性校验用")]
AssetVersionId = Annotated[
    str,
    Field(
        pattern=r"^asset://[a-z0-9_-]+/[0-9a-f]{8}/[a-z0-9_-]+/.+$",
        description="asset://{行业}/{来源哈希前8位}/{类型}/{定位}；内容变即新版本",
    ),
]
EntityId = Annotated[
    str,
    Field(
        pattern=r"^entity://[A-Za-z0-9_-]+/[a-z0-9_-]+/[A-Za-z0-9_.-]+$",
        description="entity://{厂区}/{类型}/{编号}；不随资料修改而变",
    ),
]
Ctx = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", description="128 位随机数的十六进制；只作索引")]
ErrorCodeStr = Annotated[str, Field(pattern=r"^PLANTKG_E_[A-Z_]+$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class Record(BaseModel):
    """所有契约记录的基类：拒绝未知字段、NaN/inf；构造后字段不可重新赋值（嵌套 dict 为浅冻结）。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=True,
        allow_inf_nan=False,
        json_schema_serialization_defaults_required=True,
    )


class Interval(Record):
    """左闭右开 UTC 区间 [start, end)。"""

    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Interval:
        if self.end <= self.start:
            raise ValueError("区间 end 必须晚于 start（左闭右开）")
        return self

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


# ---------------------------------------------------------------- 枚举


class DataNature(str, Enum):
    measured = "measured"
    derived = "derived"
    configured = "configured"
    synthetic = "synthetic"
    simulated = "simulated"


class Unit(str, Enum):
    """内部标准单位。新增单位是契约变更。"""

    kW = "kW"
    kWh = "kWh"
    fraction = "1"  # SOC、比例、系数
    CNY = "CNY"
    CNY_per_kWh = "CNY/kWh"
    CNY_per_kW = "CNY/kW"
    hour = "h"
    minute = "min"
    degC = "degC"
    percent = "%"
    count = "count"
    bar_per_hour = "bar/h"  # B30：空压压力变化速率
    second = "s"  # B30：电梯候梯时间


class ReviewStatus(str, Enum):
    pending = "pending"
    auto_accepted = "auto_accepted"  # 门控自动接纳；只适用于不进规划器的本体条目
    approved = "approved"
    rejected = "rejected"
    trusted_config = "trusted_config"  # 受信配置（B12 骨架的已审核 YAML 等）


# 影响费用与工艺的规则只接受这两种状态（03 §4.4）
COMPILABLE_REVIEW_STATUSES = frozenset({ReviewStatus.approved.value, ReviewStatus.trusted_config.value})


class Role(str, Enum):
    viewer = "viewer"
    operator = "operator"
    reviewer = "reviewer"  # 团队两人均为复核员（07 Q15）


class ContextChannel(str, Enum):
    gateway = "gateway"  # 工作台经网关发起，带 ctx
    none = "none"  # Nexent 界面直接发起，无 ctx：只读降级
    rest = "rest"  # 工作台只读 REST 与统一确认端点
    offline = "offline"  # loop.py 等离线入口
    mock_default = "mock_default"  # mock/dev 默认 ctx，正式运行关闭


class PendingActionKind(str, Enum):
    review_decision = "review_decision"
    plan_confirmation = "plan_confirmation"
    publish = "publish"
    rollback = "rollback"
    work_order = "work_order"  # v0.8：安全类动作的操作工单（B47）
    correction = "correction"  # 低置信转人工纠错（B47/B32）
    sampling = "sampling"  # 中等置信度抽查（B47）
    human_decision = "human_decision"  # needs_human 动作的人工待办（B47）
    review = "review"  # L1 先做后审的复核项（B47）：确认=复核通过，拒绝=复核不通过并回滚降级
    question = "question"  # 主 Agent 向值班人员提一个问题，确认=回答


class RunMode(str, Enum):
    """v0.8 运行模式（03 §3.0 运行模式）。deterministic 是 live 来源缺失时的记录口径，不是请求模式。"""

    live = "live"  # 实时：经网关实调 Nexent
    trial = "trial"  # 隔离试算与预演
    evaluation = "evaluation"  # 评测与回测
    deterministic = "deterministic"  # 本应 live 但只取到确定性实现：如实记来源，不冒充 live


class AgentResultKind(str, Enum):
    """AgentResultSource 的结果类型：专家运行、门控不一致研判、第 3 层裁决、追问。"""

    expert = "expert"
    triage = "triage"
    arbitration = "arbitration"
    follow_up = "follow_up"
    investigate = "investigate"  # v0.8 慢通道：主 Agent 用 L1 工具调查后给方案（D53）


class ActionReadiness(str, Enum):
    ready = "ready"  # 不依赖根因，第 1、2 层可先执行
    needs_diagnosis = "needs_diagnosis"  # 处置对象取决于根因结论，depends_on 写明依赖的诊断项
    needs_human = "needs_human"  # 要人回答或确认


class LoadCategory(str, Enum):
    """负荷分项（v0.8 账本 load_breakdown）。"""

    elevator = "elevator"
    hvac = "hvac"
    air_compressor = "air_compressor"
    lighting = "lighting"
    process = "process"
    base = "base"


class PendingActionStatus(str, Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    rejected = "rejected"
    expired = "expired"


PENDING_ACTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"confirmed", "rejected", "expired"}),
    "confirmed": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
}


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed", "cancelled"}),  # 启动前校验失败可直接 failed
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class JobKind(str, Enum):
    run_day = "run_day"
    run_week = "run_week"
    backtest = "backtest"
    counterfactual_score = "counterfactual_score"
    knowledge_eval = "knowledge_eval"
    ingest = "ingest"
    onboarding_investigate = "onboarding_investigate"


class ManifestStatus(str, Enum):
    """版本清单生命周期（07 Q15 由 B01 写定，见 ADR-0005）。"""

    prepared = "prepared"  # 产物已写入，未校验
    verified = "verified"  # 完整性检查通过，可激活
    active = "active"  # 当前唯一活动清单
    retired = "retired"  # 曾经激活、已被替换；回滚只能回到这里
    discarded = "discarded"  # 校验失败或放弃；从未激活


MANIFEST_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"verified", "discarded"}),
    "verified": frozenset({"active", "discarded"}),
    "active": frozenset({"retired"}),
    "retired": frozenset({"active"}),  # 回滚 = 重新激活曾经激活过的清单
    "discarded": frozenset(),
}


class ManifestComponent(str, Enum):
    ontology = "ontology"
    rules = "rules"
    parameters = "parameters"
    data_sources = "data_sources"
    compilers = "compilers"
    models = "models"
    skills = "skills"
    policy = "policy"  # v0.8：裁决权重、优先级、置信度阈值、抽查比例与响应预算（PolicyConfig）
    sensor_cases = "sensor_cases"  # v0.11（B32 D59）：传感器/设备案例累积列表，与基准 parameters 分开


class CandidateKind(str, Enum):
    parameter = "parameter"  # 决策进化，骨架为光伏预测修正系数 k（07 Q26）
    knowledge = "knowledge"  # 规则或本体新版本
    skill = "skill"  # B42 可选


class CandidateStatus(str, Enum):
    proposed = "proposed"
    validated = "validated"
    rejected = "rejected"
    published = "published"
    rolled_back = "rolled_back"


CANDIDATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"validated", "rejected"}),
    "validated": frozenset({"published", "rejected"}),
    "published": frozenset({"rolled_back"}),
    "rejected": frozenset(),
    "rolled_back": frozenset(),
}


class SolverStatus(str, Enum):
    optimal = "optimal"  # 达到配置的 mip_rel_gap；不表示已证明全局最优
    feasible = "feasible"  # 触及预算，有通过校验的可行解，附实际 gap
    timeout = "timeout"  # 触及预算且无可行解，不返回计划
    infeasible = "infeasible"  # 无解，附冲突线索，不放宽硬约束
    error = "error"  # 其他异常，含无界与独立校验未通过


SOLVER_STATUSES_WITH_PLAN = frozenset({"optimal", "feasible"})


class PlanStatus(str, Enum):
    draft = "draft"
    review_required = "review_required"
    auto_confirmed = "auto_confirmed"
    confirmed = "confirmed"
    rejected = "rejected"
    executed = "executed"
    superseded = "superseded"


PLAN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"auto_confirmed", "review_required", "rejected", "superseded"}),
    "review_required": frozenset({"confirmed", "rejected", "superseded"}),
    "auto_confirmed": frozenset({"executed", "superseded"}),
    "confirmed": frozenset({"executed", "superseded"}),
    "rejected": frozenset(),
    "executed": frozenset(),
    "superseded": frozenset(),
}


class GateOutcome(str, Enum):
    auto_confirmed = "auto_confirmed"
    review_required = "review_required"
    rejected = "rejected"


class OntologyLayer(str, Enum):
    concept = "concept"
    relation = "relation"
    constraint = "constraint"
    instance = "instance"


class JudgeBackend(str, Enum):
    jev = "jev"  # 默认：百炼 decision-model-preview
    fallback = "fallback"  # 团队保底后端
    fake = "fake"  # 骨架与冒烟测试


class RecallSource(str, Enum):
    vector = "vector"
    rule = "rule"
    manual = "manual"
    fake = "fake"


class PendingIssueReason(str, Enum):
    unit_unknown = "unit_unknown"
    subject_unknown = "subject_unknown"
    conflict = "conflict"
    missing_price = "missing_price"
    negative_price = "negative_price"
    unsupported_kind = "unsupported_kind"
    not_reviewed = "not_reviewed"
    not_visible = "not_visible"
    out_of_effect = "out_of_effect"
    config_invalid = "config_invalid"


class EvidenceEventType(str, Enum):
    tool_called = "tool_called"
    tool_result = "tool_result"
    retrieval = "retrieval"
    graph_query = "graph_query"
    rule_lookup = "rule_lookup"
    rule_applied = "rule_applied"
    conflict_detected = "conflict_detected"
    path_switch = "path_switch"  # 图谱缺边转原文检索等（03 §4.5）
    plan_created = "plan_created"
    gate_decided = "gate_decided"
    pending_action_proposed = "pending_action_proposed"
    action_decided = "action_decided"
    job_submitted = "job_submitted"
    job_finished = "job_finished"
    execution_recorded = "execution_recorded"
    version_activated = "version_activated"
    candidate_status_changed = "candidate_status_changed"
    # #245 人工干预：暂停/恢复自动执行、取消未生效动作、撤销已生效动作（只追加）
    autonomy_paused = "autonomy_paused"
    autonomy_resumed = "autonomy_resumed"
    action_cancelled = "action_cancelled"
    action_revoked = "action_revoked"


class EvidencePath(str, Enum):
    retrieval = "retrieval"  # Nexent 知识库原文检索
    reasoning = "reasoning"  # 图谱、规则与规划器
    none = "none"


class ResultStatus(str, Enum):
    ok = "ok"
    pending = "pending"
    degraded = "degraded"
    failed = "failed"
    conflict = "conflict"


class BusinessStatus(str, Enum):
    """ToolResult.business_status；不靠 HTTP 200 判成功。"""

    done = "done"
    pending = "pending"  # 40 秒内未完成，带 job_id，用同一 job_id 调 get_job
    degraded = "degraded"  # 无 ctx 的只读降级结果
    failed = "failed"


class UIEventType(str, Enum):
    run_started = "run_started"
    tool_started = "tool_started"
    tool_finished = "tool_finished"
    evidence_added = "evidence_added"
    job_progress = "job_progress"
    review_required = "review_required"
    answer_delta = "answer_delta"
    plan_ready = "plan_ready"
    run_finished = "run_finished"
    run_failed = "run_failed"
    run_cancelled = "run_cancelled"
    keepalive = "keepalive"


TERMINAL_UI_EVENTS = frozenset({"run_finished", "run_failed", "run_cancelled"})


class TerminalReason(str, Enum):
    """网关判定的终态原因（07 Q03 四种异常 + 正常结束）。"""

    completed = "completed"
    completed_from_deltas = "completed_from_deltas"  # complete 内容为空，拼接 delta
    timeout = "timeout"  # 只有一条超时文本的 final_answer
    cancelled = "cancelled"  # 中止：连接关闭、没有结束事件
    stream_lost_recovered = "stream_lost_recovered"  # 断流后查会话历史补结局
    stream_lost = "stream_lost"  # 断流且补不回
    error = "error"


class ErrorCode(str, Enum):
    NO_CTX = "PLANTKG_E_NO_CTX"
    UNKNOWN_CTX = "PLANTKG_E_UNKNOWN_CTX"
    CTX_EXPIRED = "PLANTKG_E_CTX_EXPIRED"
    FORBIDDEN = "PLANTKG_E_FORBIDDEN"
    CROSS_PLANT = "PLANTKG_E_CROSS_PLANT"
    NOT_FOUND = "PLANTKG_E_NOT_FOUND"
    NOT_VISIBLE = "PLANTKG_E_NOT_VISIBLE"  # 存在但对当前 as_of / 清单不可见
    INVALID_REQUEST = "PLANTKG_E_INVALID_REQUEST"
    DOMAIN_INVALID = "PLANTKG_E_DOMAIN_INVALID"
    PENDING_ISSUES = "PLANTKG_E_PENDING_ISSUES"  # 规则单位/对象不明、缺价等，拒绝规划
    PRECONDITION = "PLANTKG_E_PRECONDITION"  # 工具前置检查未过（如无已复核规则版本）
    REVISION_CONFLICT = "PLANTKG_E_REVISION_CONFLICT"
    STATE_CONFLICT = "PLANTKG_E_STATE_CONFLICT"
    IDEMPOTENCY_MISMATCH = "PLANTKG_E_IDEMPOTENCY_MISMATCH"
    ACTION_EXPIRED = "PLANTKG_E_ACTION_EXPIRED"
    INFEASIBLE = "PLANTKG_E_INFEASIBLE"
    SOLVER_TIMEOUT = "PLANTKG_E_SOLVER_TIMEOUT"
    SOLVER_ERROR = "PLANTKG_E_SOLVER_ERROR"
    RESULT_TOO_LARGE = "PLANTKG_E_RESULT_TOO_LARGE"
    DEPENDENCY_UNAVAILABLE = "PLANTKG_E_DEPENDENCY_UNAVAILABLE"
    INTERNAL = "PLANTKG_E_INTERNAL"


# REST 状态码映射（ADR-0006）；MCP 侧一律以文本前缀返回错误码
ERROR_HTTP_STATUS: dict[str, int] = {
    "PLANTKG_E_NO_CTX": 401,
    "PLANTKG_E_UNKNOWN_CTX": 401,
    "PLANTKG_E_CTX_EXPIRED": 401,
    "PLANTKG_E_FORBIDDEN": 403,
    "PLANTKG_E_CROSS_PLANT": 403,
    "PLANTKG_E_NOT_FOUND": 404,
    "PLANTKG_E_NOT_VISIBLE": 404,
    "PLANTKG_E_INVALID_REQUEST": 400,
    "PLANTKG_E_DOMAIN_INVALID": 422,
    "PLANTKG_E_PENDING_ISSUES": 422,
    "PLANTKG_E_PRECONDITION": 422,
    "PLANTKG_E_REVISION_CONFLICT": 409,
    "PLANTKG_E_STATE_CONFLICT": 409,
    "PLANTKG_E_IDEMPOTENCY_MISMATCH": 409,
    "PLANTKG_E_ACTION_EXPIRED": 409,
    "PLANTKG_E_INFEASIBLE": 422,
    "PLANTKG_E_SOLVER_TIMEOUT": 503,
    "PLANTKG_E_SOLVER_ERROR": 500,
    "PLANTKG_E_RESULT_TOO_LARGE": 500,
    "PLANTKG_E_DEPENDENCY_UNAVAILABLE": 503,
    "PLANTKG_E_INTERNAL": 500,
}

# 可重试：依赖暂不可用、求解超时（可换预算或转后台）
RETRYABLE_ERRORS = frozenset({"PLANTKG_E_DEPENDENCY_UNAVAILABLE", "PLANTKG_E_SOLVER_TIMEOUT"})


class PlantkgError(Exception):
    """端口与用例抛出的业务错误；入口层转成 ToolResult / ErrorResponse。"""

    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.details = details


def safe_error_details(exc: ValidationError) -> list[dict[str, Any]]:
    """把校验异常转成可返回、可记日志的结构：只留位置、类型、说明，不含输入值与 pydantic 错误上下文。

    hide_input_in_errors 只隐藏 str(exc)；exc.errors()/exc.json() 仍带原始输入，入口层与日志只能用本函数。
    """
    return [
        {"loc": [str(p) for p in err["loc"]], "type": err["type"], "msg": err["msg"]}
        for err in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def can_transition(table: dict[str, frozenset[str]], current: str, target: str) -> bool:
    """按迁移表判断状态变化是否合法；实现方迁移前必须调用。"""
    return target in table.get(current, frozenset())


# ---------------------------------------------------------------- 引用与定位


class Ref(Record):
    """动作、约束的依据：规则、配置项或资产版本，至少一个（04 ModelBuilder.add_constraint）。"""

    kind: Literal["rule", "config", "asset_version"]
    id: Identifier

    @model_validator(mode="after")
    def _check_id(self) -> Ref:
        if self.kind == "asset_version" and not self.id.startswith("asset://"):
            raise ValueError("asset_version 引用必须是 asset:// 标识")
        if self.kind == "config" and not self.id.startswith("config://"):
            raise ValueError("配置引用写成 config://{文件}#{键路径}")
        return self


class Locator(Record):
    """原文定位：页码、条款、表格单元格、图片框、行或时间区间，至少给一项。"""

    page: int | None = Field(default=None, ge=1)
    clause: str | None = None
    cell: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    time_range: Interval | None = None
    meter: str | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> Locator:
        if all(v is None for v in self.__dict__.values()):
            raise ValueError("locator 至少要有一项定位")
        return self


class TransformStep(Record):
    op: str  # 例：tz_convert、resample_15min、scale_load、dst_drop_repeat、unit_w_to_kw
    params: dict[str, Any] = Field(default_factory=dict)
    version: str
    note: str | None = None


class Quality(Record):
    flag: Literal["ok", "missing", "estimated", "suspect"] = "ok"
    note: str | None = None


# ---------------------------------------------------------------- 身份、会话与上下文


class Actor(Record):
    actor_id: Identifier
    roles: tuple[Role, ...] = ()
    display_name: str | None = None


class Context(Record):
    """一次调用的可信上下文。MCP 工具里由服务端按 ctx 查 RunSession 得到，不从工具参数逐项读取。"""

    plant_id: Identifier
    industry_pack: Identifier
    actor: Actor | None  # None 仅限无 ctx 的只读降级；不接受模型自报
    channel: ContextChannel
    agent_run_id: str | None = None  # 网关登记的一次 Agent 运行（09 D29）
    run_id: str | None = None  # 一次回放或业务运行
    trace_id: Identifier
    as_of: UtcDatetime
    version_manifest_id: str | None = None
    idempotency_key: str | None = None
    # v0.8：注册表按本次运行的模式取实现（Registry.resolve_mode）。取默认 live 时不序列化，
    # 升级前按 model_dump 算出的摘要（B15 求解回执、B16 执行幂等等）保持不变；trial/evaluation 照常写出，摘要可区分
    run_mode: RunMode = Field(default=RunMode.live, exclude_if=lambda mode: mode == RunMode.live)

    @model_validator(mode="after")
    def _actor_rules(self) -> Context:
        if self.channel in ("gateway", "rest", "mock_default") and self.actor is None:
            raise ValueError(f"channel={self.channel} 必须带服务端认证的 actor")
        if self.channel == "gateway" and self.agent_run_id is None:
            raise ValueError("网关上下文必须带 agent_run_id")
        if self.channel == "none" and self.actor is not None:
            raise ValueError("无 ctx 的运行没有可信身份，actor 必须为空")
        if self.channel == "offline" and self.agent_run_id is not None:
            raise ValueError("离线入口只有 run_id，没有 agent_run_id")
        return self

    @property
    def can_write(self) -> bool:
        return self.actor is not None and self.channel != "none"


class RunSession(Record):
    """网关为一次 Agent 运行登记的会话（03 §6.1）。ctx 不得出现在工具结果、证据与日志中。"""

    model_config = ConfigDict(hide_input_in_errors=True)  # 校验报错不带输入值，防 ctx 进异常与日志

    ctx: Ctx = Field(repr=False)
    actor: Actor
    plant_id: Identifier
    industry_pack: Identifier
    as_of: UtcDatetime
    visible_document_paths: tuple[str, ...] = ()
    agent_run_id: Identifier
    trace_id: Identifier
    created_at: UtcDatetime
    expires_at: UtcDatetime
    origin: Literal["gateway", "mock_default"] = "gateway"

    @model_validator(mode="after")
    def _ttl(self) -> RunSession:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at 必须晚于 created_at")
        return self


class ProposalOrigin(Record):
    agent_run_id: str | None = None
    run_id: str | None = None
    trace_id: Identifier
    call_id: str | None = None
    tool: str | None = None


class PendingAction(Record):
    """“Agent 提议、工作台确认”的待确认动作（09 D18）。"""

    action_id: Identifier
    kind: PendingActionKind
    plant_id: Identifier
    payload: dict[str, Any]
    proposed_by: ProposalOrigin
    proposed_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    status: PendingActionStatus = PendingActionStatus.proposed
    revision: int = Field(default=0, ge=0)  # 每次状态变化 +1；确认时须带 expected_revision
    target_ref: str | None = None  # 计划 plan_id@revision、候选 candidate_id、复核项 id 等
    target_revision: int | None = None
    decided_by: str | None = None
    decided_at: UtcDatetime | None = None
    reason: str | None = None
    edits: dict[str, Any] | None = None  # 复核员修订（仅 review_decision）
    idempotency_key: Identifier

    @model_validator(mode="after")
    def _decision_fields(self) -> PendingAction:
        decided = self.status in ("confirmed", "rejected")
        if decided:
            if self.decided_by is None or self.decided_at is None or not self.reason:
                raise ValueError("确认或拒绝必须记录确认人、时间与原因")
            if self.revision < 1:
                raise ValueError("已决动作的 revision 至少为 1")
            if self.decided_at < self.proposed_at:
                raise ValueError("decided_at 不能早于 proposed_at")
            if self.expires_at is not None and self.decided_at > self.expires_at:
                raise ValueError("过期后不能再确认或拒绝")
        elif self.decided_by is not None or self.decided_at is not None or self.reason is not None:
            raise ValueError("未决动作不能有确认人、确认时间或原因")
        if self.expires_at is not None and self.expires_at <= self.proposed_at:
            raise ValueError("expires_at 必须晚于 proposed_at")
        if self.kind != "review_decision" and self.target_ref is None:
            raise ValueError(f"{self.kind} 必须给 target_ref")
        if self.edits is not None and self.kind != "review_decision":
            raise ValueError("只有复核结论允许修订内容")
        _reject_injected(self.payload, "payload")
        if self.edits is not None:
            _reject_injected(self.edits, "edits")
        return self


class ActionDecisionRequest(Record):
    """POST /actions/{id}/confirm 与 /reject 的请求体。actor 从工作台登录会话获得。"""

    reason: Annotated[str, Field(min_length=1)]
    expected_revision: int = Field(ge=0)
    idempotency_key: Identifier
    edits: dict[str, Any] | None = None


class JobBudget(Record):
    time_limit_s: float = Field(gt=0)
    note: str | None = None


class Job(Record):
    """后台任务。不随 MCP 调用取消；get_job 服务端最多等 40 秒（09 D21）。"""

    job_id: Identifier
    kind: JobKind
    idempotency_key: Identifier
    status: JobStatus
    submitted_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    budget: JobBudget
    progress: float | None = Field(default=None, ge=0, le=1)
    result_ref: str | None = None  # 例：plan://{plan_id}@{revision}、report://{id}
    error: ToolError | None = None
    run_id: str | None = None
    agent_run_id: str | None = None

    @model_validator(mode="after")
    def _terminal_fields(self) -> Job:
        terminal = self.status in ("succeeded", "failed", "cancelled")
        if self.status == "succeeded" and (not self.result_ref or self.error is not None):
            raise ValueError("成功的任务必须给 result_ref 且不带 error")
        if self.status == "failed" and self.error is None:
            raise ValueError("失败的任务必须给 error")
        if terminal and self.finished_at is None:
            raise ValueError("终态任务必须有 finished_at")
        if not terminal and self.finished_at is not None:
            raise ValueError("未结束的任务不能有 finished_at")
        if self.status == "pending" and self.started_at is not None:
            raise ValueError("pending 任务不能有 started_at")
        if self.status in ("running", "succeeded") and self.started_at is None:
            raise ValueError(f"{self.status} 任务必须有 started_at")
        start = self.started_at or self.submitted_at
        if self.started_at is not None and self.started_at < self.submitted_at:
            raise ValueError("started_at 不能早于 submitted_at")
        if self.finished_at is not None and self.finished_at < start:
            raise ValueError("finished_at 不能早于开始时间")
        return self


class JobQueueSnapshot(Record):
    """JobRunner 只读队列快照（#69）：按运行与状态过滤后的任务页，供值守巡检枚举任务。"""

    jobs: tuple[Job, ...]
    total: int = Field(ge=0)  # 过滤后、分页前的任务数
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)


class JobExecutorHealth(Record):
    """JobRunner 只读执行器健康（#69）：线程数与队列深度。"""

    configured_workers: int = Field(ge=0)
    alive_workers: int = Field(ge=0)
    queue_depth: int = Field(ge=0)
    closed: bool = False  # 已停止接收新任务；此时 alive_workers 会降到 0


# ---------------------------------------------------------------- 资料与来源


class SourceRecord(Record):
    """规范化来源记录（03 §2.2）。value=None 表示缺测，不能当 0。"""

    source_id: Identifier
    source_version: Identifier
    content_hash: ContentHash
    locator: Locator
    nature: DataNature
    event_time: UtcDatetime
    interval: Interval | None = None  # 时段量（电量、平均功率）用区间
    available_at: UtcDatetime
    source_tz: str  # 原始时区，如 Europe/Berlin
    source_unit: str  # 原始单位，如 W
    quantity: Identifier  # 例：active_power、energy、pv_ac_power
    unit: Unit
    value: float | None
    transform_steps: tuple[TransformStep, ...] = ()
    quality: Quality = Quality()

    @model_validator(mode="after")
    def _visibility(self) -> SourceRecord:
        if self.nature == "measured":
            known_from = self.interval.end if self.interval else self.event_time
            if self.available_at < known_from:
                raise ValueError("实测数据 available_at 不能早于时段结束（= 时段结束 + 采集延迟）")
        if self.value is None and self.quality.flag not in ("missing", "suspect"):
            raise ValueError("缺测值必须标 quality.flag=missing/suspect，不能当正常值")
        return self


class SourceBundle(Record):
    bundle_id: Identifier
    source_id: Identifier
    source_version: Identifier
    mapping_version: Identifier
    records: tuple[SourceRecord, ...]
    next_cursor: str | None = None  # 分页查询（ts_query）的下一页
    license: str | None = None
    note: str | None = None


class TimeMap(Record):
    """时间映射：按当地钟面时间对齐再转 UTC（09 D28）。时区名由 B05 用 zoneinfo 校验。"""

    source_start: LocalDate  # 源数据当地日期
    plant_start: LocalDate  # 厂区当地日期
    source_tz: str
    plant_tz: str
    align: Literal["local_wall_clock"] = "local_wall_clock"
    dst_repeat_policy: Literal["keep_first"] = "keep_first"


class AssetVersion(Record):
    asset_version_id: AssetVersionId
    content_hash: ContentHash
    industry: Identifier
    asset_type: Identifier  # policy、process_doc、datasheet、image、table、timeseries
    source_uri: str
    media_type: str
    nature: DataNature
    available_at: UtcDatetime
    ingested_at: UtcDatetime
    parent_version_id: AssetVersionId | None = None
    entity_ids: tuple[EntityId, ...] = ()
    license: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def _hash_prefix(self) -> AssetVersion:
        hash8 = self.asset_version_id.split("/")[3]
        if self.content_hash[len("sha256:") : len("sha256:") + 8] != hash8:
            raise ValueError("asset_version_id 中的哈希前 8 位与 content_hash 不一致")
        return self


class Entity(Record):
    entity_id: EntityId
    entity_type: Identifier
    label: str
    aliases: tuple[str, ...] = ()
    plant_id: Identifier
    ontology_class: str | None = None  # Brick 或项目扩展命名空间中的类

    @model_validator(mode="after")
    def _plant(self) -> Entity:
        if self.entity_id.split("/")[2] != self.plant_id:
            raise ValueError("entity_id 中的厂区段必须等于 plant_id")
        return self


class AssetFragment(Record):
    fragment_id: Identifier
    asset_version_id: AssetVersionId
    locator: Locator
    kind: Literal["text", "table", "image"]
    text: str | None = None
    image_ref: str | None = None
    available_at: UtcDatetime


# ---------------------------------------------------------------- 本体与判别


class EvidenceRef(Record):
    asset_version_id: AssetVersionId | None = None
    locator: Locator | None = None
    evidence_id: str | None = None
    perspective: str | None = None  # 选样/视角（名称、单位、上下文等）
    excerpt: str | None = Field(default=None, max_length=500)


class OntologyCandidate(Record):
    candidate_id: Identifier
    layer: OntologyLayer
    subject: Identifier  # 待判别对象（实体、点位、关系对）
    options: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[EvidenceRef, ...] = ()
    recall_source: RecallSource
    status: ReviewStatus = ReviewStatus.pending

    @model_validator(mode="after")
    def _no_unknown_option(self) -> OntologyCandidate:
        if any(o.strip().lower() in UNDECIDABLE_CHOICES for o in self.options):
            raise ValueError("候选选项不放 Unknown/其他；无法确定由门控转人工")
        return self


UNDECIDABLE_CHOICES = frozenset({"unknown", "other", "none", "其他", "无法确定", "未知"})
JUDGE_MIN_ORDERS = 3  # 03 §4.3：3 种选项顺序取平均


class OrderResult(Record):
    """一种选项顺序下的判别结果。choice 必须是 distribution 的唯一第一名；
    第一名平分或无法判定时 choice 为空，该条结果不能支持自动接纳。"""

    order: tuple[str, ...] = Field(min_length=1)
    choice: str | None
    distribution: dict[str, Annotated[float, Field(ge=0, le=1)]]

    @model_validator(mode="after")
    def _consistent(self) -> OrderResult:
        if len(set(self.order)) != len(self.order):
            raise ValueError("order 中选项不能重复")
        if set(self.distribution) != set(self.order):
            raise ValueError("distribution 必须恰好覆盖 order 中全部选项，缺分数不能补 0")
        top = max(self.distribution.values())
        leaders = [k for k, v in self.distribution.items() if v == top]
        expected = leaders[0] if len(leaders) == 1 else None
        if self.choice != expected:
            raise ValueError(f"choice 必须是 distribution 的唯一第一名（平分时为空），应为 {expected!r}")
        return self


class TopK(Record):
    option: str
    prob: float = Field(ge=0, le=1)


class JudgeResult(Record):
    """decide() 结果。score 与校准概率 prob 分开；不足三种不同顺序时转慢路径/复核。"""

    candidate_id: Identifier
    choice: str
    score: float = Field(ge=0, le=1)
    prob: float | None = Field(default=None, ge=0, le=1)  # 校准后概率，可空
    coverage: float = Field(ge=0, le=1)
    backend: JudgeBackend
    model: str
    calibration_version: str | None = None
    orders: tuple[OrderResult, ...] = ()
    order_agree: bool
    cross_backend_agree: bool | None = None  # 只跑一个后端时为空
    top_k: tuple[TopK, ...] = Field(default=(), max_length=3)
    candidate_recall_source: RecallSource
    vector_top: str | None = None  # 向量召回的第一名；非向量召回时为空
    gate_accept: bool
    gate_kind: Literal["ontology", "domain"] = "ontology"  # B24：派域不要求向量首选
    distribution: dict[str, Annotated[float, Field(ge=0, le=1)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_unknown(self) -> JudgeResult:
        if self.choice.strip().lower() in UNDECIDABLE_CHOICES:
            raise ValueError("“其他/无法确定”由门控转人工表示，不作为判别选项")
        if self.prob is not None and self.calibration_version is None:
            raise ValueError("有 prob 必须给 calibration_version")
        if self.gate_kind == "domain" and (self.candidate_recall_source == "vector" or self.vector_top is not None):
            raise ValueError("派域题不使用向量召回或 vector_top")
        candidate_sets = {frozenset(o.order) for o in self.orders}
        if len(candidate_sets) > 1:
            raise ValueError("各顺序必须是同一候选集的排列")
        if candidate_sets:
            options = next(iter(candidate_sets))
            if self.distribution and (set(self.distribution) != options or abs(sum(self.distribution.values()) - 1) > 1e-6):
                raise ValueError("distribution 必须覆盖全部候选且归一")
            if self.choice not in options:
                raise ValueError("choice 必须属于候选集")
            if self.vector_top is not None and self.vector_top not in options:
                raise ValueError("vector_top 必须属于候选集")
        order_choices = {o.choice for o in self.orders}
        if self.order_agree and (len(order_choices) > 1 or None in order_choices):
            raise ValueError("order_agree 与各顺序的 choice 不一致（平分或无法判定不算一致）")
        if self.gate_accept:
            distinct_orders = {o.order for o in self.orders}
            if len(distinct_orders) < JUDGE_MIN_ORDERS:
                raise ValueError(f"自动接纳至少要 {JUDGE_MIN_ORDERS} 种不同选项顺序")
            if not self.order_agree or order_choices != {self.choice}:
                raise ValueError("顺序不一致不能自动接纳：各顺序第一名都须等于 choice")
            if self.gate_kind == "ontology" and (self.candidate_recall_source != "vector" or self.vector_top != self.choice):
                raise ValueError("自动接纳须与向量第一名一致（vector_top == choice）")
        return self


# ---------------------------------------------------------------- 规则


def _bounds_number(value: Any) -> Any:
    """与包层校验同口径：bool 不是数值（非 strict 模式下 True 会被收成 1.0）；int→float 照旧。"""
    if isinstance(value, bool):
        raise ValueError("bounds 的上下界必须是数值，不能是 bool")
    return value


_BoundValue = Annotated[float | None, BeforeValidator(_bounds_number)]


class Bounds(Record):
    lower: _BoundValue = None
    upper: _BoundValue = None

    @model_validator(mode="after")
    def _any(self) -> Bounds:
        if self.lower is None and self.upper is None:
            raise ValueError("bounds 至少给 lower 或 upper")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("bounds.lower 不能大于 upper")
        return self


class Rule(Record):
    """可编译规则（03 §4.4）。单位不明、对象不明或冲突的规则进待处理清单，不在这里出现。"""

    rule_id: Identifier
    kind: Identifier  # 由包注册的 RuleKind，如 tariff_period、operation_window
    subject_ids: tuple[EntityId, ...] = Field(min_length=1)
    condition: dict[str, Any] = Field(default_factory=dict)
    quantity: Identifier
    unit: Unit
    bounds: Bounds | None = None
    value: float | None = None  # 点值规则（如电价）；与 bounds 至少一项
    effective_from: UtcDatetime
    effective_to: UtcDatetime | None = None
    available_at: UtcDatetime  # 取来源资料的 available_at；复核/发布时间只入审计
    source_asset_version: AssetVersionId | None = None
    config_ref: str | None = None  # 受信配置来源 config://...
    locator: Locator | None = None
    nature: DataNature
    review_status: ReviewStatus
    reviewer: str | None = None
    compiler_version: str | None = None
    rule_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _provenance(self) -> Rule:
        if self.bounds is None and self.value is None:
            raise ValueError("规则必须给 bounds 或 value")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to 必须晚于 effective_from")
        if self.source_asset_version is None and self.config_ref is None:
            raise ValueError("规则必须指向来源资料版本或受信配置")
        if self.source_asset_version is not None and self.locator is None:
            raise ValueError("来自资料的规则必须有原文定位 locator")
        if self.review_status == "approved" and not self.reviewer:
            raise ValueError("approved 规则必须记录复核人")
        if self.review_status == "trusted_config" and self.config_ref is None:
            raise ValueError("trusted_config 规则必须给 config_ref")
        return self

    def is_visible(self, as_of: datetime) -> bool:
        """可见性：规划时是否已知。"""
        return self.available_at <= as_of

    def applies_to(self, slot_start: datetime) -> bool:
        """适用性：effective_from <= 时段开始 < effective_to。"""
        if slot_start < self.effective_from:
            return False
        return self.effective_to is None or slot_start < self.effective_to

    @property
    def compilable(self) -> bool:
        return self.review_status in COMPILABLE_REVIEW_STATUSES


class RuleCandidate(Record):
    """抽取器输出的候选规则（B25）；alternatives 供反事实评分取第二候选（09 D31）。"""

    candidate_id: Identifier
    draft: dict[str, Any]
    alternatives: tuple[dict[str, Any], ...] = ()
    extractor_version: str
    source_asset_version: AssetVersionId
    locator: Locator
    confidence: float | None = Field(default=None, ge=0, le=1)


class PendingIssue(Record):
    issue_id: Identifier
    reason: PendingIssueReason
    rule_id: str | None = None
    subject: str | None = None
    detail: str
    refs: tuple[Ref, ...] = ()


# ---------------------------------------------------------------- 状态、预测与规划


class StateSnapshot(Record):
    """回放状态快照。只读；真值引用不返回在线 Agent。"""

    run_id: Identifier
    snapshot_id: Identifier
    event_seq: int = Field(ge=0)
    as_of: UtcDatetime
    version_manifest_id: Identifier
    pack_states: dict[str, dict[str, Any]]


class InputSlice(Record):
    source_ids: tuple[str, ...]
    visible_until: UtcDatetime  # 输入只用 available_at <= visible_until 的数据
    window: Interval | None = None
    note: str | None = None  # 例：持久性预测取 24h 前，不可见时段取 48h 前（09 D28）


class Forecast(Record):
    """预测单独表示，不能当 measured。"""

    forecast_id: Identifier
    quantity: Identifier  # 例：base_load、pv_available
    unit: Unit
    issued_at: UtcDatetime
    target: Interval
    step_minutes: int = Field(gt=0)
    values: tuple[float | None, ...]
    lower: tuple[float | None, ...] | None = None
    upper: tuple[float | None, ...] | None = None
    model_version: Identifier
    params: dict[str, Any] = Field(default_factory=dict)  # 例：{"pv_correction_k": 1.0}
    input_slice: InputSlice
    coverage: float = Field(ge=0, le=1)
    nature: Literal["derived"] = "derived"

    @model_validator(mode="after")
    def _shape(self) -> Forecast:
        slots = (self.target.end - self.target.start) / timedelta(minutes=self.step_minutes)
        if slots != int(slots) or len(self.values) != int(slots):
            raise ValueError("values 长度必须等于目标区间的时段数")
        for band in (self.lower, self.upper):
            if band is not None and len(band) != len(self.values):
                raise ValueError("区间上下界长度必须与 values 一致")
        if self.input_slice.visible_until > self.issued_at:
            raise ValueError("预测输入不能晚于发布时刻（防未来泄漏）")
        window = self.input_slice.window
        if window is not None and window.end > self.input_slice.visible_until:
            raise ValueError("输入窗口不能超过 visible_until（防未来泄漏）")
        if self.issued_at > self.target.start:
            raise ValueError("日前预测必须在目标区间开始前发布")
        return self


class ForecastBundle(Record):
    forecasts: tuple[Forecast, ...]
    uncertainty_summary: dict[str, float] = Field(default_factory=dict)


class SolverBudget(Record):
    time_limit_s: float = Field(gt=0)
    mip_rel_gap: float = Field(gt=0, lt=1)
    threads: int = Field(default=1, ge=1)
    random_seed: int = 0


class PlanningRequest(Record):
    context: Context
    snapshot_id: Identifier
    forecast_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]  # 已发布/已审核、对 as_of 可见的适用规则
    horizon: Interval
    step_minutes: int = Field(default=15, gt=0)
    solver_budget: SolverBudget
    packs: tuple[str, ...] = Field(min_length=1)
    policy: Literal["B0", "B1", "B2"] = "B2"  # B0 不优化、B1 只优化储能、B2 储能 + 工序平移（03 §8）

    @model_validator(mode="after")
    def _context(self) -> PlanningRequest:
        if self.context.version_manifest_id is None or self.context.run_id is None:
            raise ValueError("规划请求必须绑定 run_id 与 version_manifest_id")
        if not self.context.can_write:
            raise ValueError("无 ctx 的只读降级上下文不能发起规划")
        return self


class Action(Record):
    action_id: Identifier
    object: EntityId
    quantity: Identifier  # 例：charge_power、discharge_power、operation_start、pv_curtail
    unit: Unit
    value: float
    interval: Interval
    refs: tuple[Ref, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()


class SolverInfo(Record):
    name: str
    version: str
    params: dict[str, Any]


class Plan(Record):
    plan_id: Identifier
    revision: int = Field(ge=0)
    run_id: Identifier
    status: PlanStatus
    solver_status: SolverStatus
    valid_interval: Interval
    version_manifest_id: Identifier
    snapshot_id: Identifier
    forecast_ids: tuple[str, ...]
    policy: Literal["B0", "B1", "B2"]
    actions: tuple[Action, ...]
    objective_terms: dict[str, float] = Field(default_factory=dict)
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _solver(self) -> Plan:
        if self.solver_status not in SOLVER_STATUSES_WITH_PLAN:
            raise ValueError("只有 optimal/feasible 才产生计划")
        ids = [a.action_id for a in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("计划内 action_id 必须唯一")
        for action in self.actions:
            if not (self.valid_interval.start <= action.interval.start and action.interval.end <= self.valid_interval.end):
                raise ValueError(f"动作 {action.action_id} 超出计划有效区间")
        return self


class PlanResult(Record):
    solver_status: SolverStatus
    plan: Plan | None = None
    solver: SolverInfo
    mip_gap: float | None = None
    objective_value: float | None = None
    objective_bound: float | None = None
    elapsed_s: float = Field(ge=0)
    objective_terms: dict[str, float] = Field(default_factory=dict)
    conflict_hints: tuple[str, ...] = ()
    pending_issues: tuple[PendingIssue, ...] = ()
    validation_errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _plan_presence(self) -> PlanResult:
        has_plan = self.plan is not None
        if (self.solver_status in SOLVER_STATUSES_WITH_PLAN) != has_plan:
            raise ValueError("optimal/feasible 必须带计划，其余状态不得带计划")
        if self.plan is not None:
            if self.plan.solver_status != self.solver_status:
                raise ValueError("plan.solver_status 必须与结果一致")
            if self.objective_value is None or self.objective_bound is None:
                raise ValueError("有计划时必须给目标值与下界")
            if self.validation_errors:
                raise ValueError("独立校验未通过应返回 error，不能带计划")
        if self.solver_status == "feasible" and self.mip_gap is None:
            raise ValueError("feasible 必须附实际 mip_gap")
        if self.solver_status == "infeasible" and not self.conflict_hints:
            raise ValueError("infeasible 必须附冲突线索")
        return self


class ContributionReceipt(Record):
    """包向共享模型贡献的回执；包不创建模型、不设目标、不设求解参数。"""

    pack: Identifier
    variables: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    balance_terms: tuple[str, ...] = ()
    cost_terms: tuple[str, ...] = ()
    refs: tuple[Ref, ...] = ()
    pending_issues: tuple[PendingIssue, ...] = ()


class ConstraintContribution(Record):
    rule_id: Identifier
    pack: Identifier
    constraint_names: tuple[str, ...] = Field(min_length=1)
    refs: tuple[Ref, ...] = Field(min_length=1)
    compiler_version: str


class GateDecision(Record):
    plan_id: Identifier
    revision: int = Field(ge=0)
    outcome: GateOutcome
    reasons: tuple[str, ...] = Field(min_length=1)
    policy_version: Identifier
    thresholds: dict[str, float] = Field(default_factory=dict)
    assessed_at: UtcDatetime
    pending_action_id: str | None = None  # review_required 时生成的计划确认动作
    policy_outcome: GateOutcome | None = None  # 策略原始判断；Agent 提议时 outcome 固定 review_required

    @model_validator(mode="after")
    def _pending(self) -> GateDecision:
        if self.outcome == "review_required" and self.pending_action_id is None:
            raise ValueError("转人工必须关联待确认动作")
        return self


class Approval(Record):
    plan_id: Identifier
    revision: int = Field(ge=0)
    mode: Literal["auto", "manual"]
    actor: str | None = None
    approved_at: UtcDatetime
    action_id: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _manual(self) -> Approval:
        if self.mode == "manual" and (self.actor is None or self.action_id is None):
            raise ValueError("人工确认必须记录确认人与待确认动作")
        return self


# ---------------------------------------------------------------- 执行与账本


class ExecutedAction(Record):
    action_id: Identifier
    planned_value: float
    executed_value: float
    unit: Unit
    interval: Interval
    curtailed: float = 0.0  # 削减量（先弃光，再削减放电，03 §2.4）
    reason: str | None = None


class LedgerEntry(Record):
    """一个时段的交流侧账本（非负量，kW 时段平均、kWh 时段电量）。"""

    interval: Interval
    pv_available_kwh: float = Field(ge=0)
    pv_used_kwh: float = Field(ge=0)
    pv_curtailed_kwh: float = Field(ge=0)
    grid_import_kwh: float = Field(ge=0)
    grid_export_kwh: float = Field(ge=0)
    charge_kwh: float = Field(ge=0)
    discharge_kwh: float = Field(ge=0)
    load_kwh: float = Field(ge=0)
    energy_start_kwh: float = Field(ge=0)
    energy_end_kwh: float = Field(ge=0)
    soc_end: float = Field(ge=0, le=1)
    price_cny_per_kwh: float | None = None  # 缺价不能当 0，进待处理
    cost_cny: float | None = None
    residual_kwh: float  # 只作报告：必须等于按各项重算的值，契约不信任调用方填写
    load_breakdown: dict[LoadCategory, float] | None = None  # v0.8 负荷分项电量（kWh），合计等于 load_kwh

    @property
    def computed_residual_kwh(self) -> float:
        """光伏使用 + 购电 + 放电 − 负荷 − 充电 − 外送（03 §2.4）。"""
        supply = self.pv_used_kwh + self.grid_import_kwh + self.discharge_kwh
        demand = self.load_kwh + self.charge_kwh + self.grid_export_kwh
        return supply - demand

    @model_validator(mode="after")
    def _entry(self) -> LedgerEntry:
        pv_gap = self.pv_used_kwh + self.pv_curtailed_kwh - self.pv_available_kwh
        if abs(pv_gap) > LEDGER_TOLERANCE["energy_abs_kwh"]:
            raise ValueError("光伏使用 + 弃光 必须等于光伏可发")
        if abs(self.residual_kwh - self.computed_residual_kwh) > 1e-6:
            raise ValueError("residual_kwh 必须等于按各项电量重算的残差")
        if self.price_cny_per_kwh is None and self.cost_cny is not None:
            raise ValueError("缺价时段不能有费用")
        if self.load_breakdown is not None:
            if any(v < 0 for v in self.load_breakdown.values()):
                raise ValueError("负荷分项电量不能为负")
            gap = sum(self.load_breakdown.values()) - self.load_kwh
            if abs(gap) > max(LEDGER_TOLERANCE["energy_abs_kwh"], LEDGER_TOLERANCE["energy_rel"] * self.load_kwh):
                raise ValueError(f"负荷分项之和与 load_kwh 相差 {gap:.6g} kWh")
        return self

    def energy_within_tolerance(self) -> bool:
        """07 Q16：重算的能量残差 ≤ max(1e-3 kWh, 1e-6 × 各项电量绝对值之和)。"""
        terms = (self.pv_used_kwh, self.grid_import_kwh, self.discharge_kwh, self.load_kwh, self.charge_kwh, self.grid_export_kwh)
        energy_tol = max(LEDGER_TOLERANCE["energy_abs_kwh"], LEDGER_TOLERANCE["energy_rel"] * sum(abs(t) for t in terms))
        return abs(self.computed_residual_kwh) <= energy_tol

    def soc_residual(self, storage: StorageParams) -> float:
        """SOC 递推残差：E[t+1] = E[t] + η_c·充电 − 放电/η_d，与 soc_end 两者取较大偏差（SOC 口径）。"""
        expected_end = self.energy_start_kwh + storage.eta_c * self.charge_kwh - self.discharge_kwh / storage.eta_d
        recursion = (self.energy_end_kwh - expected_end) / storage.capacity_kwh
        soc_gap = self.soc_end - self.energy_end_kwh / storage.capacity_kwh
        return max(abs(recursion), abs(soc_gap))


class StorageParams(Record):
    """账本复核 SOC 所需的储能参数（来自版本清单固定的配置）。"""

    capacity_kwh: float = Field(gt=0)
    eta_c: float = Field(gt=0, le=1)
    eta_d: float = Field(gt=0, le=1)
    soc_min: float = Field(ge=0, le=1)
    soc_max: float = Field(ge=0, le=1)


class Ledger(Record):
    """账本。balanced 由 B08 LedgerCalculator 判定；契约再独立复核能量平衡、光伏守恒、跨时段连续、
    充放互斥与（给定储能参数时的）SOC 递推。费用按电价规则另算、平移守恒、总表减分表非负等需要外部数据，
    由 B08 validate 负责并写入 violations。"""

    ledger_id: Identifier
    kind: Literal["plan", "execution"]  # 计划账本（预测输入）与执行账本（实际数据）分开（09 D30）
    run_id: Identifier
    plan_id: Identifier
    plan_revision: int = Field(ge=0)
    version_manifest_id: Identifier
    entries: tuple[LedgerEntry, ...] = Field(min_length=1)
    total_cost_cny: float | None
    peak_import_kw: float = Field(ge=0)  # 只作指标，不进目标
    tolerance_policy_version: Identifier
    calculator_version: Identifier  # 计算并判定 balanced 的 B08 实现版本
    source_refs: tuple[str, ...] = Field(min_length=1)  # 计划账本：预测 ID；执行账本：实测来源包 ID
    storage: StorageParams | None = None  # 无储能时为空，此时充放必须为 0
    balanced: bool
    violations: tuple[str, ...] = ()
    objective_gap_cny: float | None = None  # 计划账本：（购电费用 + 非购电成本项）与模型目标之差
    objective_value_cny: float | None = None  # 计划账本：模型成本项合计（含非购电项）
    objective_terms: dict[str, float] | None = None  # 计划账本：非购电成本项明细（如储能衰减、固定费）

    def recheck(self) -> list[str]:
        """契约层可独立复核的问题；balanced=True 时必须为空。"""
        problems: list[str] = []
        zero = LEDGER_TOLERANCE["energy_abs_kwh"]
        for i, e in enumerate(self.entries):
            if not e.energy_within_tolerance():
                problems.append(f"entries[{i}] 能量残差 {e.computed_residual_kwh:.6g} kWh 超出容差")
            if e.charge_kwh > zero and e.discharge_kwh > zero:
                problems.append(f"entries[{i}] 同时充放")
            if self.storage is None:
                if e.charge_kwh > zero or e.discharge_kwh > zero:
                    problems.append(f"entries[{i}] 无储能参数却有充放")
                continue
            if e.soc_residual(self.storage) > LEDGER_TOLERANCE["soc_abs"]:
                problems.append(f"entries[{i}] SOC 递推残差超出容差")
            if not (self.storage.soc_min - LEDGER_TOLERANCE["soc_abs"] <= e.soc_end <= self.storage.soc_max + LEDGER_TOLERANCE["soc_abs"]):
                problems.append(f"entries[{i}] SOC 越界")
        for i in range(1, len(self.entries)):
            prev, cur = self.entries[i - 1], self.entries[i]
            if cur.interval.start != prev.interval.end:
                problems.append(f"entries[{i}] 与上一时段不连续")
            if abs(cur.energy_start_kwh - prev.energy_end_kwh) > zero:
                problems.append(f"entries[{i}] 起始电量与上一时段期末不一致")
        return problems

    @model_validator(mode="after")
    def _balance_flag(self) -> Ledger:
        if self.balanced and self.violations:
            raise ValueError("有违规项时 balanced 不能为 True")
        if self.balanced:
            problems = self.recheck()
            if problems:
                raise ValueError(f"契约复核未通过（容差 {LEDGER_TOLERANCE['policy_version']}），balanced 不能为 True：{problems[:3]}")
        return self


class ExecutionResult(Record):
    run_id: Identifier
    plan_id: Identifier
    plan_revision: int = Field(ge=0)
    event_seq: int = Field(ge=0)
    executed: tuple[ExecutedAction, ...]
    rejected: tuple[str, ...] = ()  # 未执行动作与原因
    ledger: Ledger
    soc_deviation: float = 0.0
    forecast_error: dict[str, float] = Field(default_factory=dict)
    idempotency_key: Identifier

    @model_validator(mode="after")
    def _ledger_kind(self) -> ExecutionResult:
        if self.ledger.kind != "execution":
            raise ValueError("执行结果只能挂执行账本")
        if (self.ledger.run_id, self.ledger.plan_id, self.ledger.plan_revision) != (self.run_id, self.plan_id, self.plan_revision):
            raise ValueError("执行账本的 run_id/plan_id/plan_revision 必须与执行结果一致")
        return self


class EvaluationReport(Record):
    report_id: Identifier
    eval_days: tuple[str, ...] = Field(min_length=1)
    independent_eval_day: bool  # B0/B1/B2 对照结论只在独立评测日上报
    # B0/B1/B2 为日前规划对照；baseline/independent/coordinated 为 v0.8 场景对照（基线组、独立组、协同组）
    policies: tuple[Literal["B0", "B1", "B2", "baseline", "independent", "coordinated"], ...]
    metrics: dict[str, dict[str, float | None]]  # policy -> 指标名 -> 值
    comparable: bool
    notes: tuple[str, ...] = ()
    run_ids: tuple[str, ...]


# ---------------------------------------------------------------- 版本与进化


class ComponentRef(Record):
    ref: Identifier  # 例：rules@v3、ontology://graph/2026-11-05
    content_hash: ContentHash


class VersionManifest(Record):
    version_manifest_id: Identifier
    components: dict[ManifestComponent, ComponentRef] = Field(min_length=1)
    status: ManifestStatus
    previous_active_id: str | None = None
    ever_active: bool = False  # 回滚只能回到 ever_active 的清单
    created_at: UtcDatetime
    activated_at: UtcDatetime | None = None
    activated_by: str | None = None
    reason: str | None = None
    manifest_hash: ContentHash

    @model_validator(mode="after")
    def _lifecycle(self) -> VersionManifest:
        if self.status == "active" and (self.activated_at is None or not self.ever_active):
            raise ValueError("活动清单必须有 activated_at 且 ever_active")
        if self.status == "active" and (not self.activated_by or not self.reason):
            raise ValueError("激活必须记录操作人与原因（09 D16）")
        if self.status == "retired" and not self.ever_active:
            raise ValueError("retired 只用于曾经激活过的清单")
        if self.status in ("prepared", "verified", "discarded") and self.ever_active:
            raise ValueError("未激活状态不能 ever_active")
        return self


class Candidate(Record):
    candidate_id: Identifier
    kind: CandidateKind
    base_manifest_id: Identifier
    changes: dict[ManifestComponent, ComponentRef] = Field(min_length=1)
    status: CandidateStatus
    trigger: str  # 执行偏差、新政策、复核结论等
    created_at: UtcDatetime
    validation_policy_version: Identifier
    backtest_report_id: str | None = None
    publish_action_id: str | None = None

    @model_validator(mode="after")
    def _status(self) -> Candidate:
        if self.status in ("validated", "published") and self.backtest_report_id is None:
            raise ValueError("validated/published 候选必须有回测报告")
        if self.status == "published" and self.publish_action_id is None:
            raise ValueError("发布必须经复核员确认的待确认动作")
        return self


class BacktestReport(Record):
    report_id: Identifier
    candidate_id: Identifier
    base_manifest_id: Identifier
    split_ids: tuple[str, ...] = Field(min_length=1)  # 冻结验证日；不得含最终测试集
    seed: int
    metrics: dict[str, dict[str, float | None]]  # base/candidate -> 指标
    constraint_violations: dict[str, int]
    failures: tuple[str, ...] = ()
    thresholds: dict[str, float]
    policy_version: Identifier
    verdict: Literal["pass", "fail", "inconclusive"]
    artifact_hash: ContentHash
    job_id: str | None = None
    # 快回测：只推进到目标类事件处理完 + 收尾余量，或固定短窗口；报告如实标注
    scope: Literal["full", "fast_window"] = "full"
    # b32-v4：artifact_hash 覆盖的回测绑定信息（场景文件 sha/seed/窗口、版本、回退次数）与耗时、机器配置、告警；旧报告为空
    binding: dict[str, Any] = Field(default_factory=dict)


class EvolutionTimelineEntry(Record):
    seq: int = Field(ge=1)
    event: Identifier
    occurred_at: UtcDatetime
    details: dict[str, Any] = Field(default_factory=dict)


class SkillSummary(Record):
    """B43：候选 Skill 列表项（当前版本与发布状态）。"""

    name: Identifier
    latest_version: int = Field(ge=1)
    candidate_id: Identifier | None = None
    manifest_id: Identifier | None = None
    status: Literal["candidate", "active", "published", "rolled_back", "overridden"] = "published"
    published_at: UtcDatetime | None = None
    source_tickets: tuple[str, ...] = ()


class SkillVersionRecord(Record):
    """B43：一个 Skill 版本的发布或回滚记录（版本链的一条）。"""

    name: Identifier
    version: int = Field(ge=1)
    manifest_id: Identifier
    candidate_id: Identifier
    # rollback_to_none：回滚到不含该 Skill 的清单；overridden：被同槽位的其他来源（如 B32 调查模板）发布顶掉
    action: Literal["publish", "rollback", "rollback_to_none", "overridden"]
    verdict: Literal["pass", "fail", "inconclusive"]
    failures: tuple[str, ...] = ()
    at: UtcDatetime
    actor: str | None = None
    source_tickets: tuple[str, ...] = ()
    run_id: str | None = None
    from_manifest_id: str | None = None
    current: bool = False


class ExecutionRecord(Record):
    """B47 处置执行记录的只读视图：经钩子 b47.execution_records 交给 B43 挖掘，跨平面不直连执行库。"""

    ticket_id: str
    run_id: str | None = None
    event_type: str
    entity_id: str | None = None
    entity_type: str | None = None
    command: str
    action_index: int = Field(default=0, ge=0)
    outcome: str | None = None  # approved/accepted/correct/rejected/wrong/corrected；缺省时由 B32 反馈补
    system_error: bool = False
    at: UtcDatetime | None = None
    nature: DataNature = "derived"


class FrozenSplit(Record):
    split_id: Identifier
    purpose: Literal["calibration", "scoring", "validation", "test"]
    days: tuple[str, ...] = Field(min_length=1)
    frozen_at: UtcDatetime
    content_hash: ContentHash


# ---------------------------------------------------------------- 证据与事件


def _injected_keys(value: Any, path: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in INJECTED_PARAM_NAMES:
                found.append(f"{path}.{key}")
            found.extend(_injected_keys(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            found.extend(_injected_keys(item, f"{path}[{i}]"))
    return found


def _reject_injected(params: Any, where: str) -> None:
    """递归检查键名。注入值本身由出站层用 contains_secret 扫描（契约不知道 ctx 的值）。"""
    leaked = _injected_keys(params, where)
    if leaked:
        raise ValueError(f"{where} 不得回显注入参数：{leaked}")


class EvidenceEvent(Record):
    """证据事件：写入即可订阅，供网关实时合流（09 D19）。"""

    event_id: Identifier
    seq: int = Field(ge=0)
    occurred_at: UtcDatetime
    agent_run_id: str | None = None
    run_id: str | None = None
    trace_id: Identifier
    call_id: str | None = None  # plantkg 分配
    action_id: str | None = None
    event_type: EvidenceEventType
    tool: str | None = None
    path: EvidencePath = EvidencePath.none
    rule_ids: tuple[str, ...] = ()
    sources: tuple[EvidenceRef, ...] = ()
    version_manifest_id: str | None = None
    available_at: UtcDatetime | None = None
    nature: DataNature | None = None
    result_status: ResultStatus
    params: dict[str, Any] = Field(default_factory=dict)  # 系统绑定后的实际参数，去掉注入值
    model_params: dict[str, Any] | None = None  # 模型填值，用于“模型填值 → 系统绑定值”对照
    summary: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _no_echo(self) -> EvidenceEvent:
        _reject_injected(self.params, "params")
        if self.model_params is not None:
            _reject_injected(self.model_params, "model_params")
        if self.event_type in ("tool_called", "tool_result") and (self.call_id is None or self.tool is None):
            raise ValueError("工具事件必须带 call_id 与 tool")
        return self


class EvidencePage(Record):
    items: tuple[EvidenceEvent, ...]
    next_cursor: str | None = None


class NexentEventRef(Record):
    """保留 Nexent 原事件引用（07 Q03）；字段只作提示，不作为工具调用事实。"""

    type: str
    index: int = Field(ge=0)
    conversation_id: str | None = None


class UIEvent(Record):
    """网关合流后的统一事件（04 UIEvent）。"""

    type: UIEventType
    agent_run_id: Identifier
    trace_id: Identifier
    seq: int = Field(ge=0)
    ts: UtcDatetime
    call_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    nexent_ref: NexentEventRef | None = None
    event_id: str | None = None  # 来源证据事件或 Nexent 事件，去重用

    @model_validator(mode="after")
    def _payload(self) -> UIEvent:
        _reject_injected(self.payload, "payload")
        if self.type in ("tool_started", "tool_finished") and self.call_id is None:
            raise ValueError("工具事件必须带 plantkg call_id")
        required = UI_EVENT_PAYLOAD_KEYS.get(self.type, ())
        missing = [key for key in required if key not in self.payload]
        if missing:
            raise ValueError(f"{self.type} 的 payload 缺少 {missing}")
        if self.type in ("run_finished", "run_failed", "run_cancelled"):
            reason = self.payload["terminal_reason"]
            allowed = TERMINAL_REASON_BY_EVENT[self.type]
            if reason not in allowed:
                raise ValueError(f"{self.type} 的 terminal_reason 只能是 {sorted(allowed)}")
        return self


UI_EVENT_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "run_started": ("agent_name",),
    "tool_started": ("tool",),
    "tool_finished": ("tool", "business_status"),
    "evidence_added": ("evidence_id", "event_type"),
    "job_progress": ("job_id", "status"),
    "review_required": ("action_id", "kind"),
    "answer_delta": ("text",),
    "plan_ready": ("plan_id", "revision"),
    "run_finished": ("terminal_reason",),
    "run_failed": ("terminal_reason", "message"),
    "run_cancelled": ("terminal_reason",),
    "keepalive": (),
}

TERMINAL_REASON_BY_EVENT: dict[str, frozenset[str]] = {
    "run_finished": frozenset({"completed", "completed_from_deltas", "stream_lost_recovered"}),
    "run_failed": frozenset({"timeout", "stream_lost", "error"}),
    "run_cancelled": frozenset({"cancelled"}),
}


# ---------------------------------------------------------------- 工具结果与 MCP 规格


class ToolError(Record):
    code: ErrorCodeStr
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known(self) -> ToolError:
        if self.code not in ERROR_HTTP_STATUS:
            raise ValueError(f"未登记的错误码 {self.code}")
        if self.retryable != (self.code in RETRYABLE_ERRORS):
            raise ValueError("retryable 必须与 RETRYABLE_ERRORS 一致")
        _reject_injected(self.details, "error.details")
        return self


Job.model_rebuild()


class PageInfo(Record):
    next_cursor: str | None = None
    page_size: int = Field(ge=1, le=200)
    total: int | None = None


class ToolResult(Record):
    """工具返回（09 D22）。不靠 HTTP 200 判成功；给 Nexent 的文本由 to_agent_text() 生成。"""

    schema_version: Literal["1"] = "1"
    ok: bool
    business_status: BusinessStatus
    job_id: str | None = None
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    evidence_ids: tuple[str, ...] = ()
    run_id: str | None = None
    version_manifest_id: str | None = None
    page: PageInfo | None = None
    notice: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> ToolResult:
        if self.ok == (self.error is not None):
            raise ValueError("ok=False 当且仅当带 error")
        if (self.business_status == "failed") != (not self.ok):
            raise ValueError("business_status=failed 当且仅当 ok=False")
        if self.business_status == "pending" and not self.job_id:
            raise ValueError("pending 必须带 job_id")
        if self.business_status == "degraded" and not self.notice:
            raise ValueError("降级结果必须带 notice")
        if self.data is not None:
            _reject_injected(self.data, "data")
        return self

    def to_agent_text(self) -> str:
        """返回给 Nexent 的文本：错误码/降级提示固定在开头，UTF-8 不超过 8KB。"""
        body = json.dumps(self.model_dump(mode="json", exclude_none=True), ensure_ascii=False, separators=(",", ":"))
        if self.error is not None:
            text = f"{self.error.code}: {_one_line(self.error.message)}\n{body}"
        elif self.business_status == "degraded":
            text = f"{NOTICE_NO_CTX_PREFIX} {_one_line(self.notice or '')}\n{body}"
        else:
            text = body
        if len(text.encode("utf-8", "surrogatepass")) > TOOL_TEXT_MAX_BYTES:
            raise PlantkgError(ErrorCode.RESULT_TOO_LARGE, f"工具文本超过 {TOOL_TEXT_MAX_BYTES} 字节，须分页")
        return text


def _one_line(text: str) -> str:
    """首行只放错误码与说明，防止说明里的换行伪造第二个前缀行。"""
    return " ".join(text.split())


def tool_error(code: ErrorCode, message: str, **details: Any) -> ToolResult:
    return ToolResult(
        ok=False,
        business_status=BusinessStatus.failed,
        error=ToolError(code=code.value, message=message, retryable=code.value in RETRYABLE_ERRORS, details=details),
    )


def contains_secret(text: str, secrets: tuple[str, ...]) -> bool:
    """检查文本是否回显了注入值（ctx 等）。网关与工具层在出站前调用。"""
    return any(secret and secret in text for secret in secrets)


class ParamSpec(Record):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    type: Literal["string", "integer", "number", "boolean", "object", "array"]
    required: bool = False
    description: str
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def _not_injected(self) -> ParamSpec:
        if self.name in INJECTED_PARAM_NAMES:
            raise ValueError("注入参数由 ToolSpec.accepts_ctx 声明，不写进普通参数")
        return self


class ToolSpec(Record):
    """注册表中的工具规格。Nexent 中的名字为 plantkg_ 前缀 + name。"""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    plane: Literal["knowledge", "decision", "world", "evolution", "core"]
    provider: Annotated[str, Field(pattern=r"^B\d{2}$")]
    extended_by: tuple[str, ...] = ()
    description: str
    mode: Literal["read", "propose", "write", "job"]
    accepts_ctx: bool = True  # 生成注入白名单；ctx 可选、默认空
    exposed_to_agent: bool = True
    optional: bool = False  # 可选扩展分支提供
    paginated: bool = False
    params: tuple[ParamSpec, ...] = ()

    @property
    def nexent_name(self) -> str:
        return TOOL_PREFIX + self.name

    @property
    def requires_ctx(self) -> bool:
        """写入、提议与后台任务都要求 ctx，缺失即失败关闭。"""
        return self.mode != "read"

    @model_validator(mode="after")
    def _ctx(self) -> ToolSpec:
        if self.requires_ctx and not self.accepts_ctx:
            raise ValueError("非只读工具必须接受 ctx")
        names = {p.name for p in self.params}
        if self.paginated and not {"cursor", "page_size"} <= names:
            raise ValueError("分页工具必须有 cursor 与 page_size 参数")
        return self


# ---------------------------------------------------------------- REST


T = TypeVar("T")


class Page(Record, Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None = None
    as_of: UtcDatetime | None = None
    version_manifest_id: str | None = None


class ErrorResponse(Record):
    error: ToolError
    trace_id: str | None = None


class HealthResponse(Record):
    status: Literal["ok", "degraded"]
    profile: Literal["mock", "dev", "demo", "prod", "evaluation"]
    service_version: str
    contract_version: str
    # ok 已连通或真实实现可用；disabled 配置关闭；unavailable 检查失败；unchecked 已启用但未检查；
    # placeholder 骨架占位、无业务；fake 由测试替身顶替
    dependencies: dict[str, Literal["ok", "disabled", "unavailable", "unchecked", "placeholder", "fake"]] = Field(default_factory=dict)


class AgentRunRequest(Record):
    """POST /agent/run：网关登记会话、注入 ctx 与 document_paths 后调 Nexent 北向。"""

    agent_name: Identifier
    query: Annotated[str, Field(min_length=1, max_length=8000)]
    conversation_id: str | None = None
    as_of: UtcDatetime | None = None  # 缺省取当前回放运行的时钟
    run_id: str | None = None


class AgentRunAccepted(Record):
    agent_run_id: Identifier
    trace_id: Identifier
    stream: Literal["text/event-stream"] = "text/event-stream"


class AgentRunStopped(Record):
    agent_run_id: Identifier
    status: Literal["stopping", "already_finished"]


class NexentRunPayload(Record):
    """网关 → Nexent 北向 POST /nb/v1/chat/run 的请求体（v2.8.2 探针实测字段）。

    tool_params 形如 {"agents": {agent: {"tools": {tool: {param: value}}}}}，只给白名单工具注入 ctx，
    知识库检索工具注入 document_paths。这是唯一允许携带注入值的契约，只在网关进程内构造、不落日志。
    repr/str 与校验报错已脱敏；model_dump 仍含原值（发给 Nexent 需要），只能在出站请求处调用。
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    agent_name: Identifier
    query: Annotated[str, Field(min_length=1)]
    conversation_id: str | None = None
    tool_params: dict[str, dict[str, dict[str, dict[str, dict[str, Any]]]]] | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _shape(self) -> NexentRunPayload:
        if self.tool_params is not None and set(self.tool_params) != {"agents"}:
            raise ValueError("tool_params 顶层只有 agents")
        return self

    def __repr__(self) -> str:  # 防止注入值进日志
        return f"NexentRunPayload(agent_name={self.agent_name!r}, tool_params=<redacted>)"

    __str__ = __repr__


class DocumentIngestRequest(Record):
    filename: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=1048576)
    expert: Literal["elevator", "hvac", "energy", "equipment"]
    source_level: Literal["national_standard", "equipment_manual", "experience_record"] = "experience_record"
    nature: DataNature = DataNature.synthetic
    document_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    entity_ids: tuple[EntityId, ...] = ()
    idempotency_key: str = Field(min_length=1, max_length=128)


class DocumentIngestReceipt(Record):
    document_id: str
    asset_version_id: AssetVersionId
    content_hash: ContentHash
    parent_version_id: AssetVersionId | None = None
    expert: str
    nature: DataNature
    available_at: UtcDatetime
    status: str
    candidate_ids: tuple[str, ...] = ()
    ontology_candidate_ids: tuple[str, ...] = ()
    entity_ids: tuple[EntityId, ...] = ()
    deduplicated: bool = False
    published: bool = False
    note: str = ""
    received_at: UtcDatetime | None = None
    source_name: str = ""
    source_level: str = "experience_record"
    action_ids: tuple[str, ...] = ()
    manifest_ids: tuple[str, ...] = ()
    reviews: dict[str, str] = Field(default_factory=dict)
    backtests: dict[str, str] = Field(default_factory=dict)


class DocumentSearchHit(Record):
    document_id: str
    asset_version_id: AssetVersionId
    locator: Locator
    text: str
    score: float
    expert: str


class RouteSpec(Record):
    method: Literal["GET", "POST"]
    path: str
    summary: str
    provider: str  # B 编号，可多个，用 / 分隔
    response: str  # 契约模型名；Page[...] 表示分页
    request: str | None = None
    query: tuple[str, ...] = ()
    required_query: tuple[str, ...] = ()  # 必填查询参数（只追加）；build.py 据此生成 required:true
    role: Literal["public", "viewer", "reviewer"] = "viewer"
    write: bool = False
    stream: bool = False

    @model_validator(mode="after")
    def _required_query(self) -> RouteSpec:
        missing = [q for q in self.required_query if q not in self.query]
        if missing:
            raise ValueError(f"必填查询参数必须同时出现在 query：{missing}")
        return self


# REST 全集：直接 REST 以只读为主，写入限于统一确认、网关和受控资料入库。
API_ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec(method="GET", path="/watchdog/health", summary="Watchdog 巡检状态", provider="B42", response="WatchdogHealth"),
    RouteSpec(method="GET", path="/watchdog/blocked", summary="Watchdog 受阻事件单", provider="B42", response="WatchdogBlocked"),
    RouteSpec(method="GET", path="/watchdog/explain", summary="Watchdog 故障原因查询", provider="B42", response="WatchdogAnswer", query=("question",)),
    RouteSpec(method="GET", path="/demo/day/{run_id}", summary="只读导出的一天", provider="B34", response="DayRecord"),
    RouteSpec(method="GET", path="/health", summary="服务模式与版本，不泄露 Key", provider="B02", response="HealthResponse", role="public"),
    RouteSpec(method="GET", path="/assets", summary="资产列表", provider="B06", response="Page[AssetVersion]", query=("cursor", "page_size", "asset_type")),
    RouteSpec(method="POST", path="/assets:ingest", summary="受控资料入库", provider="B26/B06", request="DocumentIngestRequest", response="DocumentIngestReceipt", role="reviewer", write=True),
    RouteSpec(method="GET", path="/knowledge/documents:search", summary="专家资料离线检索", provider="B26", response="Page[DocumentSearchHit]", query=("expert", "q", "as_of", "cursor", "page_size"), required_query=("expert", "q")),
    RouteSpec(method="GET", path="/knowledge/graph", summary="固定清单的已发布图谱", provider="B30", response="ToolResult", query=("manifest_id", "as_of", "cursor", "page_size"), required_query=("manifest_id",)),
    RouteSpec(method="GET", path="/assets/{asset_version_id}/ingestion", summary="资料入库回执", provider="B26", response="DocumentIngestReceipt", query=("as_of", "document_id")),
    RouteSpec(method="GET", path="/assets/{asset_version_id}", summary="资产详情", provider="B06", response="AssetVersion"),
    RouteSpec(method="GET", path="/assets/{asset_version_id}/fragments", summary="原文片段与定位", provider="B21/B26", response="Page[AssetFragment]", query=("cursor", "page_size", "as_of")),
    RouteSpec(method="GET", path="/runs", summary="运行列表", provider="B04", response="Page[RunInfo]", query=("cursor", "page_size")),
    RouteSpec(method="GET", path="/runs/{run_id}", summary="固定清单、状态与轨迹引用", provider="B04", response="RunInfo"),
    RouteSpec(method="GET", path="/runs/{run_id}/view", summary="只读投影，只改查看时点，不推进时钟", provider="B07/B16", response="StateSnapshot", query=("at",), required_query=("at",)),
    RouteSpec(method="GET", path="/runs/{run_id}/ledger", summary="独立账本", provider="B16", response="Page[Ledger]", query=("kind", "cursor", "page_size"), required_query=("kind",)),
    RouteSpec(method="GET", path="/plans", summary="计划列表", provider="B19", response="Page[Plan]", query=("run_id", "cursor", "page_size")),
    RouteSpec(method="GET", path="/plans/{plan_id}", summary="计划与动作", provider="B19", response="Plan", query=("revision",)),
    RouteSpec(method="GET", path="/plans/{plan_id}/explanation", summary="计划与动作的证据投影", provider="B19/B26", response="Page[EvidenceEvent]", query=("action_id", "cursor")),
    RouteSpec(method="GET", path="/baselines", summary="B0/B1/B2 与场景三组对照结果", provider="B33", response="Page[EvaluationReport]", query=("cursor", "page_size")),
    RouteSpec(method="GET", path="/evidence", summary="带 seq 的持久证据链；run/agent/action/trace 按 SQL 过滤，事件按 as_of 可见性过滤", provider="B04/B26", response="EvidencePage", query=("run_id", "agent_run_id", "cursor", "page_size", "action_id", "trace_id")),
    RouteSpec(method="GET", path="/reviews", summary="复核队列", provider="B24", response="Page[PendingAction]", query=("cursor", "page_size")),
    RouteSpec(method="GET", path="/review-metrics", summary="复核量-F1 与复核量-费用误差", provider="B24/B31", response="MetricSeries", query=("dataset",)),
    RouteSpec(method="GET", path="/versions", summary="版本清单", provider="B04", response="Page[VersionManifest]", query=("cursor", "page_size")),
    RouteSpec(method="GET", path="/evolution/timeline", summary="纠错进化与版本发布时间线", provider="B32", response="Page[EvolutionTimelineEntry]", query=("cursor", "page_size")),
    RouteSpec(method="GET", path="/versions/diff", summary="版本差异", provider="B27", response="VersionDiff", query=("from", "to")),
    RouteSpec(method="GET", path="/versions/impact", summary="受影响计划与规则", provider="B27", response="VersionDiff", query=("from", "to")),
    RouteSpec(method="GET", path="/actions", summary="待确认动作", provider="B04", response="Page[PendingAction]", query=("status", "kind", "cursor", "page_size")),
    RouteSpec(method="GET", path="/actions/{action_id}", summary="待确认动作及来源运行", provider="B04", response="PendingAction"),
    RouteSpec(method="POST", path="/actions/{action_id}/confirm", summary="统一确认：复核结论、计划确认、发布、回滚", provider="B04", request="ActionDecisionRequest", response="PendingAction", role="reviewer", write=True),
    RouteSpec(method="POST", path="/actions/{action_id}/reject", summary="统一拒绝", provider="B04", request="ActionDecisionRequest", response="PendingAction", role="reviewer", write=True),
    RouteSpec(method="GET", path="/jobs/{job_id}", summary="后台任务状态，wait ≤ 40 秒", provider="B04", response="Job", query=("wait",)),
    RouteSpec(method="POST", path="/agent/run", summary="网关：登记会话、注入、调 Nexent 北向，返回合流 UIEvent 流", provider="B23", request="AgentRunRequest", response="UIEvent", role="viewer", write=True, stream=True),
    RouteSpec(method="POST", path="/agent/stop/{agent_run_id}", summary="网关：中止一次 Agent 运行", provider="B23", response="AgentRunStopped", role="viewer", write=True),
    RouteSpec(method="GET", path="/evolution/skills", summary="候选 Skill 列表与当前版本", provider="B43", response="Page[SkillSummary]", query=("cursor", "page_size")),
    RouteSpec(method="GET", path="/evolution/skills/{name}/history", summary="Skill 版本链（发布/回滚记录）", provider="B43", response="Page[SkillVersionRecord]"),
    # #245 人工干预（B47）：暂停自动执行、取消未生效动作、撤销已生效动作；写入与 actions 端点一样只认运维令牌
    RouteSpec(method="POST", path="/autonomy/pause", summary="暂停自动执行：非保护动作只提议，等人逐条批准", provider="B47", request="AutonomyPauseRequest", response="AutonomyStatus", role="reviewer", write=True),
    RouteSpec(method="POST", path="/autonomy/resume", summary="恢复自动执行；积压的待批准提议仍逐条批准", provider="B47", request="AutonomyResumeRequest", response="AutonomyStatus", role="reviewer", write=True),
    RouteSpec(method="GET", path="/autonomy/status", summary="暂停状态：谁、何时、为什么、积压几条", provider="B47", response="AutonomyStatus", query=("run_id",)),
    RouteSpec(method="GET", path="/autonomy/actions", summary="已下发动作及其阶段（待生效/生效中/已结束/已取消/已撤销）", provider="B47", response="Page[AppliedAction]", query=("run_id",)),
    RouteSpec(method="POST", path="/actions/{action_id}/cancel", summary="取消还没生效的已下发动作", provider="B47", request="ActionCancelRequest", response="ActionIntervention", role="reviewer", write=True),
    RouteSpec(method="POST", path="/actions/{action_id}/revoke", summary="撤销已生效的动作（提前结束到当前分钟）", provider="B47", request="ActionRevokeRequest", response="ActionIntervention", role="reviewer", write=True),
    RouteSpec(method="GET", path="/actions/{action_id}/revoke-preview", summary="撤销与保持两种情况的试算差", provider="B47", response="RevokePreview", query=("run_id",)),
)


class DataSourceRef(Record):
    """场景运行用到的数据来源与版本（v0.8）。"""

    source: Identifier  # 例：D05、HIPE
    dataset_version: Identifier  # 原始文件 SHA-256 等


class RunInfo(Record):
    run_id: Identifier
    plant_id: Identifier
    # scenario：v0.8 场景运行，由 B17 ScenarioExecutor 执行，B16 RecordExecutor 不接收
    kind: Literal["replay_day", "replay_week", "backtest", "evaluation", "scenario"]
    version_manifest_id: Identifier
    clock: Interval
    as_of: UtcDatetime
    status: Literal["created", "running", "finished", "failed"]
    forked_from: str | None = None  # 从过去时刻重试要新建运行（03 §2.3）
    forked_at: UtcDatetime | None = None  # v0.8：分叉的仿真时刻（任意分钟）
    created_at: UtcDatetime
    # v0.8：整日期末储能下界，建运行时定一次；分钟层、滚动重解与分叉子运行都沿用
    energy_end_target_kwh: float | None = Field(default=None, ge=0)
    data_sources: tuple[DataSourceRef, ...] = ()

    @model_validator(mode="after")
    def _fork(self) -> RunInfo:
        if self.forked_at is not None and self.forked_from is None:
            raise ValueError("forked_at 只用于分叉运行（须有 forked_from）")
        if self.forked_at is not None and not self.clock.start <= self.forked_at <= self.clock.end:
            raise ValueError("forked_at 必须落在运行时钟区间内")
        return self


class VersionDiff(Record):
    from_manifest_id: Identifier
    to_manifest_id: Identifier
    changed: dict[str, tuple[str, ...]]  # 组件 -> 变化的规则/本体/参数 ID
    affected_plan_ids: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()  # 无法判断影响的项，明确列出


class MetricSeries(Record):
    metric: Identifier
    dataset: Identifier
    split_id: Identifier
    points: tuple[tuple[float, float], ...]  # (复核量, 指标值)
    note: str | None = None


# ---------------------------------------------------------------- v0.8 专家提案、试算与场景（B46，03 §3.0～§3.6）


class RunBinding(Record):
    """一次专家或主 Agent 运行的绑定（03 §3.0 轮次与过期）。绑定不符的结果只入历史，不执行、不回写。"""

    run_id: Identifier
    knowledge_version: int = Field(ge=1)  # “知识 vN”：清单激活序号（SqliteVersionStore.knowledge_version）
    ticket_id: Identifier
    round: int = Field(ge=1)  # 事件单每派发一次 +1；提案就绪钩子只收当前轮
    state_version: int = Field(ge=0)
    trigger_at: UtcDatetime  # 本轮触发时刻：首轮为事件时刻，分叉重派为 forked_at，人工确认后为确认时刻


class AgentTiming(Record):
    """三个仿真时间与墙钟耗时（03 §3.0 时间协调）。仿真时间决定动作生效，墙钟只进报告。"""

    origin_event_at: UtcDatetime  # 原始事件时刻，只用于响应指标
    trigger_at: UtcDatetime
    effective_at: UtcDatetime  # = trigger_at + policy.response_budget
    requested_at: UtcDatetime  # 以下为墙钟
    returned_at: UtcDatetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    retries: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _order(self) -> AgentTiming:
        if self.trigger_at < self.origin_event_at:
            raise ValueError("trigger_at 不能早于 origin_event_at")
        if self.effective_at < self.trigger_at:
            raise ValueError("effective_at 不能早于 trigger_at")
        if self.returned_at is None and self.latency_ms is not None:
            raise ValueError("未返回的运行不能有 latency_ms")
        if self.returned_at is not None and self.returned_at < self.requested_at:
            raise ValueError("returned_at 不能早于 requested_at")
        return self


class ProposedAction(Record):
    """专家提案里的一个动作。"""

    entity_id: EntityId
    command: Identifier  # set_mode / set_setpoint / switch_circuit / broadcast / charge …
    params: dict[str, Any] = Field(default_factory=dict)
    interval: Interval
    readiness: ActionReadiness = ActionReadiness.ready
    depends_on: tuple[str, ...] = ()  # needs_diagnosis 时写明依赖的诊断项

    @model_validator(mode="after")
    def _readiness(self) -> ProposedAction:
        if self.readiness == "needs_diagnosis" and not self.depends_on:
            raise ValueError("needs_diagnosis 动作必须写明 depends_on")
        if self.readiness == "ready" and self.depends_on:
            raise ValueError("ready 动作不依赖诊断，depends_on 必须为空")
        return self


class ProposalCost(Record):
    """同一把尺子上的三项代价；cny 为负表示节省。"""

    cny: float
    wait_min: float = Field(default=0.0, ge=0)  # 候梯分钟
    comfort_min: float = Field(default=0.0, ge=0)  # 温度越界分钟


class ExpertProposal(Record):
    """专家提案（03 §3.0 提案以落库为准）。propose 工具返回 proposal_id 为空的草稿，
    B23 注册表包装层经 ProposalSink.save 落库分配 proposal_id 并补 binding 与 timing；
    diagnosis 与 evidence_refs 可由专家回答事后经 attach_reply 补充。"""

    proposal_id: str | None = None
    expert: Identifier  # energy / elevator / hvac / production …
    ticket_id: Identifier
    actions: tuple[ProposedAction, ...] = ()  # 可为空：专家认为无需动作
    cost: ProposalCost
    confidence: float = Field(ge=0, le=1)
    diagnosis: str | None = None
    evidence_refs: tuple[str, ...] = ()
    safety_tags: tuple[str, ...] = ()  # 来自全局图谱；第 1 层安全否决用
    binding: RunBinding | None = None
    timing: AgentTiming | None = None

    @model_validator(mode="after")
    def _binding(self) -> ExpertProposal:
        if self.binding is not None and self.binding.ticket_id != self.ticket_id:
            raise ValueError("binding.ticket_id 必须与提案 ticket_id 一致")
        if self.binding is not None and self.timing is not None and self.binding.trigger_at != self.timing.trigger_at:
            raise ValueError("binding.trigger_at 必须与 timing.trigger_at 一致")
        return self


class ProposalReceipt(Record):
    """propose 工具对模型的返回：只给 ID 与摘要，数值不经模型转述。"""

    proposal_id: Identifier
    summary: Annotated[str, Field(max_length=500)]


class ExpertReply(Record):
    """专家最终回答，由会诊模块（B29）解析后经 ProposalSink.attach_reply 挂到同一提案上。"""

    status: Literal["done", "waiting", "blocked", "needs_clarification", "parse_failed"]
    proposal_id: str | None = None
    diagnosis: str | None = None
    evidence_refs: tuple[str, ...] = ()
    question: str | None = None

    @model_validator(mode="after")
    def _question(self) -> ExpertReply:
        if self.status == "needs_clarification" and not self.question:
            raise ValueError("needs_clarification 必须给 question")
        return self


class SimAction(Record):
    """场景仿真里的设备动作（电梯、暖通、照明、广播等）；经 B17 apply，不走 Plan 与 B15。"""

    entity_id: EntityId
    command: Identifier
    params: dict[str, Any] = Field(default_factory=dict)
    interval: Interval


class TrialViolation(Record):
    kind: Literal["capacity", "hard_zone", "safety"]
    entity_id: str | None = None
    at: UtcDatetime
    detail: str


class TrialResult(Record):
    """TrialRunner.trial 的结果（03 §3.4 第 2 层）。硬约束违规即不可行，软代价不参与抵消。

    `violations` 只含相对“同一快照的空候选基线”新增或加重的违规；基线本来就有的违规记在
    `baseline_violations`，不参与本次可行性判断（#173 相对语义：既有违规下允许缓解动作）。
    """

    feasible: bool
    violations: tuple[TrialViolation, ...] = ()
    soft_cost: ProposalCost
    minute_ledger: tuple[LedgerEntry, ...] = ()
    baseline_violations: tuple[TrialViolation, ...] = ()
    baseline_soft_cost: ProposalCost | None = None
    # #201-5：候选试算后仍存在的既有违规分钟数（候选违规中与基线违规同 (kind, entity_id) 的 distinct at 数）；
    # 只追加的可选字段，旧实现与测试替身不给时按 0 处理，不改既有相对违规语义。
    remaining_violation_minutes: int | None = None

    @model_validator(mode="after")
    def _feasible(self) -> TrialResult:
        if self.feasible == bool(self.violations):
            raise ValueError("有新增硬约束违规时 feasible 必须为 False，无新增违规时必须为 True")
        return self


class ScenarioSlotSummary(Record):
    """场景运行的 15 分钟槽汇总（由 15 条分钟账本求和）。不复用 LedgerEntry，也不走 Ledger.recheck 的
    单条充放互斥：槽内可先充后放，物理校验只在分钟账本上做。"""

    interval: Interval
    charge_kwh: float = Field(ge=0)
    discharge_kwh: float = Field(ge=0)
    net_storage_kwh: float  # 充 − 放（交流侧）
    load_kwh: float = Field(ge=0)
    pv_available_kwh: float = Field(ge=0)
    pv_used_kwh: float = Field(ge=0)
    grid_import_kwh: float = Field(ge=0)
    grid_export_kwh: float = Field(ge=0)
    pv_curtailed_kwh: float = Field(ge=0)
    soc_end: float = Field(ge=0, le=1)
    cost_cny: float | None = None
    peak_minute_kw: float = Field(ge=0)  # 分钟购电功率最大值
    demand_15m_kw: float = Field(ge=0)  # 槽内购电电量 × 4
    minutes_balanced: bool  # 槽内每分钟都平衡
    capacity_violations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _sums(self) -> ScenarioSlotSummary:
        tol = LEDGER_TOLERANCE["energy_abs_kwh"]
        if abs(self.net_storage_kwh - (self.charge_kwh - self.discharge_kwh)) > tol:
            raise ValueError("net_storage_kwh 必须等于 charge_kwh − discharge_kwh")
        if abs(self.pv_used_kwh + self.pv_curtailed_kwh - self.pv_available_kwh) > tol:
            raise ValueError("光伏使用 + 弃光 必须等于光伏可发")
        return self


class ResponseBudget(Record):
    """仿真响应预算（仿真分钟），取实测墙钟 p95 向上取整。"""

    layer12_min: int = Field(ge=0)  # 第 1、2 层（专家确定性提案 + 加权优化）
    layer3_min: int = Field(ge=0)  # 第 3 层主 Agent 裁决


class PolicyConfig(Record):
    """版本清单 policy 组件的内容（03 §3.4、§3.5）：带版本、可进化。"""

    policy_version: Identifier
    arbitration_weights: dict[str, float]  # 第 2 层：cny / wait_min / comfort_min → 权重
    priority_profiles: dict[str, tuple[str, ...]]  # 第 3 层：场景或时段 → 优先级顺序，安全固定第一
    confidence_low: float = Field(default=0.3, ge=0, le=1)  # 低于此值不执行、转人工纠错
    confidence_high: float = Field(default=0.7, ge=0, le=1)  # [low, high) 标“待抽查”
    sampling_ratio: float = Field(default=0.2, ge=0, le=1)
    response_budget: ResponseBudget
    # v0.8（D55）：重大损失金额线；带版本、可进化，只追加字段。None 表示不启用这条线
    major_loss_cny: float | None = Field(default=None, ge=0)
    # v0.8（B29）：预演（提前行动）的检查窗口与触发阈值；带版本、可进化，只追加字段
    preview_horizon_min: int = Field(default=30, ge=1)  # 只检查未来这么多分钟内可预见的冲突
    preview_soc_low: float = Field(default=0.3, ge=0, le=1)  # 储能 SOC 低于此值才值得预演削峰
    preview_transformer_high: float = Field(default=0.85, ge=0, le=1)  # 变压器负载率高于此值才算冲突
    # v0.8（B47 D53）：自主等级表；键为“事件类型”或“事件类型|设备类型”，缺省按是否见过给默认等级。
    # 默认 L2 指见过的类别；从未见过的类别从 L0 起步（人批准、抽查无误后由 B32 升级）。只追加字段。
    autonomy_levels: dict[str, Literal["L0", "L1", "L2", "L3"]] = Field(default_factory=dict)
    autonomy_default_level: Literal["L0", "L1", "L2", "L3"] = "L2"
    autonomy_unseen_level: Literal["L0", "L1", "L2", "L3"] = "L0"
    # v0.8（B47）：人工待办的处理时限（仿真分钟）；到期未处理不执行、事件单标受阻。只追加字段。
    human_deadline_min: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _rules(self) -> PolicyConfig:
        if self.confidence_low >= self.confidence_high:
            raise ValueError("confidence_low 必须小于 confidence_high")
        bad = [k for k, v in self.priority_profiles.items() if not v or v[0] != "safety"]
        if bad:
            raise ValueError(f"优先级配置必须以 safety 开头：{bad}")
        return self


# ---- “一天”数据格式（contracts/day.schema.json）：开发工作台实时流与回放展示页共用


class DayEvent(Record):
    event_id: Identifier
    at: UtcDatetime
    event_type: Identifier
    source: Literal["script", "random", "sim_limit", "operator_message"]
    entity_id: str | None = None
    severity: int = Field(default=1, ge=0, le=5)
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    nature: DataNature


class TicketSubscription(Record):
    entity_id: Identifier  # 图谱实体，也可以是事件单或工单本身
    event_types: tuple[str, ...] = Field(min_length=1)


class StatusChange(Record):
    """一次状态迁移（#217-3）：只在状态变化时追加，不按分钟写。"""

    status: str
    at: UtcDatetime


class Ticket(Record):
    ticket_id: Identifier
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None
    status: Literal["open", "waiting", "blocked", "resolved", "closed"]  # blocked = 受阻
    rehearsal: bool = False  # 预演事件单
    round: int = Field(ge=0)
    event_ids: tuple[str, ...] = ()
    experts: tuple[str, ...] = ()
    subscriptions: tuple[TicketSubscription, ...] = ()
    summary: str = ""
    # v0.8（#217-3 / #238）：状态迁移历史，导出时按当日结束取 status；原样带出，不过滤、不截断
    status_history: tuple[StatusChange, ...] = ()


class SeverityEstimate(Record):
    """D55 严重程度估算：不采取保护动作的预期损失与是否停产。金额只含停产产值与设备损坏估值。"""

    loss_cny: float = Field(ge=0)
    production_stop: bool
    horizon_min: int = Field(ge=1)
    basis: str  # 估算依据：停产分钟、受影响产线、设备类型、读数是否可疑
    field_check: bool = False  # 关键读数可疑（残差报警/质量非 ok）：工单写“请先现场核实”，保护动作交 Agent 决定


class ProbeProposal(Record):
    """D61 试探提案：慢通道调查给出一个只作用于观察窗口的试探动作，及待检验假设与预期读数反应。"""

    ticket_id: Identifier
    round: int = Field(ge=1)
    actions: tuple[ProposedAction, ...] = Field(min_length=1)
    window_min: int = Field(default=3, ge=1)
    hypothesis: str
    expectations: tuple[str, ...] = ()
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _window(self) -> ProbeProposal:
        shortest = min((a.interval.end - a.interval.start).total_seconds() // 60 for a in self.actions)
        if shortest < self.window_min:
            raise ValueError("试探动作的生效区间不能短于观察窗口")
        return self



class DecisionConflict(Record):
    summary: str
    proposal_ids: tuple[str, ...]


class DecisionConversion(Record):
    """换算：把各提案（或组合）放到同一把尺子上。"""

    policy_version: Identifier
    scores: dict[str, float]  # 提案或组合 ID → 加权代价
    infeasible: tuple[str, ...] = ()  # 被 TrialRunner 排除的组合
    notes: tuple[str, ...] = ()


class DecisionResult(Record):
    selected_proposal_ids: tuple[str, ...]
    outcome: Literal["executed", "work_order", "to_human", "sampling", "blocked"]
    summary: str


class DeliveryOutcome(Record):
    """B47 执行回执（#168）：B28 只依赖本契约映射 DayDecision.result.outcome，不 import 分级执行。

    排队 / 工单 → work_order，infeasible / stale → blocked，全部执行 → executed；
    correction 是低置信转人工纠错。action_indexes 是本次提案里对应的动作序号。
    """

    outcome: Literal["executed", "queued_work_order", "queued_human", "blocked_infeasible", "stale", "correction"]
    reason: str = ""
    work_order_ids: tuple[str, ...] = ()
    action_indexes: tuple[int, ...] = ()


class DayDecision(Record):
    """裁决“冲突 → 换算 → 结果”三步（03 §3.6）。"""

    decision_id: Identifier
    ticket_id: Identifier
    round: int = Field(ge=1)
    layer: Literal[1, 2, 3]
    at: UtcDatetime
    conflict: DecisionConflict
    conversion: DecisionConversion
    result: DecisionResult


class WorkOrder(Record):
    work_order_id: Identifier
    ticket_id: Identifier
    created_at: UtcDatetime
    reason: str
    actions: tuple[ProposedAction, ...] = ()
    status: Literal["proposed", "confirmed", "rejected", "expired", "done"]
    pending_action_id: str | None = None  # 经统一确认端点（PendingActionKind.work_order）
    # v0.8（#217-3 / #238）：状态迁移历史，导出时按当日结束取 status；原样带出，不过滤、不截断
    status_history: tuple[StatusChange, ...] = ()


class TempRule(Record):
    """临时指令转成的临时规则（03 §3.6），到期自动失效，不进长期知识。"""

    rule_id: Identifier
    text: str
    effective: Interval
    entity_ids: tuple[str, ...] = ()
    created_at: UtcDatetime
    source_event_id: str | None = None
    estimated_cost_cny: float | None = None


class KnowledgeVersionEntry(Record):
    version: int = Field(ge=1)  # 知识 vN
    version_manifest_id: Identifier
    activated_at: UtcDatetime
    summary: str = ""


class QaEntry(Record):
    qa_id: Identifier
    at: UtcDatetime
    question: str
    answer: str
    agent_run_id: str | None = None
    evidence_refs: tuple[str, ...] = ()


class AgentRunNode(Record):
    """一次运行里的一个 Agent 节点（B23）：主 Agent 为 depth 0；子 Agent 按 Nexent SSE 的 invocation_id 区分。"""

    node_id: Identifier  # 主 Agent 为 "root"，子 Agent 为 invocation_id
    agent_name: Identifier
    depth: int = Field(ge=0)
    parent_id: str | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    final_answer: str | None = None


class AgentToolCall(Record):
    """一次 plantkg 工具调用（B23）：以 plantkg 自己的调用记录为准，不取 SSE 里模型填的参数。"""

    call_id: Identifier
    agent_name: Identifier  # 发起调用的 Agent（按注入的 ctx 归属）
    depth: int = Field(default=0, ge=0)
    tool: Identifier
    model_params: dict[str, Any] = Field(default_factory=dict)  # 模型填值（不含注入参数）
    bound_params: dict[str, Any] = Field(default_factory=dict)  # 系统绑定值：会话给出的厂区、身份、as_of、运行
    started_at: UtcDatetime
    duration_ms: int = Field(ge=0)
    business_status: str
    summary: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def _no_echo(self) -> AgentToolCall:
        _reject_injected(self.model_params, "model_params")
        _reject_injected(self.bound_params, "bound_params")
        return self


class AgentRunRecord(Record):
    """一次专家或主 Agent 运行（live 实调、缓存或确定性实现）的记录。

    B23 扩展：live 运行另记模型、Agent 节点与 depth、工具调用（模型填值 → 系统绑定值）与最终回答，
    供展示页“查看 Nexent 运行记录”使用；这些字段都可选，缓存与确定性记录可以不填。
    工作台的问答（kind=follow_up）不属于事件单，binding 可以为空。"""

    agent_run_id: Identifier
    kind: AgentResultKind
    agent_name: Identifier
    run_mode: RunMode
    source: Literal["nexent", "cache", "deterministic"]
    binding: RunBinding | None
    timing: AgentTiming
    status: Literal["finished", "failed", "timeout", "blocked", "cancelled"]
    coverage_gap: bool = False  # 缓存缺失、退化为确定性实现（覆盖不足）
    proposal_ids: tuple[str, ...] = ()
    model: str | None = None
    conversation_id: str | None = None
    query: str | None = None
    terminal_reason: TerminalReason | None = None
    nodes: tuple[AgentRunNode, ...] = ()
    tool_calls: tuple[AgentToolCall, ...] = ()
    final_answer: str | None = None

    @model_validator(mode="after")
    def _binding_required(self) -> AgentRunRecord:
        if self.binding is None and self.kind != "follow_up":
            raise ValueError("只有工作台问答（follow_up）可以没有事件单绑定")
        return self


class AssetStateCounts(Record):
    dormant: int = Field(ge=0)
    identified: int = Field(ge=0)
    in_graph: int = Field(ge=0)
    referenced: int = Field(ge=0)


class DayMetric(Record):
    metric: Identifier
    group: Literal["baseline", "independent", "coordinated", "knowledge", "config"]  # config = 配置限值，不参与改善率
    value: float | None
    unit: str | None = None
    note: str | None = None
    # v0.8（#238）：config 组填 configured；只追加，旧导出不带也不影响
    nature: DataNature | None = None
    # #235：版本/批次维度（基线、修复后、批次1…），不再写成指标名的 @ 后缀；(group, metric, variant) 在一份日导出里唯一
    variant: str | None = None
    counts: AssetStateCounts | None = None
    as_of: UtcDatetime | None = None


class RunChainEntry(Record):
    """运行接续链：展示页离线重建分叉。"""

    run_id: Identifier
    forked_from: str | None = None
    forked_at: UtcDatetime | None = None
    knowledge_version: int | None = Field(default=None, ge=1)
    data_sources: tuple[DataSourceRef, ...] = ()


class StateChange(Record):
    at: UtcDatetime
    entity_id: Identifier
    field: Identifier  # soc / temperature / humidity / running / fault / queue …
    value: Any


class StateFrame(Record):
    """每 15 分钟一帧全量设备状态，加上本帧到下一帧之间按分钟的变化。"""

    at: UtcDatetime
    devices: dict[str, dict[str, Any]]  # 实体 ID → 全量状态
    changes: tuple[StateChange, ...] = ()

    @model_validator(mode="after")
    def _window(self) -> StateFrame:
        end = self.at + timedelta(minutes=15)
        if any(not self.at <= ch.at < end for ch in self.changes):
            raise ValueError("帧间变化必须落在 [at, at+15min)")
        return self


class DayContext(Record):
    """“一天”的跨日引用闭包（#217-1、#217-2 / #238）。

    被当日记录引用、但发生在当日以外的记录，原样携带，不进当日时间轴和计数。
    展示页按 ID 查找时先查当日小节、再查这里。
    """

    proposals: tuple[ExpertProposal, ...] = ()
    events: tuple[DayEvent, ...] = ()
    tickets: tuple[Ticket, ...] = ()


class DayRecord(Record):
    """“一天”数据（day.json）。开发工作台实时流与回放展示页共用；展示页只靠它离线重建状态。

    v0.8（B49）：可选 `cache_version` 给评测缓存键的数据版本（无生产者时留空，不伪造）；
    `evidence` 是本次运行链的证据事件，`assets` 只放被证据引用到的资产条目（B49 资料页用）。
    v0.8（#238）：`context` 携带被当日记录引用的跨日记录（默认空）。
    """

    schema_version: Literal["1"] = "1"
    plant_id: Identifier
    local_date: LocalDate
    timezone: Identifier
    run_id: Identifier  # 展示的主运行（接续链末端）
    runs: tuple[RunChainEntry, ...] = Field(min_length=1)
    events: tuple[DayEvent, ...] = ()
    tickets: tuple[Ticket, ...] = ()
    proposals: tuple[ExpertProposal, ...] = ()
    decisions: tuple[DayDecision, ...] = ()
    work_orders: tuple[WorkOrder, ...] = ()
    context: DayContext = Field(default_factory=DayContext)
    temp_rules: tuple[TempRule, ...] = ()
    knowledge_versions: tuple[KnowledgeVersionEntry, ...] = ()
    qa: tuple[QaEntry, ...] = ()
    agent_runs: tuple[AgentRunRecord, ...] = ()
    metrics: tuple[DayMetric, ...] = ()
    state_series: tuple[StateFrame, ...] = ()
    ledger_minutes: tuple[LedgerEntry, ...] = ()
    cache_version: str | None = None
    assets: tuple[AssetVersion, ...] = ()
    evidence: tuple[EvidenceEvent, ...] = ()

    @model_validator(mode="after")
    def _refs(self) -> DayRecord:
        runs = {r.run_id for r in self.runs}
        if self.run_id not in runs:
            raise ValueError("run_id 必须在 runs 接续链里")
        # 链首可以是从前一天的运行分叉来的；其余分叉必须指向链内运行
        if any(r.forked_from is not None and r.forked_from not in runs for r in self.runs[1:]):
            raise ValueError("runs 的 forked_from 必须指向链内运行（链首除外）")
        tickets = {t.ticket_id for t in self.tickets}
        if any(p.proposal_id is None for p in self.proposals):
            raise ValueError("day.json 里的提案都已落库，必须有 proposal_id")
        dangling = sorted({x.ticket_id for x in (*self.proposals, *self.decisions, *self.work_orders)} - tickets)
        if dangling:
            raise ValueError(f"引用了不存在的事件单：{dangling}")
        if [f.at for f in self.state_series] != sorted(f.at for f in self.state_series):
            raise ValueError("state_series 必须按时间排序")
        keys = [(m.group, m.metric, m.variant) for m in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("metrics 的 (group, metric, variant) 必须唯一")
        return self


# ---------------------------------------------------------------- #245 人工干预（B47）


class AutonomyPauseRequest(Record):
    """POST /autonomy/pause：run_id 缺省是整厂（live 值守），给了就只暂停该场景运行。"""

    reason: str = Field(min_length=1, max_length=500)
    run_id: Identifier | None = None


class AutonomyResumeRequest(Record):
    run_id: Identifier | None = None


class AutonomyStatus(Record):
    """GET /autonomy/status：paused 为 False 时 by/since/reason 是上一次暂停的信息（没暂停过则为空）。"""

    paused: bool
    scope: Literal["plant", "run"]
    run_id: str | None = None
    by: str | None = None
    since: UtcDatetime | None = None
    reason: str | None = None
    resumed_by: str | None = None
    resumed_at: UtcDatetime | None = None
    backlog: int = Field(default=0, ge=0)  # 暂停以来积压、仍待批准的提议条数


class AppliedAction(Record):
    """已下发到仿真/执行层的动作；action_id 是取消、撤销与预计影响的对象。"""

    action_id: Identifier
    run_id: Identifier
    entity_id: Identifier
    command: Identifier
    params: dict[str, Any] = Field(default_factory=dict)
    interval: Interval
    stage: Literal["scheduled", "active", "ended", "cancelled", "revoked"]
    protection: bool = False  # 来自预案 protection 模板的保护动作
    ticket_id: str | None = None
    proposal_id: str | None = None
    temp_rule_id: str | None = None


class ActionCancelRequest(Record):
    reason: str = Field(min_length=1, max_length=500)
    confirm_protection: bool = False  # 取消还没生效的保护动作时的二次确认
    run_id: Identifier | None = None


class ActionRevokeRequest(Record):
    """撤销已生效动作；system_wrong 计入人工纠错，other 只记录。保护动作必须填 note 并带 confirm_protection。"""

    reason_kind: Literal["system_wrong", "other"]
    note: str = Field(default="", max_length=500)
    confirm_protection: bool = False
    run_id: Identifier | None = None


class ActionIntervention(Record):
    """取消/撤销的记录；重复请求返回同一条。"""

    action_id: Identifier
    run_id: Identifier
    kind: Literal["cancelled", "revoked"]
    operator: str
    at: UtcDatetime
    reason_kind: Literal["system_wrong", "other"] | None = None
    note: str = ""
    protection: bool = False
    effective_end: UtcDatetime | None = None  # 撤销后的生效结束时刻
    original_end: UtcDatetime | None = None


class RevokeOutcome(Record):
    """撤销预计影响里的一种情况在剩余窗口内的试算结果。"""

    cost_cny: float
    violation_minutes: int = Field(ge=0)
    t1_peak_kw: float = Field(ge=0)  # 剩余窗口内变压器 T1 进线口径的分钟峰值


class RevokePreview(Record):
    action_id: Identifier
    run_id: Identifier
    window: Interval  # 试算的剩余窗口：当前分钟到动作原结束时刻
    revoke: RevokeOutcome
    keep: RevokeOutcome
    cost_delta_cny: float  # 撤销 - 保持
    violation_minutes_delta: int
    t1_peak_delta_kw: float
    protection: bool = False
    note: str = ""


# ---------------------------------------------------------------- 导出清单


def record_models() -> dict[str, type[Record]]:
    """全部可序列化契约记录（供 contracts/build.py 生成 schema 与测试遍历）。"""
    found: dict[str, type[Record]] = {}
    for name, value in globals().items():
        if isinstance(value, type) and issubclass(value, Record) and value is not Record and value is not Page:
            found[name] = value
    return dict(sorted(found.items()))


def enum_types() -> dict[str, type[Enum]]:
    found = {
        name: value
        for name, value in globals().items()
        if isinstance(value, type) and issubclass(value, Enum) and value.__module__ == __name__
    }
    return dict(sorted(found.items()))


_ROUTE_PARAM = re.compile(r"\{([a-z_]+)\}")


def route_path_params(route: RouteSpec) -> tuple[str, ...]:
    return tuple(_ROUTE_PARAM.findall(route.path))


# ---------------------------------------------------------------- MCP 工具全集（04「MCP 工具全集与实现归属」）


def _p(name: str, type_: str, description: str, required: bool = False) -> ParamSpec:
    return ParamSpec(name=name, type=type_, required=required, description=description)


_CURSOR = _p("cursor", "string", "分页游标；首次调用留空")
_PAGE_SIZE = ParamSpec(name="page_size", type="integer", description="每页条数，1～50", minimum=1, maximum=50)

TOOL_SPECS: tuple[ToolSpec, ...] = (
    # 资产与本体
    ToolSpec(name="ingest", plane="knowledge", provider="B06", extended_by=("B21",), mode="write",
             description="接入一份资料，登记来源、版本与数据性质；接入不批准任何事实",
             params=(_p("source_uri", "string", "资料位置", True), _p("asset_type", "string", "policy/process_doc/datasheet/image/table", True),
                     _p("available_at", "string", "资料可见时间 ISO-8601 UTC", True))),
    ToolSpec(name="list_assets", plane="knowledge", provider="B06", mode="read", paginated=True,
             description="列出当前 as_of 可见的资料版本（政策、手册等），不含运行简报和实时读数",
             params=(_p("asset_type", "string", "按类型过滤"), _CURSOR, _PAGE_SIZE)),
    ToolSpec(name="get_asset", plane="knowledge", provider="B06", extended_by=("B21",), mode="read",
             description="读取一份资料版本的元数据与片段定位",
             params=(_p("asset_version_id", "string", "asset:// 标识", True),)),
    ToolSpec(name="propose_candidates", plane="knowledge", provider="B22", mode="write",
             description="为对象生成四层本体候选（概念、关系、约束、实例）",
             params=(_p("subject_ids", "array", "对象 ID 列表", True), _p("layer", "string", "concept/relation/constraint/instance", True))),
    ToolSpec(name="judge_candidates", plane="knowledge", provider="B23", mode="write",
             description="用判别后端对候选打分并做一致性门控；score/prob/coverage 分开返回",
             params=(_p("candidate_ids", "array", "候选 ID 列表", True),)),
    ToolSpec(name="link_entities", plane="knowledge", provider="B22", mode="write",
             description="建立关系或别名归一，生成待门控候选",
             params=(_p("subject_id", "string", "主体", True), _p("target_id", "string", "客体", True), _p("relation", "string", "关系类型", True))),
    ToolSpec(name="validate", plane="knowledge", provider="B22", mode="read",
             description="对指定命名图做 SHACL 结构校验，只检查结构",
             params=(_p("graph", "string", "命名图名称；留空为当前清单"),)),
    ToolSpec(name="snapshot", plane="knowledge", provider="B22", mode="read",
             description="返回当前清单下本体快照摘要"),
    ToolSpec(name="diff", plane="knowledge", provider="B22", extended_by=("B27",), mode="read",
             description="比较两个版本的本体与规则差异",
             params=(_p("from_version", "string", "旧清单 ID", True), _p("to_version", "string", "新清单 ID", True))),
    ToolSpec(name="graph_query", plane="knowledge", provider="B22", extended_by=("B26",), mode="read", paginated=True,
             description="按白名单查询 ID 读取固定可见图；不接受任意 SPARQL",
             params=(_p("query_id", "string", "白名单查询 ID", True), _p("params", "object", "查询参数"), _CURSOR, _PAGE_SIZE)),
    ToolSpec(name="gate_and_queue", plane="knowledge", provider="B24", mode="write", exposed_to_agent=False,
             description="服务端策略：按不确定性 × 影响面把低置信条目排进复核队列",
             params=(_p("candidate_ids", "array", "候选 ID 列表", True),)),
    ToolSpec(name="next_review_batch", plane="knowledge", provider="B24", mode="read", paginated=True,
             description="取出待复核条目（按决策感知影响面排序）",
             params=(_CURSOR, _PAGE_SIZE)),
    ToolSpec(name="propose_review_decision", plane="knowledge", provider="B24", mode="propose",
             description="为复核项提出建议结论，只生成待确认动作，由复核员在工作台确认",
             params=(_p("review_id", "string", "复核项 ID", True), _p("decision", "string", "accept/revise/reject", True),
                     _p("revision", "object", "修订内容"), _p("rationale", "string", "依据说明", True))),
    ToolSpec(name="extract_rules", plane="knowledge", provider="B25", mode="write",
             description="从资料片段抽取候选规则；不能跳过审核",
             params=(_p("asset_version_id", "string", "资料版本", True), _p("rule_kind", "string", "限定规则类型"))),
    ToolSpec(name="lookup_rules", plane="knowledge", provider="B26", mode="read", paginated=True,
             description="查询对当前 as_of 可见且适用的已发布规则",
             params=(_p("subject_id", "string", "适用对象"), _p("kind", "string", "规则类型"), _CURSOR, _PAGE_SIZE)),
    ToolSpec(name="retrieve_evidence", plane="knowledge", provider="B26", mode="read", optional=True,
             description="图谱缺边时检索原文；新事实只作候选",
             params=(_p("question", "string", "检索问题", True),)),
    ToolSpec(name="workorder_search", plane="knowledge", provider="B26", mode="read", paginated=True,
             description="按设备、故障现象与时间段查询历史工单原文与确定性聚类；只读",
             params=(_p("equipment", "string", "设备编号，如 AC-2"), _p("phenomenon", "string", "故障现象关键字"),
                     _p("from_time", "string", "起始时间 ISO-8601 UTC"), _p("to_time", "string", "结束时间 ISO-8601 UTC"),
                     _CURSOR, _PAGE_SIZE)),
    # 决策
    ToolSpec(name="run_day", plane="decision", provider="B19", mode="job",
             description="生成一份日前计划（确定性数值用例）；40 秒内未完成返回 pending 与 job_id，用同一 job_id 调 get_job",
             params=(_p("plan_date", "string", "厂区当地日期 YYYY-MM-DD", True), _p("policy", "string", "B0/B1/B2，默认 B2"))),
    ToolSpec(name="get_plan", plane="decision", provider="B19", mode="read",
             description="读取计划与动作摘要",
             params=(_p("plan_id", "string", "计划 ID", True), _p("revision", "integer", "修订号；留空取最新"))),
    ToolSpec(name="explain", plane="decision", provider="B19", extended_by=("B26",), mode="read", paginated=True, optional=True,
             description="返回计划或单个动作依据的规则、原文定位、版本与可见时间",
             params=(_p("plan_id", "string", "计划 ID", True), _p("action_id", "string", "动作 ID"), _CURSOR, _PAGE_SIZE)),
    ToolSpec(name="propose_plan_confirmation", plane="decision", provider="B15", mode="propose",
             description="提出计划确认，只生成待确认动作",
             params=(_p("plan_id", "string", "计划 ID", True), _p("expected_revision", "integer", "预期修订号", True),
                     _p("rationale", "string", "依据说明", True))),
    # 影响与进化
    ToolSpec(name="impact_of_change", plane="knowledge", provider="B27", mode="read", optional=True,
             description="列出两版之间的变更、受影响计划与未知项；不覆盖旧运行",
             params=(_p("from_version", "string", "旧清单 ID", True), _p("to_version", "string", "新清单 ID", True))),
    ToolSpec(name="propose_candidates_evo", plane="evolution", provider="B18", extended_by=("B30", "B42"), mode="write", optional=True,
             description="由执行偏差或知识变更提出进化候选",
             params=(_p("kind", "string", "parameter/knowledge/skill", True), _p("trigger", "object", "触发依据", True))),
    ToolSpec(name="backtest", plane="evolution", provider="B18", mode="job", optional=True,
             description="在冻结验证日上回测候选；走后台任务",
             params=(_p("candidate_id", "string", "候选 ID", True),
                     _p("scenarios", "array", "可选场景列表（scenario_id、seed、path）；缺省用默认场景集"))),
    ToolSpec(name="propose_publish", plane="evolution", provider="B18", mode="propose",
             description="提出发布已验证候选，只生成待确认动作",
             params=(_p("candidate_id", "string", "候选 ID", True), _p("rationale", "string", "依据说明", True))),
    ToolSpec(name="propose_rollback", plane="evolution", provider="B18", mode="propose", optional=True,
             description="提出回滚到曾经发布过的清单，只生成待确认动作",
             params=(_p("target_manifest_id", "string", "目标清单 ID", True), _p("rationale", "string", "依据说明", True))),
    # 查询与任务
    ToolSpec(name="ts_query", plane="world", provider="B07", mode="read", paginated=True,
             description="按 available_at 过滤的只读时序查询",
             params=(_p("quantity", "string", "物理量", True), _p("start", "string", "UTC 开始", True), _p("end", "string", "UTC 结束", True), _CURSOR, _PAGE_SIZE)),
    ToolSpec(name="sensor_query", plane="world", provider="B22", mode="read",
             description="场景观测层：传感器读数与 Quality、残差报警、候选假设（只列候选不下结论）与虚拟传感器推算值；可按设备或传感器查最近窗口，since 只看试探生效后的读数",
             params=(_p("entity", "string", "设备或节点编号（如 AC-1、AIR-HDR）或 zone:production"),
                     _p("sensor_id", "string", "传感器 ID（如 AIR-HDR.P）"),
                     ParamSpec(name="minutes", type="integer", description="最近分钟数，默认 5", minimum=1, maximum=60),
                     _p("since", "string", "只取时间戳晚于该时刻的读数（ISO 8601）"))),
    ToolSpec(name="ledger", plane="world", provider="B16", extended_by=("B08",), mode="read", paginated=True,
             description="读取计划账本或执行账本摘要",
             params=(_p("run_id", "string", "运行 ID", True), _p("kind", "string", "plan/execution", True), _CURSOR, _PAGE_SIZE)),
    ToolSpec(name="get_job", plane="core", provider="B04", mode="read",
             description="查询后台任务；服务端最多等 40 秒。返回 pending 时用同一 job_id 再调用",
             params=(_p("job_id", "string", "任务 ID", True), ParamSpec(name="wait", type="integer", description="等待秒数 0～40", minimum=0, maximum=JOB_WAIT_MAX_S))),
    # v0.8 慢通道（D53/D54）：调查用的只读试算、向值班人员提问与升级工单
    ToolSpec(name="trial", plane="decision", provider="B27", mode="read",
             description="在固定快照上隔离试算候选动作，不改真实运行；返回可行性、违规与软代价",
             params=(_p("actions", "array", "候选动作（entity_id、command、params、interval）", True),
                     _p("horizon_min", "integer", "试算时长（分钟），默认取配置"),
                     _p("ticket_id", "string", "事件单 ID；有多张打开时必填"))),
    ToolSpec(name="ask_operator", plane="core", provider="B27", mode="write",
             description="向值班人员提一个问题；答案经回答卡片回写后继续本轮调查",
             params=(_p("question", "string", "要问的问题", True),
                     _p("ticket_id", "string", "事件单 ID；有多张打开时必填"))),
    ToolSpec(name="escalate", plane="core", provider="B27", mode="write",
             description="把当前事件单升级为操作工单，只升不降；经 urgent 钩子交 B47",
             params=(_p("reason", "string", "升级理由", True),
                     _p("ticket_id", "string", "事件单 ID；有多张打开时必填"))),
    # v0.8 值守层（03 §3.0 上下文组织）：一个通用召回工具按 ID 前缀展开一跳，注入与返回统一为对象 ID + 一行摘要 + 下一跳 ID
    ToolSpec(name="recall", plane="core", provider="B27", mode="read",
             description="按 ID 前缀（event://、ticket://、proposal://、entity://、asset://）展开一跳；单次不超过 8KB，装不下给 next",
             params=(_p("id", "string", "对象 ID，如 ticket://TK-0001", True),)),
    # 专家提案（03 §3.0）：由注册表统一包装（B23），草稿经 ProposalSink 落库，对模型只返回 proposal_id 与摘要。
    # 事件单与轮次来自本次运行的绑定，不由模型填写；各专家分支可在 focus 之外按需扩展参数
    *(ToolSpec(name=f"{domain}_propose", plane="decision", provider=provider, extended_by=("B23",), mode="propose",
               description=f"{label}专家：针对当前事件单确定性算出动作、代价、置信度与安全标记，提案落库后返回 proposal_id",
               params=(_p("focus", "string", "本次关注的对象或问题（可留空）"),))
      for domain, provider, label in (("energy", "B19", "能源"), ("elevator", "B20", "电梯"), ("hvac", "B21", "暖通"),
                                      ("equipment", "B22", "设备监测"))),
    # v0.8 包扩展工具：原由 pack.yaml provides.tools + 包内 ToolSpec 登记，v0.8 统一并入契约目录，
    # 使运行时 exposed_to_agent 工具与 mcp/tools.json 同源（#160）。
    ToolSpec(name="trace_impact", plane="knowledge", provider="B25", mode="read",
             description="沿全局图谱查某设备、管网或配电节点停供时受影响的设备、楼层和区域，每项带图谱路径与安全标记",
             params=(_p("entity", "string", "设备或节点编号（如 AC-1、AIR-HDR、TX1）或 entity:// 标识", True),
                     _p("media", "string", "只沿这些介质查，逗号分隔：power,cooling_water,compressed_air,chilled_water；不填查全部"))),
    ToolSpec(name="find_sources", plane="knowledge", provider="B25", mode="read",
             description="按症状在全局图谱上找来源候选并按领域汇总：load_surge 找下游用电设备（load），pressure_drop 找供气方（supply）与泄漏点（leak），其他症状找上游供给方；位置为区域时服务该区的设备标 serving",
             params=(_p("location", "string", "出现症状的节点编号或区域（如 TX1、AIR-HDR、cleanroom）", True),
                     _p("symptom", "string", "load_surge、overload、pressure_drop、temperature_deviation、power_loss 等", True))),
    ToolSpec(name="safety_tags", plane="knowledge", provider="B25", mode="read",
             description="确定性安全判断：对目标执行某动作是否碰安全标记（含断电、停机经电、水、气网络断到的设备）；命中即须生成操作工单，查不清按涉及处理",
             params=(_p("target", "string", "动作目标的编号或 entity:// 标识", True),
                     _p("action", "string", "动作种类，如 power_off、shed、stop、setpoint_change、dispatch、observe", True),
                     _p("occupied", "boolean", "目标电梯轿厢内是否有人；不填按有人处理"))),
    ToolSpec(name="priority_rules", plane="knowledge", provider="B25", mode="read",
             description="优先级配置：安全永远第一，其余按区域与时段生效；给区域与时刻返回当时顺序，不给返回整份配置",
             params=(_p("zone", "string", "区域：production、admin、cleanroom、hazmat、datacenter、park"),
                     _p("at", "string", "厂区当地时刻（ISO 8601，如 2026-07-01T08:05:00）"))),
    ToolSpec(name="playbooks", plane="knowledge", provider="B25", mode="read",
             description="应急预案：触发条件、牵头专家、分工步骤、是否需操作工单和规程原文；可按事件或目标筛选",
             params=(_p("event", "string", "触发事件，如 elevator_stuck、pressure_drop、load_surge、power_loss、ventilation_fault"),
                     _p("target", "string", "事件发生的设备编号或区域"))),
    ToolSpec(name="prepare_tariff_review", plane="knowledge", provider="B30", mode="read",
             description="结合已抽取政策与可见节日日历，生成待复核的电价时段表",
             params=(_p("candidate_id", "string", "政策候选编号", True),
                     _p("calendar_asset_version", "string", "节日日历资料版本", True),
                     _p("subject_id", "string", "电网接入点实体", True),
                     _p("category", "string", "用电类别", True),
                     _p("effective_from", "string", "规则生效 UTC 时间", True))),
    ToolSpec(name="investigate_onboarding", plane="knowledge", provider="B30", mode="job", exposed_to_agent=False,
             description="提交异常资料调查，结果供人工复核",
             params=(_p("candidate_id", "string", "候选编号", True),)),
    ToolSpec(name="onboarding_investigations", plane="knowledge", provider="B30", mode="read", exposed_to_agent=False,
             description="读取异常资料调查结果",
             params=(_p("candidate_id", "string", "候选编号", True),)),
    ToolSpec(name="record_onboarding_batch", plane="knowledge", provider="B30", mode="write", exposed_to_agent=False,
             description="登记已完成复核批次的实际耗时及用量",
             params=(_p("batch_id", "string", "批次编号", True),
                     _p("candidate_ids", "array", "批次候选编号", True),
                     _p("review_minutes", "number", "实际复核分钟数", True),
                     _p("tokens", "integer", "实际 token 数", True),
                     _p("elapsed_seconds", "number", "实际总秒数", True))),
    ToolSpec(name="score_review_candidate", plane="knowledge", provider="B30", mode="job", exposed_to_agent=False,
             description="提交候选规则反事实评分",
             params=(_p("candidate_id", "string", "候选编号", True),
                     _p("alternative", "object", "备选解释", True))),
    ToolSpec(name="process_onboarding", plane="knowledge", provider="B30", mode="write", exposed_to_agent=False,
             description="抽取资料，按分流结果为例外候选提交调查",
             params=(_p("asset_version_id", "string", "资料版本", True),)),
)


# B42 read-only projections; the ticket remains owned by B27.
class WatchdogTicket(Record):
    ticket_id: str
    status: str
    ticket_status: str
    reason: str
    checked_at: str
    recovery: str
    evidence: list[str] = Field(default_factory=list)
    episode: str | None = None
    job_observation: list[dict[str, Any]] = Field(default_factory=list)


class WatchdogBlocked(Record):
    items: list[WatchdogTicket]


class WatchdogAnswer(Record):
    answer: str


class WatchdogHealth(Record):
    checked_at: str
    health: dict[str, dict[str, str]]
