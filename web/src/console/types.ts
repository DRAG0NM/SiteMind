/** 控制台的视图模型：三块屏都从这里取数，界面组件不再自己算。 */
import type { CampusEntity, CampusSpot } from '../shared/campus'
import type { LoadPoint } from '../shared/LoadCurve'
import type { Day, Verdict } from '../store'

export type ScreenKey = 'wall' | 'agent' | 'desk' | 'metrics' | 'knowledge'

export interface ConsoleActionView {
  key: string
  label: string
  /** 悬停提示：认不出的命令把原命令名放这里 */
  title?: string
  detail: string
  entityId: string
  /** 原始命令与参数，厂区图着色用 */
  command: string
  params: Record<string, unknown>
  spot: CampusSpot
  startedAt: number
  endsAt: number
  /** 1.5 秒内撤销过的动作 */
  retracted: boolean
  /** 撤销影响的一句话，撤销确认框里用 */
  impact: string
}

export type QueueKind = 'approve' | 'work_order' | 'review' | 'correction' | 'question' | 'publish'

/** 操作工单来源：伤亡类与重大损失类分开展示（B47 的口径）。 */
export type WorkOrderSource = 'casualty' | 'major_loss' | 'other'

export interface QueueFact { label: string; value: string }

export interface QueueItemView {
  id: string
  kind: QueueKind
  title: string
  subtitle: string
  /** 为什么提出 */
  detail: string
  facts: QueueFact[]
  /** 判断依据：冲突、换算说明、派单原因等，展示用，不与 facts 重复 */
  notes: string[]
  /** 来源记录（裁决 / 工单 / 版本清单编号），处理结果写入处置记录 */
  source: string
  at: number
  entityId: string | null
  /** 后端有对应动作编号时才可能有真接口，没有就是前端替身 */
  actionId: string | null
  /** 后端动作当前修订，确认时原样带 expected_revision */
  revision?: number
  /** 后端动作的原始类型（sampling/review/...）：抽查卡据此判断哪些条目可勾“不是系统判断有误” */
  backendKind?: string
  /** 真实待确认动作的 payload（question 的正文、工单的 actions 等都在这里） */
  payload?: Record<string, unknown>
  /** data.ts 补编的演示件 */
  synthetic?: boolean
  /** 操作工单来源 */
  workOrderSource?: WorkOrderSource
  /** 确认后才执行的处置动作；确认前不会下发（工单 payload.actions） */
  disposalActions?: string[]
  /** 已自动执行的预案保护动作；只有后端 payload 带保护动作字段或演示补编数据时才有值 */
  protections?: string[]
  /** protections 是演示补编的清单 */
  protectionsSynthetic?: boolean
  verdict: Verdict | null
}

export interface RuleView {
  id: string
  text: string
  until: string
  cancelled: boolean
}

export interface CompareRow { label: string; note: string; chosen: boolean }

export interface LaneStepView {
  id: string
  label: string
  tool: string
  /** 开始时刻（毫秒） */
  start: number
  durationMs: number
  status: 'done' | 'running' | 'pending'
  /** 没有工具调用记录，用事件时间戳推算 */
  estimated: boolean
  input: string
  compare: CompareRow[]
  result: string
}

export interface LaneView {
  agent: string
  label: string
  steps: LaneStepView[]
}

export interface StreamRow {
  id: string
  at: number
  type: string
  agent: string
  summary: string
  stepId: string | null
}

export interface AlertView {
  id: string
  at: number
  title: string
  state: string
  severity: number
}

export interface EnergyState {
  strategy: string
  price: string
  priceTier: string
  soc: string
  power: string
  plan: { at: string; text: string }[]
  /** 全厂负荷与来源构成，单位 kW；缺账本分钟时为 null */
  load: { total: number; grid: number; pv: number; storage: number; capacity: number | null } | null
}

export interface ConsoleCounts {
  approve: number
  work_order: number
  review: number
  correction: number
  question: number
  publish: number
  pending: number
}

export interface ConsoleModel {
  day: Day
  cursor: number
  now: string
  playing: boolean
  paused: boolean
  pausedAt: number | null
  mode: 'replay' | 'live'
  source: string
  /** 回放进度 0..1，接实时数据后为 null */
  progress: number | null
  campus: CampusEntity[]
  loadPoints: LoadPoint[]
  actions: ConsoleActionView[]
  rules: RuleView[]
  queue: QueueItemView[]
  counts: ConsoleCounts
  lanes: LaneView[]
  stream: StreamRow[]
  alerts: AlertView[]
  energy: EnergyState
  /** 本次运行的信息，运行席标题用；trigger 是场景触发时刻（HH:MM），墙钟时刻不上屏 */
  run: { id: string; trigger?: string; channel: string; elapsed: string } | null
  /** 后台预演的结论（与预演一致 / 没有预演记录） */
  rehearsal: { text: string; ok: boolean } | null
}
