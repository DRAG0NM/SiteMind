"""plantkg 端口定义（B01 冻结）。

各平面、包与后端实现这些 Protocol；core 不导入任何实现（03 §1.1 规则 1）。签名依据
development/04「端口签名与共享模型贡献」。端口只收发 contracts.py 的记录；Pyomo 表达式等求解器对象
在 ModelBuilder 内部流转，类型记为 Expr，不进入契约记录。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from plantkg.core.contracts import (
    Actor,
    AgentResultKind,
    AgentRunRecord,
    AssetFragment,
    AssetVersion,
    BacktestReport,
    Candidate,
    ConstraintContribution,
    Context,
    ContributionReceipt,
    DayEvent,
    Entity,
    EntityId,
    ExpertProposal,
    ExpertReply,
    EvaluationReport,
    EvidenceEvent,
    EvidencePage,
    ExecutionResult,
    ForecastBundle,
    FrozenSplit,
    GateDecision,
    Interval,
    Job,
    JobExecutorHealth,
    JobKind,
    JobQueueSnapshot,
    JobStatus,
    JudgeResult,
    Ledger,
    OntologyCandidate,
    PendingAction,
    PendingActionKind,
    PendingIssue,
    Plan,
    PlanningRequest,
    PlanResult,
    Ref,
    Rule,
    RuleCandidate,
    RunBinding,
    RunInfo,
    RunSession,
    SourceBundle,
    SimAction,
    StateSnapshot,
    ToolResult,
    ToolSpec,
    TrialResult,
    UIEvent,
    Unit,
    VersionDiff,
    VersionManifest,
)

# 求解器表达式（Pyomo 等），只在 ModelBuilder 与包之间流转
Expr = Any
VarDomain = Literal["continuous", "binary", "integer"]


# ---------------------------------------------------------------- 世界平面


@runtime_checkable
class Adapter(Protocol):
    """把原始数据转成规范化来源记录（B05/B21）。"""

    name: str

    def normalize(self, source_ref: str, mapping: Mapping[str, Any], visibility: Mapping[str, Any]) -> SourceBundle: ...


@runtime_checkable
class StateProvider(Protocol):
    """Replay 现在、Sim 以后（03 §1.3）。一个 run 一个回放时钟与状态提交者。"""

    def at(self, context: Context, instant: datetime) -> StateSnapshot: ...

    def advance(self, context: Context, expected_seq: int, next_instant: datetime) -> StateSnapshot: ...


@runtime_checkable
class Executor(Protocol):
    """Record 现在、Apply 以后。幂等：同一 idempotency_key 返回同一结果。"""

    def execute(self, confirmed_plan: Plan, context: Context, expected_seq: int, idempotency_key: str) -> ExecutionResult: ...


@runtime_checkable
class LedgerCalculator(Protocol):
    """B08：独立于优化变量重算账本，是 balanced 的唯一判定方。

    责任边界：compute 从计划动作与来源数据重算每时段各项电量、残差、SOC 与费用，不读求解器变量值；
    validate 做契约层做不到的检查（费用按已审核电价规则另算、平移前后工序电量相等、总表减分表非负、
    计划账本与模型目标一致），问题写入 violations。Ledger 构造时契约再独立复核能量平衡、SOC 递推等
    （Ledger.recheck），两边任一不通过都不能 balanced=True。
    """

    def compute(self, plan: Plan, actuals: SourceBundle | ForecastBundle, kind: Literal["plan", "execution"], context: Context) -> Ledger: ...

    def validate(self, ledger: Ledger, rules: Sequence[Rule], original_profiles: SourceBundle | None = None) -> Sequence[str]: ...


@runtime_checkable
class TimeSeriesQuery(Protocol):
    """B07：按 available_at 过滤的只读时序查询（ts_query）。"""

    def query(self, context: Context, quantity: str, window: Interval, cursor: str | None = None) -> SourceBundle: ...


# ---------------------------------------------------------------- 决策平面


@runtime_checkable
class Forecaster(Protocol):
    def predict(self, context: Context, horizon: Interval, visible_history: SourceBundle) -> ForecastBundle: ...


@runtime_checkable
class ModelBuilder(Protocol):
    """包接触共享模型的唯一入口（04 规划模型契约）。目标由 B09 唯一汇总。"""

    @property
    def time_set(self) -> Sequence[Interval]: ...

    @property
    def grid_import(self) -> Expr: ...

    @property
    def grid_export(self) -> Expr: ...

    @property
    def pv_available(self) -> tuple[float, ...] | None:
        """光伏可发预测（kW，每时段一个），只读；v0.8 追加，未提供时为 None。"""
        ...

    @property
    def total_load(self) -> Expr:
        """总负荷表达式 total_load[t]，所有包贡献完并 finalize 后可用；v0.8 追加。"""
        ...

    def add_var(self, name: str, domain: VarDomain, bounds: tuple[float | None, float | None], unit: Unit, *,
                index: Sequence[int] | None = None, scalar: bool = False,
                action: tuple[EntityId, str] | None = None) -> Expr: ...

    def add_constraint(self, name: str, expr: Expr, refs: Sequence[Ref]) -> None: ...

    def register_balance(self, side: Literal["supply", "demand"], expr: Expr, label: str) -> None: ...

    def register_cost(self, expr: Expr, label: str) -> None: ...


@runtime_checkable
class RuleKind(Protocol):
    """由包注册的规则类型与编译器。不能编译时返回 PendingIssue，不静默忽略。"""

    kind: str
    compiler_version: str

    def compile(self, rule: Rule, context: Context) -> ConstraintContribution | PendingIssue: ...


@runtime_checkable
class DomainPack(Protocol):
    """领域包：pv、storage、tariff、production（之后：environment…）。"""

    name: str
    rule_kinds: Sequence[RuleKind]
    tool_specs: Sequence[ToolSpec]

    def contribute(
        self, model_builder: ModelBuilder, state: StateSnapshot, rules: Sequence[Rule], forecasts: ForecastBundle
    ) -> ContributionReceipt: ...


@runtime_checkable
class Planner(Protocol):
    """唯一共享 MILP（B09）；求解只在 JobRunner 后台任务里执行。"""

    def solve(self, request: PlanningRequest) -> PlanResult: ...


@runtime_checkable
class Gate(Protocol):
    def assess(self, plan: Plan, forecast_quality: Mapping[str, float], rule_status: Mapping[str, str], baseline: Plan | None) -> GateDecision: ...

    def preview_inputs(self, context: Context) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class Evaluator(Protocol):
    def compare(self, executions: Sequence[ExecutionResult], comparable_baselines: Sequence[ExecutionResult]) -> EvaluationReport: ...


@runtime_checkable
class Explainer(Protocol):
    """B19 最小引用、B26 完整投影：动作 → 规则、原文定位、版本与可见时间。"""

    def explain(self, context: Context, plan_id: str, action_id: str | None = None, cursor: str | None = None) -> EvidencePage: ...


# ---------------------------------------------------------------- 知识平面


@runtime_checkable
class AssetRepository(Protocol):
    def ingest(self, context: Context, source_uri: str, asset_type: str, available_at: datetime) -> AssetVersion: ...

    def get(self, context: Context, asset_version_id: str) -> AssetVersion: ...

    def list(self, context: Context, cursor: str | None = None, page_size: int = 50) -> tuple[Sequence[AssetVersion], str | None]: ...

    def resolve_entity(self, context: Context, entity_id: str) -> Entity: ...


@runtime_checkable
class AssetContentReader(Protocol):
    """按资料版本及调用时点读取经散列校验的原文字节。"""

    def read_content(self, context: Context, asset_version_id: str) -> bytes: ...


@runtime_checkable
class DocumentParser(Protocol):
    """B21：文本、表格、图文解析。"""

    def parse(self, asset: AssetVersion) -> Sequence[AssetFragment]: ...


@runtime_checkable
class KnowledgeIndex(Protocol):
    """B21/B26：Nexent 知识库桥。可见文档清单由 plantkg 按 as_of 计算。"""

    def index(self, asset: AssetVersion, fragments: Sequence[AssetFragment]) -> str: ...

    def visible_document_paths(self, context: Context) -> Sequence[str]: ...


@runtime_checkable
class GraphStore(Protocol):
    """B03/B22：版本化命名图；只读查询走白名单/参数化。"""

    def prepare(self, graph_name: str, triples_ttl: str) -> str: ...

    def read_named_graph(
        self, context: Context, graph_name: str, query_id: str, params: Mapping[str, Any], cursor: str | None = None
    ) -> tuple[Sequence[Mapping[str, Any]], str | None]:
        """返回（本页结果，下一页 cursor）。"""
        ...


@runtime_checkable
class SeriesStore(Protocol):
    """B03：Parquet + DuckDB 时序。"""

    def write_partition(self, bundle: SourceBundle) -> str: ...

    def query(self, context: Context, quantity: str, window: Interval) -> SourceBundle: ...


@runtime_checkable
class OntologyBuilder(Protocol):
    """B22：四层本体候选、链接、结构校验与快照。"""

    def propose_candidates(self, context: Context, subject_ids: Sequence[str], layer: str) -> Sequence[OntologyCandidate]: ...

    def link_entities(self, context: Context, subject_id: str, target_id: str, relation: str) -> OntologyCandidate: ...

    def validate(self, context: Context, graph_name: str) -> Sequence[PendingIssue]: ...

    def snapshot(self, context: Context) -> str: ...


@runtime_checkable
class Judge(Protocol):
    """判别后端：jev（默认）、fallback、fake，由 judge.backend 切换（B23）。"""

    backend: str

    def decide(self, state: Mapping[str, Any], question: OntologyCandidate, options: Sequence[str]) -> JudgeResult: ...


@runtime_checkable
class Calibrator(Protocol):
    """B23：按后端与模型版本隔离的概率校准。"""

    def fit(self, results: Sequence[JudgeResult], labels: Sequence[str], split: FrozenSplit) -> str: ...

    def predict(self, result: JudgeResult) -> float | None: ...


@runtime_checkable
class RuleExtractor(Protocol):
    """B25：抽取不批准；候选经复核后才成为 Rule。"""

    def extract(self, context: Context, fragments: Sequence[AssetFragment]) -> Sequence[RuleCandidate]: ...


@runtime_checkable
class RuleRepository(Protocol):
    def applicable(self, context: Context, window: Interval, cursor: str | None = None) -> tuple[Sequence[Rule], str | None]:
        """返回（对 context.as_of 可见、在 window 内适用、可编译的规则，下一页 cursor）。"""
        ...

    def pending_issues(self, context: Context) -> Sequence[PendingIssue]: ...


@runtime_checkable
class ChangeImpact(Protocol):
    """B27：版本差异与受影响计划；未知项明确列出。"""

    def diff(self, context: Context, from_manifest_id: str, to_manifest_id: str) -> VersionDiff: ...


# ---------------------------------------------------------------- 进化平面


@runtime_checkable
class CandidateGenerator(Protocol):
    kind: str

    def propose(self, trigger: Mapping[str, Any], base_manifest: VersionManifest, context: Context) -> Sequence[Candidate]: ...


@runtime_checkable
class Backtester(Protocol):
    """只在冻结验证日上回测；走 JobRunner。"""

    def run(self, candidate: Candidate, frozen_split: FrozenSplit, context: Context) -> BacktestReport: ...


@runtime_checkable
class ValidationPolicy(Protocol):
    """参数候选须更好且硬约束不退化；知识候选按正确性/审核准则（07 Q17）。"""

    version: str

    def judge(self, candidate: Candidate, report: BacktestReport) -> Literal["validated", "rejected"]: ...


# ---------------------------------------------------------------- core 基础


@runtime_checkable
class Repository(Protocol):
    """SQLite 单写者仓储（B03）。"""

    def transaction_short(self) -> AbstractContextManager[None]: ...

    def read(self, context: Context, table: str, key: Mapping[str, Any]) -> Mapping[str, Any] | None: ...

    def append_once(self, key: str, record: Mapping[str, Any]) -> bool:
        """幂等追加；key 已存在且内容相同返回 False，不同则抛 IDEMPOTENCY_MISMATCH。"""
        ...


@runtime_checkable
class VersionStore(Protocol):
    """不可变产物 → 完整性检查 → 短事务激活（03 §1.2）。"""

    def prepare(self, components: Mapping[str, Any], reason: str) -> VersionManifest: ...

    def check_integrity(self, manifest_id: str) -> VersionManifest: ...

    def activate(self, expected_active: str | None, candidate: str, actor: Actor, reason: str) -> VersionManifest: ...

    def active(self) -> VersionManifest: ...

    def get(self, manifest_id: str) -> VersionManifest: ...

    def has_verifier(self, prefix: str) -> bool: ...


@runtime_checkable
class RunStore(Protocol):
    def create_run(self, context: Context, kind: str, clock: Interval, forked_from: str | None = None) -> RunInfo: ...

    def get_run(self, run_id: str) -> RunInfo: ...


@runtime_checkable
class EvidenceSink(Protocol):
    def append(self, event: EvidenceEvent) -> EvidenceEvent: ...

    def list(self, run_id: str | None, cursor: str | None, visibility: Context) -> EvidencePage: ...

    def subscribe(self, trace_id: str) -> Iterator[EvidenceEvent]: ...


@runtime_checkable
class CtxResolver(Protocol):
    """按 ctx 查 RunSession 得到 Context；查不到返回 None，由失败关闭装饰器处理（B02）。"""

    def resolve(self, ctx: str | None) -> Context | None: ...


@runtime_checkable
class RunSessionStore(Protocol):
    def register(
        self, actor: Actor, plant_id: str, as_of: datetime, visible_docs: Sequence[str], ttl_s: int, agent_run_id: str, trace_id: str,
        industry_pack: str | None = None, origin: str = "gateway", *, run_id: str | None = None,
        version_manifest_id: str | None = None,
    ) -> RunSession: ...

    def resolve(self, ctx: str) -> RunSession | None: ...

    def resolve_binding(self, ctx: str) -> tuple[str, str] | None: ...

    def expire(self, ctx: str) -> None: ...


@runtime_checkable
class JobRunner(Protocol):
    """后台任务。同一 idempotency_key 返回同一 Job；get 最多等 40 秒。"""

    def submit(self, kind: JobKind, payload: Mapping[str, Any], idempotency_key: str, context: Context) -> Job: ...

    def get(self, job_id: str, wait_seconds: float = 0) -> Job: ...

    def snapshot(self, context: Context, run_id: str | None = None, status: JobStatus | str | None = None,
                 limit: int = 50, offset: int = 0) -> JobQueueSnapshot:
        """按厂区只读枚举任务（#69）：按 run_id 与状态过滤，按提交时间排序，limit/offset 分页。

        context.plant_id 不是本执行器的厂区时抛 CROSS_PLANT；total 为过滤后、分页前的任务数。
        """
        ...

    def health(self, context: Context) -> JobExecutorHealth:
        """按厂区只读读取执行器健康（#69）：配置线程数、存活线程数、队列深度与是否已关闭。"""
        ...


@runtime_checkable
class JobHandlerRegistrar(Protocol):
    """领域模块向 B04 后台任务队列登记执行函数。"""

    def register(self, kind: JobKind | str, handler: Any, *, fallback: bool = False) -> None: ...


@runtime_checkable
class AgentRunReader(Protocol):
    """按运行编号读取网关保存的 Agent 运行记录。"""

    def get(self, agent_run_id: str) -> AgentRunRecord | None: ...


@runtime_checkable
class PendingActionStore(Protocol):
    def propose(self, kind: PendingActionKind, payload: Mapping[str, Any], context: Context,
                *, action_id: str | None = None) -> PendingAction: ...

    def confirm(self, action_id: str, actor: Actor, reason: str, expected_revision: int, idempotency_key: str, edits: Mapping[str, Any] | None = None) -> PendingAction: ...

    def reject(self, action_id: str, actor: Actor, reason: str, expected_revision: int, idempotency_key: str,
               edits: Mapping[str, Any] | None = None) -> PendingAction: ...


@runtime_checkable
class ActionHandler(Protocol):
    """统一确认端点的执行函数：B15 计划确认、B18 发布/回滚、B24 复核结论，向 B04 注册。"""

    kind: PendingActionKind

    def apply(self, action: PendingAction, actor: Actor) -> ToolResult: ...


@runtime_checkable
class ToolHandler(Protocol):
    """注册表工具：MCP 薄入口只做校验、上下文绑定、端口调用与结果转换。"""

    spec: ToolSpec

    def __call__(self, context: Context | None, **params: Any) -> ToolResult: ...


@runtime_checkable
class Gateway(Protocol):
    """B23：登记会话、注入 ctx 与 document_paths、调 Nexent 北向、合流事件。"""

    def run(self, actor: Actor, query: str, agent_name: str, as_of: datetime | None = None) -> AsyncIterator[UIEvent]: ...

    async def stop(self, agent_run_id: str, actor: Actor) -> None: ...


# ---------------------------------------------------------------- v0.8 端口（B46，03 §3.0）


@runtime_checkable
class ProposalSink(Protocol):
    """B27：专家提案落库。save 分配 proposal_id；回答经 attach_reply 补诊断与依据，不改数值字段。"""

    def save(self, proposal: ExpertProposal, binding: RunBinding) -> str: ...

    def attach_reply(self, proposal_id: str, reply: ExpertReply) -> ExpertProposal: ...

    def get(self, proposal_id: str) -> ExpertProposal: ...

    def list(self, ticket_id: str, round_: int | None = None) -> list[ExpertProposal]: ...


@runtime_checkable
class RuleMatcher(Protocol):
    """B27：判断事件是否已被已发布规则覆盖（B30/B32 的规则 + B25 playbooks）。

    快通道只在“有匹配规则且 Jev 门控放行”时走；B27 提供按 playbooks 与事件类型白名单的 fallback 实现。
    """

    def covers(self, event: DayEvent) -> bool: ...


@runtime_checkable
class KnowledgePublisher(Protocol):
    """B30 先登记最简实现，B32 登记完整实现替换；只生成待确认的发布提议。"""

    def propose_publish(self, candidate: Candidate) -> PendingAction: ...


@runtime_checkable
class AgentResultSource(Protocol):
    """专家运行与主 Agent 研判、第 3 层裁决、追问的结果来源。按 Context.run_mode 经注册表分派：
    live 由 B23 网关实调；deterministic 由各 kind 的负责分支登记；trial/evaluation 只由 B33 缓存登记。"""

    def get(self, kind: AgentResultKind, binding: RunBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class TrialRunner(Protocol):
    """B17：在快照上隔离试算候选动作，不改真实运行。"""

    def trial(self, snapshot: StateSnapshot, candidate: Sequence[SimAction], horizon: Interval) -> TrialResult: ...


@runtime_checkable
class ForkHook(Protocol):
    """分叉回调：B17 在知识发布分叉时调用，B27（事件单、订阅）、B47（临时规则、工单）用 register_hook 登记。"""

    def on_fork(self, parent_run: RunInfo, child_run: RunInfo, at: datetime) -> None: ...


# 钩子名：fn(context) -> (RunBinding, AgentTiming) | None。网关运行绑定的事件单，模型不能改
RUN_BINDING_HOOK = "run.binding"


class LiveRunResult(TypedDict, total=False):
    """live 运行结果的最小契约（B23/B27/B29 共用）：网关交回、值守层消费的字段。

    status 取 AgentRunRecord 的取值；terminal_reason 取 TerminalReason；其余字段消息可按需缺省。
    """

    status: str
    terminal_reason: str | None
    final_answer: str | None
    agent_run_id: str | None
    latency_ms: int | None
    retries: int
    coverage_gap: bool
