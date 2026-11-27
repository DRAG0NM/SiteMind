/** 控制台证据抽屉的数据链：先读后端解释与证据，读不到就用当天记录合成，并标出数据性质。 */
import {
  getAssetFragments, getEvidenceEvents, getPlanExplanation, getPlans,
  type EvidenceEventRaw,
} from '../../api'
import type { Day } from '../../store'
import type { DataNature } from '../common/nature'
import { proposalsOfTicket } from '../../showcase/data/lookup'
import { onlineRunId } from '../../showcase/data/online'

export type EvidenceKind = 'queue' | 'action' | 'work_order'
export type EvidencePath = 'retrieval' | 'reasoning'
/** 页面只要给出对象与可选的运行/动作编号，其余由这里决定。 */
export interface EvidenceSubject {
  title: string
  entityId?: string | null
  actionId?: string | null
  runId?: string | null
  kind?: EvidenceKind
}

export interface EvidenceRow { label: string; value: string }
export interface EvidenceFragment { id: string; locator: string; text: string; kind?: string | null }

export interface EvidenceView {
  /** 数据来自真实接口；false 表示四个接口都没读到，已退回当天记录合成 */
  real: boolean
  /** 真实数据的来源：方案解释或证据链 */
  origin: string
  /** 真实链路的证据都没标 nature 时为 null，界面显示“来源未标注”，不按实测兜底（#248-5） */
  nature: DataNature | null
  natureSource: string
  path: EvidencePath
  model: EvidenceRow[]
  bound: EvidenceRow[]
  graph: string[]
  fragments: EvidenceFragment[]
}

const NATURES: DataNature[] = ['measured', 'derived', 'simulated', 'synthetic', 'configured']

const messageOf = (reason: unknown) => (reason instanceof Error ? reason.message : String(reason))

const unique = (values: string[]) => Array.from(new Set(values.map(value => value.trim()).filter(Boolean)))

function formatValue(value: unknown): string {
  if (value == null) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try { return JSON.stringify(value) } catch { return String(value) }
}

function formatStamp(value: string | null | undefined, timezone: string): string {
  if (!value) return '—'
  const stamp = Date.parse(value)
  if (!Number.isFinite(stamp)) return value
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: timezone, hourCycle: 'h23',
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).format(stamp)
  } catch {
    return new Date(stamp).toISOString()
  }
}

function paramRows(events: EvidenceEventRaw[], key: 'model_params' | 'params'): EvidenceRow[] {
  const rows: EvidenceRow[] = []
  const seen = new Set<string>()
  for (const event of events) {
    const params = event[key]
    if (!params || typeof params !== 'object') continue
    for (const [name, value] of Object.entries(params)) {
      if (seen.has(name)) continue
      seen.add(name)
      rows.push({ label: name, value: formatValue(value) })
    }
  }
  return rows
}

function livePath(events: EvidenceEventRaw[]): EvidencePath {
  let retrieval = 0
  let reasoning = 0
  for (const event of events) {
    if (event.path === 'retrieval') retrieval += 1
    else if (event.path === 'reasoning') reasoning += 1
  }
  return retrieval > reasoning ? 'retrieval' : 'reasoning'
}

/** 证据链里第一条标了合法 nature 的事件；都没标就返回 null，不冒充实测（#248-5）。 */
function liveNature(events: EvidenceEventRaw[]): DataNature | null {
  for (const event of events) {
    if (event.nature && NATURES.includes(event.nature as DataNature)) return event.nature as DataNature
  }
  return null
}

/** sources 里出现 asset:// 时再拉资料原文片段；失败由调用方统一退回合成。 */
async function loadFragments(events: EvidenceEventRaw[]): Promise<EvidenceFragment[]> {
  const assets = unique(events.flatMap(event =>
    (event.sources ?? []).map(source => source?.asset_version_id ?? '')))
  const rows: EvidenceFragment[] = []
  for (const assetId of assets) {
    let fragments
    try {
      fragments = await getAssetFragments(assetId)
    } catch (reason) {
      throw new Error(`原文片段读取失败：${messageOf(reason)}`)
    }
    for (const [index, fragment] of fragments.entries()) {
      const text = (fragment.text ?? '').trim()
      if (!text) continue
      rows.push({
        id: `${assetId}#${fragment.fragment_id || index}`,
        locator: fragment.locator?.trim() || fragment.fragment_id || '原文',
        text,
        kind: fragment.kind,
      })
    }
  }
  return rows
}

export function buildLiveView(
  events: EvidenceEventRaw[], fragments: EvidenceFragment[], origin: string, subject: EvidenceSubject,
): EvidenceView {
  const refs = unique(events.flatMap(event => [
    ...(event.rule_ids ?? []),
    ...(event.sources ?? []).flatMap(source => [
      source?.evidence_id ?? '',
      source?.asset_version_id ?? '',
    ]),
  ]))
  const graph = unique([subject.entityId ?? '', ...refs])
  const excerpts: EvidenceFragment[] = []
  for (const event of events) {
    for (const source of event.sources ?? []) {
      const text = (source?.excerpt ?? '').trim()
      if (!text) continue
      excerpts.push({
        id: `${source?.evidence_id ?? source?.asset_version_id ?? 'source'}-${excerpts.length}`,
        locator: source?.locator?.trim() || source?.evidence_id || source?.asset_version_id || '资料原文',
        text,
        kind: null,
      })
    }
  }
  return {
    real: true,
    origin,
    nature: liveNature(events),
    natureSource: origin,
    path: livePath(events),
    model: paramRows(events, 'model_params'),
    bound: paramRows(events, 'params'),
    graph,
    fragments: [...excerpts, ...fragments],
  }
}

/**
 * 真实数据顺序：/plans 取第一个方案 → 方案解释 → 证据链 → 都没有才抛错由 useRemote 退回合成。
 * 原文片段的接口失败也算整条真实链路失败，界面统一标 synthetic，不混着显示。
 */
export async function loadEvidence(subject: EvidenceSubject, day: Day): Promise<EvidenceView> {
  // 在线模式（?run_id=）的运行编号优先，其次调用方给的，最后才是 day.run_id（#248-8）。
  const runId = onlineRunId() || subject.runId?.trim() || day.run_id
  const reasons: string[] = []
  let events: EvidenceEventRaw[] = []
  let origin = ''

  try {
    const plans = await getPlans(runId)
    const planId = plans.find(plan => typeof plan?.plan_id === 'string' && plan.plan_id.trim())?.plan_id
    if (!planId) reasons.push('后端没有这次运行的方案记录')
    else {
      const explanation = await getPlanExplanation(planId, subject.actionId)
      if (explanation.length) { events = explanation; origin = '方案解释' }
      else reasons.push('方案解释为空')
    }
  } catch (reason) {
    reasons.push(messageOf(reason))
  }

  if (!events.length) {
    try {
      const evidence = await getEvidenceEvents(runId, subject.actionId)
      if (evidence.length) { events = evidence; origin = '证据链' }
      else reasons.push('证据链没有这条记录')
    } catch (reason) {
      reasons.push(messageOf(reason))
    }
  }

  if (!events.length) throw new Error(unique(reasons).join('；') || '后端没有这条证据记录')
  const fragments = await loadFragments(events)
  return buildLiveView(events, fragments, origin, subject)
}

/** 从当天的 proposals / decisions / agent_runs / knowledge_versions 里取该对象或动作的依据。 */
export function buildSyntheticView(day: Day, cursor: number, subject: EvidenceSubject): EvidenceView {
  const entityId = subject.entityId?.trim() || null
  const actionId = subject.actionId?.trim() || null
  const at = (value?: string | null) => (value ? Date.parse(value) : Number.NaN)
  const seenBy = (value?: string | null) => {
    const stamp = at(value)
    return !Number.isFinite(stamp) || stamp <= cursor
  }

  const workOrder = day.work_orders.find(order =>
    (actionId ? order.pending_action_id === actionId : false)
    || (entityId ? (order.actions ?? []).some(action => action.entity_id === entityId) : false))
  // 指定了工单就按事件单编号聚合：当日提案在前，跨日引用闭包里被裁决引用的提案按 ID 补上。
  const proposals = (workOrder ? proposalsOfTicket(day, workOrder.ticket_id) : day.proposals).filter(proposal => {
    if (!seenBy(proposal.binding?.trigger_at)) return false
    if (workOrder && proposal.ticket_id === workOrder.ticket_id) return true
    return entityId ? (proposal.actions ?? []).some(action => action.entity_id === entityId) : true
  })
  const proposal = proposals[proposals.length - 1] ?? null
  const ticketId = workOrder?.ticket_id ?? proposal?.ticket_id ?? null
  const decision = [...day.decisions]
    .filter(item => {
      if (!seenBy(item.at)) return false
      if (proposal?.proposal_id) {
        const ids = [...(item.conflict?.proposal_ids ?? []), ...(item.result?.selected_proposal_ids ?? [])]
        return ids.includes(proposal.proposal_id)
      }
      return ticketId ? item.ticket_id === ticketId : false
    })
    .sort((a, b) => at(a.at) - at(b.at))
    .pop() ?? null
  const run = day.agent_runs
    .filter(item => (ticketId ? item.binding?.ticket_id === ticketId : true))
    .filter(item => seenBy(item.timing?.trigger_at ?? item.timing?.effective_at))
    .sort((a, b) => at(a.timing?.trigger_at) - at(b.timing?.trigger_at))
    .pop() ?? null
  const knowledge = [...day.knowledge_versions]
    .filter(item => seenBy(item.activated_at))
    .sort((a, b) => at(a.activated_at) - at(b.activated_at))
    .pop() ?? null

  const actions = [...(proposal?.actions ?? []), ...(workOrder?.actions ?? [])]
    .filter(action => !entityId || action.entity_id === entityId)

  const model: EvidenceRow[] = []
  const pushModel = (label: string, value: string) => {
    if (model.some(row => row.label === label)) return
    model.push({ label, value })
  }
  if (proposal?.diagnosis) pushModel('诊断', proposal.diagnosis)
  for (const action of actions) {
    pushModel('动作', action.command)
    for (const [name, value] of Object.entries(action.params ?? {})) pushModel(name, formatValue(value))
  }
  if (proposal?.confidence != null) pushModel('置信度', proposal.confidence.toFixed(2))
  if (run?.tool_calls?.length) pushModel('工具调用', run.tool_calls.map(call => call.tool).join('、'))

  const bound: EvidenceRow[] = []
  const pushBound = (label: string, value: string) => {
    if (bound.some(row => row.label === label)) return
    bound.push({ label, value })
  }
  if (actionId) pushBound('动作编号', actionId)
  if (proposal?.binding) {
    pushBound('运行编号', proposal.binding.run_id)
    pushBound('知识版本', `v${proposal.binding.knowledge_version}`)
    pushBound('事件单', proposal.binding.ticket_id)
    pushBound('轮次', String(proposal.binding.round))
    pushBound('状态版本', String(proposal.binding.state_version))
    pushBound('触发时刻', formatStamp(proposal.binding.trigger_at, day.timezone))
  }
  if (decision?.conversion?.policy_version) pushBound('策略版本', decision.conversion.policy_version)
  if (decision?.layer != null) pushBound('裁决层级', `L${decision.layer}`)
  for (const [name, value] of Object.entries(decision?.conversion?.scores ?? {})) {
    pushBound(`评分 · ${name}`, formatValue(value))
  }
  if (workOrder) {
    pushBound('工单', workOrder.work_order_id)
    pushBound('工单状态', workOrder.status)
    if (workOrder.pending_action_id) pushBound('待确认动作', workOrder.pending_action_id)
  }
  if (!bound.length) pushBound('运行编号', day.run_id)

  const graph = unique([
    entityId ?? '',
    ...(proposal?.actions ?? []).map(action => action.entity_id),
    ...(workOrder?.actions ?? []).map(action => action.entity_id),
    ...(proposal?.evidence_refs ?? []),
    ...day.temp_rules
      .filter(rule => (entityId ? (rule.entity_ids ?? []).includes(entityId) : false))
      .map(rule => rule.rule_id),
    proposal?.proposal_id ?? '',
    knowledge ? `knowledge v${knowledge.version}` : '',
  ])

  const fragments: EvidenceFragment[] = []
  if (knowledge?.summary) {
    fragments.push({ id: `knowledge-v${knowledge.version}`, locator: `知识 v${knowledge.version}`, text: knowledge.summary })
  }
  for (const note of decision?.conversion?.notes ?? []) {
    fragments.push({ id: `decision-note-${fragments.length}`, locator: '裁决换算说明', text: note })
  }
  if (workOrder?.reason) {
    fragments.push({ id: workOrder.work_order_id, locator: '工单原因', text: workOrder.reason })
  }
  if (run?.final_answer) {
    fragments.push({ id: run.agent_run_id, locator: 'Agent 最后回答', text: run.final_answer })
  }

  return {
    real: false,
    origin: '当天记录',
    nature: 'synthetic',
    natureSource: '仿真演示场景记录',
    path: 'reasoning',
    model,
    bound,
    graph,
    fragments,
  }
}
