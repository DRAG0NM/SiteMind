/**
 * 自动巡演：8 段分镜与舞台态要的数据。
 *
 * 分镜表照 .design/sketches/Tour.dc.html 原样搬过来，文案不改；每段的时长、落点页、
 * 镜头与舞台态都在这里定。舞台态要显示的冲突、换算、裁决、专家泳道与预演记录全部从
 * day.json 现算，缺数据的地方写 — 或“当日数据没有这一段”。
 */
import type { CampusSpot } from '../shared/campus'
import { describeAction } from '../shared/campus'
import { outcomeLabel } from '../shared/labels'
import { expertShort, plainText } from './data/labels'
import { activeEvents } from './data/selectors'
import { proposalActions, readings } from './data/rollup'
import { clock, dayAtHour, dayEnd, time } from './data/time'
import type { Day, Decision, Proposal, ProposedAction, Ticket } from './data/types'
import type { TabKey } from './tabs'

export interface TourFrame {
  /** 0 起 */
  index: number
  /** 分镜标题，如“一 应急研判” */
  title: string
  /** 分镜时刻（墙上时间） */
  hour: string
  camera: string
  stage: string
  landing: string
  /** 这一段切到的标签页 */
  tab: TabKey
  /** 每段真实时长（毫秒，1× 倍速） */
  durationMs: number
  /** 事件段：进入舞台态 */
  stageMode: boolean
  /** 普通段提前落到的时刻（毫秒）；让页面进入时就看到刚生效的数据 */
  leadMs?: number
  /** 镜头推近的实体；null 不推 */
  spot: CampusSpot | null
}

export const TOUR_FRAMES: readonly TourFrame[] = [
  {
    index: 0, title: '开场 上岗前', hour: '07:30',
    camera: '整个厂区由暗转亮', stage: '资料入库解析，知识图谱实时构建，知识 v0 → v1',
    landing: '资料与图谱', tab: 'knowledge',
    durationMs: 9000, stageMode: false, spot: null,
  },
  {
    index: 1, title: '一 应急研判', hour: '08:05',
    camera: '推进到行政楼，楼层展开', stage: '冲突 → 换算 → 裁决三段，专家并行泳道',
    landing: '总览舞台 → 主 Agent', tab: 'overview',
    durationMs: 13000, stageMode: true, spot: 'admin',
  },
  {
    index: 2, title: '插曲 新资料到厂', hour: '09:30',
    camera: '切到图谱视图', stage: '两份资料阈值冲突，Agent 查原文定新阈值，v1 → v2',
    landing: '资料与图谱', tab: 'knowledge',
    durationMs: 9000, stageMode: false, leadMs: 5 * 60_000, spot: null,
  },
  {
    index: 3, title: '二 并行反应', hour: '10:30',
    camera: '在空压站和 3F 之间分屏', stage: '四条泳道两条亮起，各自显示研判耗时',
    landing: '设备监测 / 能源', tab: 'equipment',
    durationMs: 10000, stageMode: false, spot: 'compressor',
  },
  {
    index: 4, title: '临时指令', hour: '12:30',
    camera: '镜头停在行政楼', stage: '自然语言指令转为 13:00～17:00 临时规则，并同步测算电费变动',
    landing: '暖通', tab: 'hvac',
    durationMs: 9000, stageMode: false, leadMs: 30 * 60_000, spot: 'admin',
  },
  {
    index: 5, title: '三 涉及安全', hour: '14:20',
    camera: '切进货梯井道，画面压暗', stage: '操作工单弹出，等人确认；显示事件到工单、工单到完成各用时',
    landing: '电梯', tab: 'elevator',
    durationMs: 13000, stageMode: true, spot: 'elevator',
  },
  {
    index: 6, title: '四 纠错后进化', hour: '16:00',
    camera: '沿图谱多跳路径飞行', stage: '调查链路，人纠正，L0 → L2，次日同类事件走快通道',
    landing: '主 Agent → 资料与图谱', tab: 'agent',
    durationMs: 10000, stageMode: false, leadMs: 25 * 60_000, spot: null,
  },
  {
    index: 7, title: '收尾 数字说话', hour: '24:00',
    camera: '拉远到全厂全景', stage: '三组运行方式的数字依次落位，进化时间线收束',
    landing: '效果对比', tab: 'compare',
    durationMs: 11000, stageMode: false, spot: null,
  },
]

/** 开场由暗转亮的时长（毫秒，1×） */
export const TOUR_INTRO_MS = 1100
/** 舞台态占一段的比例，剩下的时间回到普通页面 */
export const STAGE_SHARE = 0.72
/** 事件段把回放头再往前推一点，带上刚落下的裁决 */
const STAGE_LEAD_MS = 2 * 60_000

/** 一段的分镜时刻换算成当天时刻；事件段多推 2 分钟，让裁决记录进画面，普通段按各自 leadMs 提前。 */
export function tourFrameAt(day: Day, frame: TourFrame) {
  const base = hourAt(day, frame.hour)
  return base + (frame.leadMs ?? (frame.stageMode ? STAGE_LEAD_MS : 0))
}

/** 开场那一刻；打开页面时回放头放这里。 */
export const tourStart = (day: Day) => hourAt(day, TOUR_FRAMES[0].hour)

function hourAt(day: Day, hour: string) {
  if (hour === '24:00') return dayEnd(day)
  const [h, m] = hour.split(':').map(Number)
  return dayAtHour(day, h) + m * 60_000
}

const outcomeText = (outcome: string | undefined) => (outcome ? outcomeLabel(outcome) : '没有裁决')

const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value))
const trim = (text: string, max: number) => (text.length > max ? `${text.slice(0, max)}…` : text)

const proposalActionText = (proposal: Proposal, at: number) => {
  const active = proposalActions(proposal, at)
  const actions: Array<{ entityId: string; command: string; params?: Record<string, unknown>; start: number; end: number }> =
    active.length ? active : (proposal.actions ?? []).map(action => ({
      entityId: action.entity_id,
      command: action.command,
      params: action.params,
      start: time(action.interval.start),
      end: time(action.interval.end),
    }))
  return actions.map(describeAction)
}

const actionTextOf = (action: ProposedAction) => describeAction({
  entityId: action.entity_id,
  command: action.command,
  params: action.params,
  start: time(action.interval.start),
  end: time(action.interval.end),
})

export interface TourLane {
  expert: string
  name: string
  /** 诊断摘要（数据；没有方案时说明未参与） */
  diagnosis: string
  /** 耗时文字，如 12.4 s；没有就 — */
  latency: string
  /** 0..1，泳道进度 */
  progress: number
  selected: boolean
}

export interface TourConflict {
  name: string
  demand: string
}

export interface TourStageData {
  decision: Decision | null
  ticket: Ticket | null
  /** 大标题：冲突摘要；没有裁决时写空状态 */
  title: string
  /** 三句说明：事件、读数、裁决，全部由 day.json 算 */
  sentences: string[]
  conflict: TourConflict[]
  conversionHeadline: string
  verdict: string[]
  verdictNote: string
  rehearsal: string
  /** 转人工时的事件 → 工单 → 确认用时 */
  handoffFlow: string
  lanes: TourLane[]
}

const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`)

/** 挑这一幕的裁决：优先提案最多的，其次离分镜时刻最近的。 */
function pickDecision(day: Day, at: number) {
  const window = 15 * 60_000
  const candidates = day.decisions.filter(decision => Math.abs(time(decision.at) - at) <= window)
  return candidates.sort((a, b) => {
    const proposals = (b.conflict.proposal_ids?.length ?? 0) - (a.conflict.proposal_ids?.length ?? 0)
    if (proposals) return proposals
    return Math.abs(time(a.at) - at) - Math.abs(time(b.at) - at)
  })[0]
}

function laneLatency(day: Day, proposal: Proposal | undefined) {
  if (!proposal) return null
  if (proposal.timing?.latency_ms != null) return proposal.timing.latency_ms
  const runs = day.agent_runs.filter(item =>
    item.proposal_ids?.includes(proposal.proposal_id ?? '') && item.timing?.latency_ms != null)
  const expertRun = runs.find(item => item.kind === 'expert')
  return (expertRun ?? runs[0])?.timing?.latency_ms ?? null
}

/** 舞台态的四条泳道：调度顺序里的四类专家，各自的诊断、耗时与进度。 */
function buildLanes(day: Day, at: number, cursor: number, decision: Decision | null, ticket: Ticket | null): TourLane[] {
  const ticketProposals = day.proposals.filter(proposal =>
    proposal.ticket_id === ticket?.ticket_id
    && (proposal.binding ? time(proposal.binding.trigger_at) <= at : true))
  const referenced = new Set(decision?.conflict.proposal_ids ?? [])
  const roster = Array.from(new Set([
    ...day.proposals.filter(proposal => referenced.has(proposal.proposal_id ?? '')).map(proposal => proposal.expert),
    'elevator', 'energy', 'hvac', 'equipment',
    ...ticketProposals.map(proposal => proposal.expert),
  ])).filter(Boolean).slice(0, 4)
  const selected = new Set(decision?.result.selected_proposal_ids ?? [])

  return roster.map(expert => {
    const proposal = ticketProposals.find(item => item.expert === expert && referenced.has(item.proposal_id ?? ''))
      ?? ticketProposals.find(item => item.expert === expert)
    const latencyMs = laneLatency(day, proposal)
    const trigger = proposal?.binding ? time(proposal.binding.trigger_at) : ticket ? time(ticket.opened_at) : at
    const finish = decision ? time(decision.at) : at + 60_000
    const progress = !proposal
      ? 0.04
      : latencyMs != null
        ? 1
        : clamp((cursor - trigger) / Math.max(1, finish - trigger), 0.08, 0.96)
    return {
      expert,
      name: expertShort(expert),
      diagnosis: proposal
        ? trim(proposal.diagnosis ?? (proposalActionText(proposal, at).join('、') || '已提交方案'), 34)
        : ticket?.experts?.includes(expert) && decision
          ? `第 ${decision.layer} 层转人工 · 专家不自动处置`
          : '本段未参与会诊',
      latency: seconds(latencyMs),
      progress,
      selected: Boolean(proposal?.proposal_id && selected.has(proposal.proposal_id)),
    }
  })
}

/**
 * 事件段的舞台数据。at 是这一幕的分镜时刻（已含 2 分钟推进），cursor 是回放头，
 * 泳道进度按回放头走，这样四条泳道在段内会看得见地往前推。
 */
export function tourStage(day: Day, at: number, cursor = at): TourStageData {
  const decision = pickDecision(day, at) ?? null
  const ticket = decision ? day.tickets.find(item => item.ticket_id === decision.ticket_id) ?? null : null

  const eventIds = ticket?.event_ids ?? []
  const events = day.events
    .filter(event => eventIds.includes(event.event_id) && time(event.at) <= cursor)
    .sort((a, b) => time(a.at) - time(b.at))
  const visibleEvents = events.length ? events : activeEvents(day, at, 20 * 60_000).reverse().slice(0, 2)
  const eventSentence = visibleEvents.length
    ? `${clock(time(visibleEvents[0].at), day.timezone)} 起，${visibleEvents.slice(0, 2).map(event => event.summary).join('；')}。`
    : `回放头 ${clock(at, day.timezone)} 没有进行中的事件记录。`

  const facts = readings(day, at)
    .filter(item => item.value !== '—')
    .slice(0, 4)
    .map(item => `${item.label} ${item.value}${item.unit}`)
  const readingSentence = facts.length ? `${facts.join('、')}。` : '设备快照里没有这一刻的读数。'

  const selectedProposals = decision
    ? day.proposals.filter(proposal => proposal.proposal_id && decision.result.selected_proposal_ids.includes(proposal.proposal_id))
    : []
  const verdict = Array.from(new Set(selectedProposals.flatMap(proposal => proposalActionText(proposal, at))))
  const verdictNote = decision
    ? `结果：${outcomeText(decision.result.outcome)}${decision.result.summary ? ` · ${decision.result.summary}` : ''}`
    : '这一刻没有裁决记录。'
  const decisionSentence = decision
    ? `Agent ${verdict.length ? `下发 ${verdict.join('、')}。` : `结论：${decision.result.summary}`}`
    : '这一刻没有裁决记录。'

  const referenced = new Set(decision?.conflict.proposal_ids ?? [])
  const conflict = decision?.conflict.proposal_ids?.length
    ? day.proposals.filter(proposal => referenced.has(proposal.proposal_id ?? '')).map(proposal => ({
        name: expertShort(proposal.expert),
        demand: trim(proposal.diagnosis ?? proposal.actions?.map(actionTextOf).join('、') ?? '未写诊断', 18),
      }))
    : decision
      ? [{ name: ticket?.experts?.map(expertShort).join(' / ') || '现场', demand: decision.conflict.summary }]
      : []

  const rehearsalNote = decision?.conversion.notes?.find(note => note.includes('预演'))
  const rehearsal = rehearsalNote
    ? rehearsalNote
    : ticket?.rehearsal
      ? '当日有预演记录'
      : '当日数据没有预演记录'

  const handoffFlow = decision && decision.result.outcome !== 'executed' && ticket
    ? (() => {
        const order = day.work_orders.find(item => item.ticket_id === ticket.ticket_id)
        return `事件 ${clock(time(ticket.opened_at), day.timezone)}${order ? ` → 工单 ${clock(time(order.created_at), day.timezone)}` : ''}${ticket.closed_at && time(ticket.closed_at) <= at ? ` → 关闭 ${clock(time(ticket.closed_at), day.timezone)}` : ' · 等待人工确认'}`
      })()
    : ''

  return {
    decision,
    ticket,
    title: decision ? decision.conflict.summary : '这一段没有裁决记录',
    sentences: [eventSentence, readingSentence, decisionSentence],
    conflict,
    conversionHeadline: decision?.conversion.notes?.[0] ? plainText(decision.conversion.notes[0]) : '当日数据没有这一段的换算',
    verdict,
    verdictNote,
    rehearsal,
    handoffFlow,
    lanes: buildLanes(day, at, cursor, decision, ticket),
  }
}
