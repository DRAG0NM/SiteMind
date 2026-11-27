/**
 * 回放展示页用到的 day.json 契约（B46 contracts/day.schema.json）。
 * 与契约一一对应的记录直接用生成文件里的类型（npm run gen:types 生成，不要手改 schema.gen.ts）；
 * 展示页自己的放宽或扩展写在下面，并注明原因。
 */
import type * as Generated from '../../shared/day/schema.gen'

export type DataNature = Generated.DataNature
export type ActionReadiness = Generated.ActionReadiness
export type Interval = Generated.Interval
export type RunBinding = Generated.RunBinding
export type AgentTiming = Generated.AgentTiming
export type ProposedAction = Generated.ProposedAction
export type ProposalCost = Generated.ProposalCost
/** 契约里的名字是 ExpertProposal，展示页沿用旧名 Proposal。 */
export type Proposal = Generated.ExpertProposal
export type DayEvent = Generated.DayEvent
export type TicketSubscription = Generated.TicketSubscription

/**
 * 契约把 status 限成枚举；展示页要兼容旧导出与手写样例里的其它状态值（测试里也有 running），
 * 所以只放宽这一个字段，其余照契约。
 */
export interface Ticket extends Omit<Generated.Ticket, 'status'> {
  status: string
}

export type DecisionResult = Generated.DecisionResult
export type DecisionConversion = Generated.DecisionConversion
export type DecisionConflict = Generated.DecisionConflict
/** 契约里的名字是 DayDecision，展示页沿用旧名 Decision。 */
export type Decision = Generated.DayDecision

/** 同 Ticket：展示页按字符串读工单状态，不跟契约枚举较真。 */
export interface WorkOrder extends Omit<Generated.WorkOrder, 'status'> {
  status: string
}

export type TempRule = Generated.TempRule
/** 契约里的名字是 KnowledgeVersionEntry。 */
export type KnowledgeVersion = Generated.KnowledgeVersionEntry
export type QaEntry = Generated.QaEntry
export type AgentRunNode = Generated.AgentRunNode
export type AgentToolCall = Generated.AgentToolCall
/** 契约里的名字是 AgentRunRecord。 */
export type AgentRun = Generated.AgentRunRecord
export type DayMetric = Generated.DayMetric
export type StateChange = Generated.StateChange
export type StateFrame = Generated.StateFrame
/** 契约里的名字是 LedgerEntry。 */
export type LedgerMinute = Generated.LedgerEntry
/** 资料资产版本（B26）；day.json 可选小节，旧导出没有这一节。 */
export type AssetVersion = Generated.AssetVersion
/** 证据里的原文定位；B49 只用到 asset_version_id 与 excerpt。 */
export type EvidenceRef = Generated.EvidenceRef
export type ResultStatus = Generated.ResultStatus
/** 证据事件（B17 导出）；day.json 可选小节，旧导出没有这一节。 */
export type EvidenceEvent = Generated.EvidenceEvent
export type RunChainEntry = Generated.RunChainEntry
/** 被当日记录引用、但发生在当日以外的记录（#217-1/#217-2）。只按 ID 查找，不进时间轴和计数。 */
export interface DayContext extends Omit<Generated.DayContext, 'proposals' | 'events' | 'tickets'> {
  proposals?: Proposal[]
  events?: DayEvent[]
  tickets?: Ticket[]
}

type OptionalSections =
  | 'context'
  | 'events'
  | 'tickets'
  | 'proposals'
  | 'decisions'
  | 'work_orders'
  | 'temp_rules'
  | 'knowledge_versions'
  | 'qa'
  | 'agent_runs'
  | 'metrics'
  | 'state_series'
  | 'ledger_minutes'

/**
 * day.json 全量。契约里除 runs 外的小节都可选（校验层会补成空数组），
 * 展示页一直按“小节是数组”用，这里把必填性补回来，页面与选择器不用改。
 */
export type Day = Omit<Generated.DayRecord, OptionalSections> & {
  /** 可选：跨日引用闭包（B17 导出才有）。 */
  context?: DayContext
  events: DayEvent[]
  tickets: Ticket[]
  proposals: Proposal[]
  decisions: Decision[]
  work_orders: WorkOrder[]
  temp_rules: TempRule[]
  knowledge_versions: KnowledgeVersion[]
  qa: QaEntry[]
  agent_runs: AgentRun[]
  metrics: DayMetric[]
  state_series: StateFrame[]
  ledger_minutes: LedgerMinute[]
}
