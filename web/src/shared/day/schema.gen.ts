/**
 * 本文件由 contracts/day.schema.json 生成，不要手改。
 * 重新生成：cd web && npm run gen:types（契约改了没重跑时 schema.test.ts 会失败）。
 */

export type DataNature = "measured" | "derived" | "configured" | "synthetic" | "simulated";
export type ActionReadiness = "ready" | "needs_diagnosis" | "needs_human";
/**
 * AgentResultSource 的结果类型：专家运行、门控不一致研判、第 3 层裁决、追问。
 */
export type AgentResultKind = "expert" | "triage" | "arbitration" | "follow_up" | "investigate";
/**
 * v0.8 运行模式（03 §3.0 运行模式）。deterministic 是 live 来源缺失时的记录口径，不是请求模式。
 */
export type RunMode = "live" | "trial" | "evaluation" | "deterministic";
/**
 * 网关判定的终态原因（07 Q03 四种异常 + 正常结束）。
 */
export type TerminalReason =
  "completed" | "completed_from_deltas" | "timeout" | "cancelled" | "stream_lost_recovered" | "stream_lost" | "error";
export type EvidenceEventType =
  | "tool_called"
  | "tool_result"
  | "retrieval"
  | "graph_query"
  | "rule_lookup"
  | "rule_applied"
  | "conflict_detected"
  | "path_switch"
  | "plan_created"
  | "gate_decided"
  | "pending_action_proposed"
  | "action_decided"
  | "job_submitted"
  | "job_finished"
  | "execution_recorded"
  | "version_activated"
  | "candidate_status_changed"
  | "autonomy_paused"
  | "autonomy_resumed"
  | "action_cancelled"
  | "action_revoked";
export type EvidencePath = "retrieval" | "reasoning" | "none";
export type ResultStatus = "ok" | "pending" | "degraded" | "failed" | "conflict";

/**
 * “一天”数据（day.json）。开发工作台实时流与回放展示页共用；展示页只靠它离线重建状态。
 *
 * v0.8（B49）：可选 `cache_version` 给评测缓存键的数据版本（无生产者时留空，不伪造）；
 * `evidence` 是本次运行链的证据事件，`assets` 只放被证据引用到的资产条目（B49 资料页用）。
 * v0.8（#238）：`context` 携带被当日记录引用的跨日记录（默认空）。
 */
export interface DayRecord {
  schema_version?: "1";
  plant_id: string;
  local_date: string;
  timezone: string;
  run_id: string;
  /**
   * @minItems 1
   */
  runs: [RunChainEntry, ...RunChainEntry[]];
  events?: DayEvent[];
  tickets?: Ticket[];
  proposals?: ExpertProposal[];
  decisions?: DayDecision[];
  work_orders?: WorkOrder[];
  context?: DayContext;
  temp_rules?: TempRule[];
  knowledge_versions?: KnowledgeVersionEntry[];
  qa?: QaEntry[];
  agent_runs?: AgentRunRecord[];
  metrics?: DayMetric[];
  state_series?: StateFrame[];
  ledger_minutes?: LedgerEntry[];
  cache_version?: string | null;
  assets?: AssetVersion[];
  evidence?: EvidenceEvent[];
}
/**
 * 运行接续链：展示页离线重建分叉。
 */
export interface RunChainEntry {
  run_id: string;
  forked_from?: string | null;
  forked_at?: string | null;
  knowledge_version?: number | null;
  data_sources?: DataSourceRef[];
}
/**
 * 场景运行用到的数据来源与版本（v0.8）。
 */
export interface DataSourceRef {
  source: string;
  dataset_version: string;
}
export interface DayEvent {
  event_id: string;
  at: string;
  event_type: string;
  source: "script" | "random" | "sim_limit" | "operator_message";
  entity_id?: string | null;
  severity?: number;
  summary: string;
  payload?: {
    [k: string]: unknown;
  };
  nature: DataNature;
}
export interface Ticket {
  ticket_id: string;
  opened_at: string;
  closed_at?: string | null;
  status: "open" | "waiting" | "blocked" | "resolved" | "closed";
  rehearsal?: boolean;
  round: number;
  event_ids?: string[];
  experts?: string[];
  subscriptions?: TicketSubscription[];
  summary?: string;
  status_history?: StatusChange[];
}
export interface TicketSubscription {
  entity_id: string;
  /**
   * @minItems 1
   */
  event_types: [string, ...string[]];
}
/**
 * 一次状态迁移（#217-3）：只在状态变化时追加，不按分钟写。
 */
export interface StatusChange {
  status: string;
  at: string;
}
/**
 * 专家提案（03 §3.0 提案以落库为准）。propose 工具返回 proposal_id 为空的草稿，
 * B23 注册表包装层经 ProposalSink.save 落库分配 proposal_id 并补 binding 与 timing；
 * diagnosis 与 evidence_refs 可由专家回答事后经 attach_reply 补充。
 */
export interface ExpertProposal {
  proposal_id?: string | null;
  expert: string;
  ticket_id: string;
  actions?: ProposedAction[];
  cost: ProposalCost;
  confidence: number;
  diagnosis?: string | null;
  evidence_refs?: string[];
  safety_tags?: string[];
  binding?: RunBinding | null;
  timing?: AgentTiming | null;
}
/**
 * 专家提案里的一个动作。
 */
export interface ProposedAction {
  /**
   * entity://{厂区}/{类型}/{编号}；不随资料修改而变
   */
  entity_id: string;
  command: string;
  params?: {
    [k: string]: unknown;
  };
  interval: Interval;
  readiness?: ActionReadiness;
  depends_on?: string[];
}
/**
 * 左闭右开 UTC 区间 [start, end)。
 */
export interface Interval {
  start: string;
  end: string;
}
/**
 * 同一把尺子上的三项代价；cny 为负表示节省。
 */
export interface ProposalCost {
  cny: number;
  wait_min?: number;
  comfort_min?: number;
}
/**
 * 一次专家或主 Agent 运行的绑定（03 §3.0 轮次与过期）。绑定不符的结果只入历史，不执行、不回写。
 */
export interface RunBinding {
  run_id: string;
  knowledge_version: number;
  ticket_id: string;
  round: number;
  state_version: number;
  trigger_at: string;
}
/**
 * 三个仿真时间与墙钟耗时（03 §3.0 时间协调）。仿真时间决定动作生效，墙钟只进报告。
 */
export interface AgentTiming {
  origin_event_at: string;
  trigger_at: string;
  effective_at: string;
  requested_at: string;
  returned_at?: string | null;
  latency_ms?: number | null;
  retries?: number;
}
/**
 * 裁决“冲突 → 换算 → 结果”三步（03 §3.6）。
 */
export interface DayDecision {
  decision_id: string;
  ticket_id: string;
  round: number;
  layer: 1 | 2 | 3;
  at: string;
  conflict: DecisionConflict;
  conversion: DecisionConversion;
  result: DecisionResult;
}
export interface DecisionConflict {
  summary: string;
  proposal_ids: string[];
}
/**
 * 换算：把各提案（或组合）放到同一把尺子上。
 */
export interface DecisionConversion {
  policy_version: string;
  scores: {
    [k: string]: number;
  };
  infeasible?: string[];
  notes?: string[];
}
export interface DecisionResult {
  selected_proposal_ids: string[];
  outcome: "executed" | "work_order" | "to_human" | "sampling" | "blocked";
  summary: string;
}
export interface WorkOrder {
  work_order_id: string;
  ticket_id: string;
  created_at: string;
  reason: string;
  actions?: ProposedAction[];
  status: "proposed" | "confirmed" | "rejected" | "expired" | "done";
  pending_action_id?: string | null;
  status_history?: StatusChange[];
}
/**
 * “一天”的跨日引用闭包（#217-1、#217-2 / #238）。
 *
 * 被当日记录引用、但发生在当日以外的记录，原样携带，不进当日时间轴和计数。
 * 展示页按 ID 查找时先查当日小节、再查这里。
 */
export interface DayContext {
  proposals?: ExpertProposal[];
  events?: DayEvent[];
  tickets?: Ticket[];
}
/**
 * 临时指令转成的临时规则（03 §3.6），到期自动失效，不进长期知识。
 */
export interface TempRule {
  rule_id: string;
  text: string;
  effective: Interval;
  entity_ids?: string[];
  created_at: string;
  source_event_id?: string | null;
  estimated_cost_cny?: number | null;
}
export interface KnowledgeVersionEntry {
  version: number;
  version_manifest_id: string;
  activated_at: string;
  summary?: string;
}
export interface QaEntry {
  qa_id: string;
  at: string;
  question: string;
  answer: string;
  agent_run_id?: string | null;
  evidence_refs?: string[];
}
/**
 * 一次专家或主 Agent 运行（live 实调、缓存或确定性实现）的记录。
 *
 * B23 扩展：live 运行另记模型、Agent 节点与 depth、工具调用（模型填值 → 系统绑定值）与最终回答，
 * 供展示页“查看 Nexent 运行记录”使用；这些字段都可选，缓存与确定性记录可以不填。
 * 工作台的问答（kind=follow_up）不属于事件单，binding 可以为空。
 */
export interface AgentRunRecord {
  agent_run_id: string;
  kind: AgentResultKind;
  agent_name: string;
  run_mode: RunMode;
  source: "nexent" | "cache" | "deterministic";
  binding: RunBinding | null;
  timing: AgentTiming;
  status: "finished" | "failed" | "timeout" | "blocked" | "cancelled";
  coverage_gap?: boolean;
  proposal_ids?: string[];
  model?: string | null;
  conversation_id?: string | null;
  query?: string | null;
  terminal_reason?: TerminalReason | null;
  nodes?: AgentRunNode[];
  tool_calls?: AgentToolCall[];
  final_answer?: string | null;
}
/**
 * 一次运行里的一个 Agent 节点（B23）：主 Agent 为 depth 0；子 Agent 按 Nexent SSE 的 invocation_id 区分。
 */
export interface AgentRunNode {
  node_id: string;
  agent_name: string;
  depth: number;
  parent_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  final_answer?: string | null;
}
/**
 * 一次 plantkg 工具调用（B23）：以 plantkg 自己的调用记录为准，不取 SSE 里模型填的参数。
 */
export interface AgentToolCall {
  call_id: string;
  agent_name: string;
  depth?: number;
  tool: string;
  model_params?: {
    [k: string]: unknown;
  };
  bound_params?: {
    [k: string]: unknown;
  };
  started_at: string;
  duration_ms: number;
  business_status: string;
  summary?: string | null;
}
export interface DayMetric {
  metric: string;
  group: "baseline" | "independent" | "coordinated" | "knowledge" | "config";
  value: number | null;
  unit?: string | null;
  note?: string | null;
  nature?: DataNature | null;
  variant?: string | null;
  counts?: AssetStateCounts | null;
  as_of?: string | null;
}
export interface AssetStateCounts {
  dormant: number;
  identified: number;
  in_graph: number;
  referenced: number;
}
/**
 * 每 15 分钟一帧全量设备状态，加上本帧到下一帧之间按分钟的变化。
 */
export interface StateFrame {
  at: string;
  devices: {
    [k: string]: {
      [k: string]: unknown;
    };
  };
  changes?: StateChange[];
}
export interface StateChange {
  at: string;
  entity_id: string;
  field: string;
  value: unknown;
}
/**
 * 一个时段的交流侧账本（非负量，kW 时段平均、kWh 时段电量）。
 */
export interface LedgerEntry {
  interval: Interval;
  pv_available_kwh: number;
  pv_used_kwh: number;
  pv_curtailed_kwh: number;
  grid_import_kwh: number;
  grid_export_kwh: number;
  charge_kwh: number;
  discharge_kwh: number;
  load_kwh: number;
  energy_start_kwh: number;
  energy_end_kwh: number;
  soc_end: number;
  price_cny_per_kwh?: number | null;
  cost_cny?: number | null;
  residual_kwh: number;
  load_breakdown?: {
    [k: string]: number;
  } | null;
}
export interface AssetVersion {
  /**
   * asset://{行业}/{来源哈希前8位}/{类型}/{定位}；内容变即新版本
   */
  asset_version_id: string;
  /**
   * 完整 sha256，去重与完整性校验用
   */
  content_hash: string;
  industry: string;
  asset_type: string;
  source_uri: string;
  media_type: string;
  nature: DataNature;
  available_at: string;
  ingested_at: string;
  parent_version_id?: string | null;
  /**
   * Items: entity://{厂区}/{类型}/{编号}；不随资料修改而变
   */
  entity_ids?: string[];
  license?: string | null;
  title?: string | null;
}
/**
 * 证据事件：写入即可订阅，供网关实时合流（09 D19）。
 */
export interface EvidenceEvent {
  event_id: string;
  seq: number;
  occurred_at: string;
  agent_run_id?: string | null;
  run_id?: string | null;
  trace_id: string;
  call_id?: string | null;
  action_id?: string | null;
  event_type: EvidenceEventType;
  tool?: string | null;
  path?: EvidencePath;
  rule_ids?: string[];
  sources?: EvidenceRef[];
  version_manifest_id?: string | null;
  available_at?: string | null;
  nature?: DataNature | null;
  result_status: ResultStatus;
  params?: {
    [k: string]: unknown;
  };
  model_params?: {
    [k: string]: unknown;
  } | null;
  summary?: string | null;
}
export interface EvidenceRef {
  asset_version_id?: string | null;
  locator?: Locator | null;
  evidence_id?: string | null;
  perspective?: string | null;
  excerpt?: string | null;
}
/**
 * 原文定位：页码、条款、表格单元格、图片框、行或时间区间，至少给一项。
 */
export interface Locator {
  page?: number | null;
  clause?: string | null;
  cell?: string | null;
  bbox?: [unknown, unknown, unknown, unknown] | null;
  line_start?: number | null;
  line_end?: number | null;
  time_range?: Interval | null;
  meter?: string | null;
}
