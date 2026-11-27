import { describeAction, spotOf } from '../../shared/campus'
import { agentLabel, businessStatusLabel, confidenceText, costText, expertLabel, plainText, runStatusLabel, toolLabel, toolSummaryText } from './labels'
import { eventsOfTicket, findProposal, proposalsOfTicket } from './lookup'
import { proposalActions } from './rollup'
import { decisionVisible, latestKnowledge } from './selectors'
import { clock, resultVisibleAt, time } from './time'
import type { Day, Proposal } from './types'

export interface EvidenceExpert {
  name: string
  expert: string
  diagnosis: string
  cost: string
  confidence: string
  selected: boolean
}

export interface EvidenceRun {
  runId: string
  agent: string
  status: string
  /** 场景触发时刻（HH:MM）；墙钟 timing 只作耗时证据，不上屏。 */
  trigger: string
  latency: string
  ticket: string
  finalAnswer?: string
  nodes: { agent: string; depth: number; duration: string }[]
  tools: { tool: string; agent?: string; depth?: number; duration: string; status: string; summary: string }[]
}

export interface Evidence {
  title: string
  subtitle: string
  entityId: string | null
  refs: string[]
  rule: { source: string; text: string } | null
  notes: string[]
  graph: string[]
  experts: EvidenceExpert[]
  runs: EvidenceRun[]
}

const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`)
/** 运行耗时统一写成“47.7 秒”，与触发时刻一起显示。 */
const runSeconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} 秒`)
/** 场景时刻（HH:MM）：优先 binding.trigger_at，缺了写 —，不拿墙钟时刻顶替。 */
const sceneClock = (iso: string | null | undefined, timezone: string) => {
  const at = iso ? time(iso) : Number.NaN
  return Number.isFinite(at) ? clock(at, timezone) : '—'
}

/** 选中一个设备后，抽屉里放这一天里与它有关的依据、动作、专家意见与运行记录。 */
export function evidenceFor(day: Day, cursor: number, entityId: string | null, fallbackTitle: string, requestedTicketId?: string): Evidence {
  // 同一测点一天里会被多次事件引用（例如 T1 的早高峰与晚高峰），按数组顺序取末项会错配到早上的提案；
  // 这里按仿真生效时刻取最近一条可见的提案。指定事件单时按 ID 查找（先当日、再 context）；按设备找仍只看当日。
  const proposals = (requestedTicketId ? proposalsOfTicket(day, requestedTicketId) : day.proposals)
    .filter(proposal =>
      (requestedTicketId ? true : entityId != null && (
        (proposal.actions ?? []).some(action => action.entity_id === entityId)
        || (proposal.evidence_refs ?? []).includes(entityId)))
      && resultVisibleAt(proposal) <= cursor)
    .sort((a, b) => resultVisibleAt(a) - resultVisibleAt(b))
  const proposal = proposals[proposals.length - 1]
  const ticketId = requestedTicketId ?? proposal?.ticket_id
  const selectedIds = new Set(day.decisions
    .filter(decision => decisionVisible(day, decision, cursor))
    .flatMap(decision => decision.result.selected_proposal_ids))
  const decision = proposal
    ? [...day.decisions].reverse().find(item =>
      decisionVisible(day, item, cursor)
      && (item.conflict.proposal_ids ?? []).includes(proposal.proposal_id ?? ''))
    : undefined
  // 指定事件单时事件按 ID 查（先当日、再 context）；按设备找仍只用当日时间轴。
  const events = (requestedTicketId
    ? eventsOfTicket(day, requestedTicketId)
    : day.events.filter(event => entityId !== null && event.entity_id === entityId))
    .filter(event => time(event.at) <= cursor)
  const event = events[events.length - 1]
  const knowledge = latestKnowledge(day, cursor)
  const actions = proposal ? proposalActions(proposal, cursor) : []
  const actionText = actions.length ? actions.map(describeAction).join('、') : null
  const title = actionText ?? event?.summary ?? fallbackTitle
  const subtitle = `${event ? clock(time(event.at), day.timezone) : '—'}${proposal ? ` · ${expertLabel(proposal.expert)}提案` : ''}`

  const refs = Array.from(new Set([
    // 抽屉要能看到主事件单这一次裁决的全部依据，不能只给其中一位专家的；跨日提案按 ID 从 context 取。
    ...(decision
      ? decision.result.selected_proposal_ids
        .flatMap(proposalId => findProposal(day, proposalId) ?? [])
        .flatMap(item => item.evidence_refs ?? [])
      : []),
    ...(proposal?.evidence_refs ?? []),
    ...(decision?.conversion.notes ?? []).map(plainText),
    ...(knowledge ? [`knowledge v${knowledge.version}`] : []),
  ])).filter(Boolean)

  const graph = Array.from(new Set([
    ...(entityId ? [entityId] : []),
    ...(proposal?.evidence_refs ?? []).filter(ref => ref.includes('://')),
    ...(day.temp_rules.filter(rule => (rule.entity_ids ?? []).includes(entityId ?? '')).map(rule => rule.rule_id)),
  ]))

  const experts: EvidenceExpert[] = proposalsOfTicket(day, ticketId)
    .filter(item => resultVisibleAt(item) <= cursor)
    .map(item => ({
      name: expertLabel(item.expert),
      expert: item.expert,
      diagnosis: item.diagnosis ?? '—',
      cost: costText(item.cost.cny),
      confidence: confidenceText(item.confidence),
      selected: item.proposal_id ? selectedIds.has(item.proposal_id) : false,
    }))

  const nodeDuration = (start?: string | null, finish?: string | null) =>
    start && finish ? seconds(Date.parse(finish) - Date.parse(start)) : '—'
  const runs: EvidenceRun[] = day.agent_runs
    // 整次运行按仿真生效时刻裁剪：生效前不露工具明细和最终回答，墙钟 requested_at / returned_at 不参与比较。
    .filter(run => ticketId != null && run.binding?.ticket_id === ticketId
      && resultVisibleAt(run) <= cursor)
    .map(run => {
      const tools = (run.tool_calls ?? []).map(call => ({
        tool: toolLabel(call.tool),
        agent: call.agent_name ? agentLabel(call.agent_name) : undefined,
        depth: call.depth,
        duration: seconds(call.duration_ms),
        status: call.business_status ? businessStatusLabel(call.business_status) : '—',
        summary: call.summary ? toolSummaryText(call.summary) : call.business_status ? businessStatusLabel(call.business_status) : '—',
      }))
      // 运行记录里的节点优先；没有节点时用工具调用的 agent_name / depth 还原调用层级。
      const nodes = run.nodes?.length
        ? run.nodes.map(node => ({ agent: agentLabel(node.agent_name), depth: node.depth, duration: nodeDuration(node.started_at, node.finished_at) }))
        : tools.filter(call => call.agent).map(call => ({ agent: call.agent as string, depth: call.depth ?? 0, duration: '—' }))
      return {
        runId: run.agent_run_id,
        agent: agentLabel(run.agent_name),
        status: runStatusLabel(run.status),
        trigger: sceneClock(run.binding?.trigger_at, day.timezone),
        latency: runSeconds(run.timing?.latency_ms),
        ticket: run.binding?.ticket_id?.split('/').pop() ?? '—',
        finalAnswer: run.final_answer ?? undefined,
        nodes: nodes.filter((node, index) => nodes.findIndex(item => item.agent === node.agent && item.depth === node.depth) === index),
        tools,
      }
    })

  return {
    title,
    subtitle,
    entityId,
    refs,
    rule: knowledge ? { source: `知识 v${knowledge.version}`, text: knowledge.summary ?? '' } : null,
    notes: [
      ...(proposal?.diagnosis ? [`判断：${proposal.diagnosis}`] : []),
      ...(decision ? [`裁决：${decision.result.summary}`, `权衡：${decision.conflict.summary}`] : []),
      ...(proposal?.safety_tags?.length ? [`安全标签：${proposal.safety_tags.join('、')}`] : []),
    ],
    graph,
    experts,
    runs,
  }
}

/** 某个提案的裁决与依据，供主 Agent 页与运行记录用。 */
export function decisionChain(day: Day, cursor: number) {
  const past = [...day.decisions].filter(item => decisionVisible(day, item, cursor)).sort((a, b) => time(a.at) - time(b.at))
  if (!past.length) return null
  // 决策链优先看最新一次自动执行的裁决，其次有提案的；慢通道执行结果没有提案，也要能看到。
  const preferred = past.filter(item => item.result.outcome === 'executed' || (item.conflict.proposal_ids ?? []).length > 0)
  const decision = [...preferred].reverse()[0] ?? past[past.length - 1]
  const selected = new Set(decision.result.selected_proposal_ids)
  const proposals = (decision.conflict.proposal_ids ?? [])
    .flatMap(proposalId => findProposal(day, proposalId) ?? [])
  const pending = [...past].reverse().find(item => item.result.outcome === 'to_human'
    && item.decision_id !== decision.decision_id) ?? null
  return {
    decision,
    selected: proposals.filter(proposal => proposal.proposal_id && selected.has(proposal.proposal_id)),
    rejected: proposals.filter(proposal => !proposal.proposal_id || !selected.has(proposal.proposal_id)),
    spot: proposals.map(proposal => spotOf((proposal.actions ?? [])[0]?.entity_id)).find(Boolean) ?? null,
    pending,
  }
}

/** 最近一次真正执行了动作的裁决；总览右栏的结论用它，避免和“正在执行的动作”对不上。 */
export function latestExecutedDecision(day: Day, cursor: number) {
  const executed = [...day.decisions]
    .filter(item => decisionVisible(day, item, cursor) && item.result.outcome === 'executed'
      && item.result.selected_proposal_ids.length > 0)
    .sort((a, b) => time(a.at) - time(b.at))
  return executed[executed.length - 1] ?? null
}

export function isSelected(proposal: Proposal, day: Day, cursor: number) {
  return day.decisions
    .filter(decision => decisionVisible(day, decision, cursor))
    .some(decision => decision.result.selected_proposal_ids.includes(proposal.proposal_id ?? ''))
}
