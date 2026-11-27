import { actionTitle, buildCampus, describeAction, spotOf } from '../shared/campus'
import type { LoadPoint } from '../shared/LoadCurve'
import { toolLabel } from '../shared/labels'
import { campusReadings } from '../shared/readings'
import { dayEnd, dayStart, devicesAt, time, useDay, type Day, type Verdict } from '../store'
import type { WatchdogTicket, PendingActionRaw } from '../api'
import { proposalsOfTicket } from '../showcase/data/lookup'
import { useMemo } from 'react'
import type {
  AlertView, ConsoleActionView, ConsoleModel, EnergyState, LaneStepView, LaneView, QueueItemView, QueueKind,
  RuleView, StreamRow, WorkOrderSource,
} from './types'

export { toolLabel }

const EXPERT_LABEL: Record<string, string> = {
  plantkg_main: '主 Agent',
  energy_expert: '能源专家',
  elevator_expert: '电梯专家',
  hvac_expert: '暖通专家',
  equipment_expert: '设备监测专家',
  watchdog: 'Watchdog',
}
const LANE_ORDER = ['plantkg_main', 'energy_expert', 'elevator_expert', 'hvac_expert', 'equipment_expert', 'watchdog']
const KIND_ORDER = ['approve', 'work_order', 'review', 'correction', 'question', 'publish'] as const

export const fmtClock = (at: number, timeZone = 'Asia/Shanghai') =>
  new Intl.DateTimeFormat('zh-CN', { timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(at)
const money = (value: number) => `¥${value.toFixed(1)}`
const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`
/** 运行席头部的耗时：与证据抽屉统一写“47.7 秒”。 */
const runSeconds = (ms: number) => `${(ms / 1000).toFixed(1)} 秒`
const firstName = (text: string) => text.split(/[；;，,。]/)[0] || text
const shortId = (value: string) => value.split('/').pop() ?? value
const asText = (value: unknown): string => typeof value === 'string' && value.trim() ? value.trim() : ''

/** 操作工单来源：B47 把伤亡类与重大损失类分开派单，界面也分开显示。 */
function workOrderSourceOf(reason: string): WorkOrderSource {
  if (/困人|伤亡|人员|救援|安全/.test(reason)) return 'casualty'
  if (/重大损失|超.*线|损失.*线/.test(reason)) return 'major_loss'
  return 'other'
}

/** 待确认动作的一句话：对象 · 命令。 */
function pendingActionLabel(item: Record<string, unknown>): string {
  const entity = asText(item.entity_id)
  const command = asText(item.command)
  return `${entity ? (spotOf(entity) ?? shortId(entity)) : '对象'} · ${command || '动作'}`
}

/** 演示补编的预案保护动作：只给演示日试算的人工工单用，界面上标“合成”。 */
const DEMO_PROTECTIONS: Record<string, string[]> = {
  'wo-0002': ['其他货梯接管生产任务，生产不受影响', '暖通给井道通风，等待救援'],
  'wo-demo-air': ['能源专家切换照明备用回路（同一事件单并行处置）'],
  'wo-demo-t1': ['储能放电 390 kW 支撑 T1', '电梯分区运行把启动功率压下来'],
}

/** 真实待确认动作 → 处置席队列项：只有这五类卡片与发布能落到队列里，其余后端动作不显示。 */
const ACTION_QUEUE_KIND: Record<string, QueueKind | undefined> = {
  work_order: 'work_order', correction: 'correction', sampling: 'review', review: 'review', review_decision: 'review',
  question: 'question', publish: 'publish', plan_confirmation: 'approve', human_decision: 'approve',
}

export function actionQueueItems(actions: PendingActionRaw[]): QueueItemView[] {
  const items: QueueItemView[] = []
  for (const action of actions) {
    const kind = ACTION_QUEUE_KIND[action.kind]
    if (!kind || !action.action_id) continue
    const payload = action.payload ?? {}
    const ticket = asText(payload.ticket_id)
    const entity = asText(payload.entity_id)
    const target = action.target_ref ?? ''
    const reason = asText(payload.reason)
    const workOrderId = asText(payload.work_order_id)
    const title = action.kind === 'question'
      ? asText(payload.question) || '主 Agent 的提问'
      : action.kind === 'work_order' || action.kind === 'human_decision' ? reason || (
        action.kind === 'work_order' ? `操作工单 ${workOrderId || action.action_id}` : `人工待办 ${shortId(target) || action.action_id}`)
        : action.kind === 'correction' ? `纠正 ${shortId(ticket || target) || '事件单'} 的低置信判断`
          : action.kind === 'sampling' || action.kind === 'review' || action.kind === 'review_decision'
            ? `核对 ${shortId(ticket || target) || '事件单'} 的处置`
            : action.kind === 'publish' ? `发布 ${shortId(target) || action.action_id}`
              : `批准计划 ${shortId(target) || action.action_id}`
    const actions = Array.isArray(payload.actions) ? payload.actions as Record<string, unknown>[] : []
    // payload.actions 是确认后才执行的处置动作；已执行过的保护动作只有 payload 带字段时才有
    const protections = Array.isArray(payload.protection_actions)
      ? (payload.protection_actions as Record<string, unknown>[])
      : []
    const at = action.proposed_at ? Date.parse(action.proposed_at) : NaN
    items.push({
      id: `action:${action.action_id}`,
      kind,
      title,
      subtitle: `真实接口 · ${action.kind}${ticket ? ` · ${shortId(ticket)}` : ''}`,
      detail: reason || `后端登记了 ${action.kind} 待确认动作，确认后由后端执行函数处理。`,
      facts: [
        { label: '对象', value: entity ? (spotOf(entity) ?? shortId(entity)) : '—' },
        { label: '事件单', value: ticket ? shortId(ticket) : '—' },
        { label: '编号', value: action.action_id },
      ],
      notes: [
        `动作类型 ${action.kind}`,
        `状态 ${action.status}`,
        action.target_ref ? `目标 ${action.target_ref}` : '没有目标引用',
      ],
      source: `待确认动作 ${action.action_id}`,
      at: Number.isFinite(at) ? at : 0,
      entityId: entity || null,
      actionId: action.action_id,
      revision: action.revision,
      backendKind: action.kind,
      payload,
      workOrderSource: kind === 'work_order' ? workOrderSourceOf(reason) : undefined,
      disposalActions: kind === 'work_order' && actions.length ? actions.map(pendingActionLabel) : undefined,
      protections: kind === 'work_order' && protections.length ? protections.map(pendingActionLabel) : undefined,
      verdict: null,
    })
  }
  return items
}

/** 与当日运行有关、且此刻仍在执行窗口里的动作。 */
export function activeActions(day: Day, cursor: number): ConsoleActionView[] {
  const selected = new Set<string>()
  for (const decision of day.decisions) {
    if (time(decision.at) > cursor) continue
    for (const id of decision.result.selected_proposal_ids) selected.add(id)
  }
  const rows: ConsoleActionView[] = []
  for (const proposal of day.proposals) {
    if (proposal.proposal_id && !selected.has(proposal.proposal_id)) continue
    for (const action of proposal.actions ?? []) {
      const startedAt = time(action.interval.start)
      const endsAt = time(action.interval.end)
      if (startedAt > cursor || endsAt <= cursor) continue
      const spot = spotOf(action.entity_id) ?? 'factory'
      const params = (action.params ?? {}) as Record<string, unknown>
      const power = Number(params.kw)
      rows.push({
        key: `${action.entity_id}-${action.command}-${action.interval.start}`,
        label: describeAction({ entityId: action.entity_id, command: action.command, params, start: startedAt, end: endsAt }),
        title: actionTitle({ entityId: action.entity_id, command: action.command, params, start: startedAt, end: endsAt }),
        detail: Number.isFinite(power) && power > 0 ? `${power} kW · 至 ${fmtClock(endsAt, day.timezone)}` : `至 ${fmtClock(endsAt, day.timezone)}`,
        entityId: action.entity_id,
        command: action.command,
        params,
        spot,
        startedAt,
        endsAt,
        retracted: false,
        impact: '撤销后这条指令立即停止，设备回到执行前状态',
      })
    }
  }
  return rows
}

export function queueItems(day: Day, cursor: number, verdicts: Record<string, Verdict>, watchdog: WatchdogTicket[]): QueueItemView[] {
  const items: QueueItemView[] = []
  for (const decision of day.decisions) {
    if (time(decision.at) > cursor || decision.result.outcome !== 'to_human') continue
    const id = `decision:${decision.decision_id}`
    const scores = Object.entries(decision.conversion.scores ?? {}).map(([key, value]) => `${key} ${money(value)}`)
    items.push({
      id,
      kind: 'approve',
      title: firstName(decision.result.summary),
      subtitle: `主 Agent ${fmtClock(time(decision.at), day.timezone)} 提出 · 需值班批准`,
      detail: `${decision.conflict.summary}${decision.conversion.notes?.length ? `（${decision.conversion.notes.join('；')}）` : ''}`,
      facts: [
        { label: '裁决层级', value: `L${decision.layer}` },
        { label: '换算得分', value: scores.length ? scores.join(' / ') : '—' },
        { label: '到期后', value: '按预案自动恢复' },
      ],
      notes: [
        `冲突：${decision.conflict.summary}`,
        ...(decision.conversion.notes ?? []),
        `政策 ${decision.conversion.policy_version}`,
      ],
      source: `裁决 ${decision.decision_id.split('/').pop() ?? decision.decision_id}`,
      at: time(decision.at),
      entityId: null,
      actionId: null,
      verdict: verdicts[id] ?? null,
    })
  }
  for (const order of day.work_orders) {
    if (time(order.created_at) > cursor || order.status !== 'proposed') continue
    const id = `work_order:${order.work_order_id}`
    const action = order.actions?.[0]
    const demoProtections = DEMO_PROTECTIONS[order.work_order_id] ?? []
    items.push({
      id,
      kind: 'work_order',
      title: order.reason.replace(/（合成演示）$/, ''),
      subtitle: `操作工单 ${order.work_order_id} · ${order.pending_action_id ? '演示记录，等确认' : '等确认'}`,
      detail: order.reason,
      facts: [
        { label: '对象', value: action ? (spotOf(action.entity_id) ?? action.entity_id) : '—' },
        { label: '建议窗口', value: action ? `${fmtClock(time(action.interval.start), day.timezone)}～${fmtClock(time(action.interval.end), day.timezone)}` : '—' },
        { label: '事件单', value: order.ticket_id.split('/').pop() ?? order.ticket_id },
      ],
      notes: [
        `事件单 ${order.ticket_id.split('/').pop() ?? order.ticket_id} 派出的操作工单`,
        action ? `建议动作：${describeAction({ entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0 })}` : '工单里没有建议动作',
        // 工单记录里的待确认动作编号是演示数据：没有后端可确认，处理只记在处置记录；真实动作以 /actions 为准
        order.pending_action_id
          ? `记录里的待确认动作编号 ${order.pending_action_id}（演示数据，不可直接确认；真实动作以 /actions 为准）`
          : null,
        '确认到场后由值班人员回填处置结果',
      ].filter((note): note is string => Boolean(note)),
      source: `操作工单 ${order.work_order_id}`,
      at: time(order.created_at),
      entityId: action?.entity_id ?? null,
      actionId: null,
      synthetic: Boolean(order.pending_action_id),
      workOrderSource: workOrderSourceOf(order.reason),
      disposalActions: (order.actions ?? []).map(item => `${spotOf(item.entity_id) ?? item.entity_id} · ${item.command}`),
      protections: demoProtections.length ? demoProtections : undefined,
      protectionsSynthetic: demoProtections.length > 0,
      verdict: verdicts[id] ?? null,
    })
  }
  for (const ticket of watchdog) {
    const id = `watchdog:${ticket.ticket_id}`
    items.push({
      id,
      kind: 'work_order',
      title: `Watchdog 受阻：${ticket.reason}`,
      subtitle: `事件单 ${ticket.ticket_id} · ${ticket.ticket_status}`,
      detail: `${ticket.recovery}${ticket.checked_at ? `（最近检查 ${ticket.checked_at}）` : ''}`,
      facts: [
        { label: '状态', value: ticket.ticket_status },
        { label: '原因', value: ticket.reason },
        { label: '恢复', value: ticket.recovery },
      ],
      notes: [`Watchdog 已连续检查：${ticket.recovery}`, `事件单状态 ${ticket.ticket_status}`],
      source: `Watchdog · ${ticket.ticket_id}`,
      at: cursor,
      entityId: null,
      actionId: null,
      workOrderSource: 'other',
      verdict: verdicts[id] ?? null,
    })
  }
  for (const decision of day.decisions) {
    const at = time(decision.at)
    if (at > cursor || cursor - at > 30 * 60_000) continue
    if (decision.result.outcome !== 'executed' || decision.layer > 2) continue
    const id = `review:${decision.decision_id}`
    items.push({
      id,
      kind: 'review',
      title: `核对 ${fmtClock(at, day.timezone)} 快通道处置${decision.round > 1 ? `（第 ${decision.round} 轮）` : ''}`,
      subtitle: `回执 ${decision.result.selected_proposal_ids.length} 条 · ${decision.result.summary.split(/[；;，,。]/)[0]}`,
      detail: decision.result.summary,
      facts: [
        { label: '裁决层级', value: `L${decision.layer}` },
        { label: '政策', value: decision.conversion.policy_version },
        { label: '换算得分', value: Object.entries(decision.conversion.scores ?? {}).map(([key, value]) => `${key} ${money(value)}`).join(' / ') || '—' },
      ],
      notes: [...(decision.conversion.notes ?? []), `预演与实盘：${(decision.conversion.notes ?? []).some(note => note.includes('预演')) ? '现场重算后与预演一致' : '这次没有预演记录'}`],
      source: `裁决 ${decision.decision_id.split('/').pop() ?? decision.decision_id}`,
      at,
      entityId: null,
      actionId: null,
      verdict: verdicts[id] ?? null,
    })
  }
  for (const version of day.knowledge_versions) {
    const at = time(version.activated_at)
    if (at <= cursor || at > cursor + 2 * 3600_000) continue
    const id = `publish:v${version.version}`
    items.push({
      id,
      kind: 'publish',
      title: `发布知识 v${version.version}`,
      subtitle: `${version.version_manifest_id} · ${fmtClock(at, day.timezone)} 生效`,
      detail: version.summary ?? '没有说明',
      facts: [
        { label: '版本', value: `v${version.version}` },
        { label: '清单', value: version.version_manifest_id },
      ],
      notes: [version.summary ?? '没有说明', `清单 ${version.version_manifest_id}`, '发布后有 2 小时回滚窗口'],
      source: `知识版本 v${version.version}`,
      at,
      entityId: null,
      actionId: null,
      verdict: verdicts[id] ?? null,
    })
  }
  // B35 演示补编（synthetic）：低置信纠错、主 Agent 提问与重大损失操作工单，回放头在任何时刻都能看到这三类卡片。
  const hasTicket = (suffix: string) => day.tickets.some(ticket => ticket.ticket_id.endsWith(suffix))
  if (hasTicket('tk-0005')) {
    const id = 'correction:ticket://tk-0005'
    items.push({
      id,
      kind: 'correction',
      title: '照明与空压同时异常，判为慢通道（低置信）',
      subtitle: '事件单 tk-0005 · 慢通道 · 低置信事件',
      detail: '照明与空压同时异常，没有匹配规则，进入慢通道。主 Agent 的多跳追溯结论需要值班用一句话纠正。',
      facts: [
        { label: '事件单', value: 'tk-0005' },
        { label: '通道', value: '慢通道' },
        { label: '置信', value: '低（未达复核线）' },
      ],
      notes: [
        '慢通道：多跳追溯 配电回路 → 照明 → 空压',
        '纠正会沉淀为候选规则，回测通过后发布',
        '标准演示场景中的人工纠错示例',
      ],
      source: '事件单 tk-0005',
      at: cursor,
      entityId: null,
      actionId: null,
      synthetic: true,
      verdict: verdicts[id] ?? null,
    })
  }
  if (hasTicket('tk-0004')) {
    const id = 'question:ticket://tk-0004'
    items.push({
      id,
      kind: 'question',
      title: '货梯困人：轿厢里还有人吗？',
      subtitle: '主 Agent 提问 · 事件单 tk-0004 · 等回答',
      detail: '救援方案取决于轿厢内是否有人。主 Agent 只问这一个问题，回答后继续本轮调查。',
      facts: [
        { label: '事件单', value: 'tk-0004' },
        { label: '提问方', value: '主 Agent' },
        { label: '状态', value: '待回答' },
      ],
      notes: [
        '回答内容提交后将同步回写至主 Agent 上下文',
        '标准演示场景中的人机协同提问示例',
      ],
      source: '主 Agent · 现场确认',
      at: cursor,
      entityId: null,
      actionId: null,
      synthetic: true,
      verdict: verdicts[id] ?? null,
    })
  }
  if (hasTicket('tk-0003')) {
    const id = 'work_order:wo-b47-major-loss'
    items.push({
      id,
      kind: 'work_order',
      title: '试算损失 ¥62,000 超重大损失线，停机检修',
      subtitle: '操作工单 wo-b47-major-loss · 重大损失 · 等确认',
      detail: '设备监测试算：空压机停机检修预计损失 ¥62,000，超过重大损失阈值 ¥50,000，按安全分级规则派发重大损失类操作工单。',
      facts: [
        { label: '来源', value: '重大损失（试算）' },
        { label: '试算损失', value: '¥62,000' },
        { label: '事件单', value: 'tk-0003' },
      ],
      notes: [
        '试算损失 ¥62,000 > 重大损失线 ¥50,000（configured）',
        '派单前预案已自动执行保护动作',
        '确认=值班到场确认，回填处置结果',
      ],
      source: '操作工单 wo-b47-major-loss',
      at: cursor,
      entityId: 'entity://demo-electronics/compressor/AIR-1',
      actionId: null,
      synthetic: true,
      workOrderSource: 'major_loss',
      disposalActions: ['空压机 AIR-1 · open_work_order（停机检修，确认后才下发）'],
      protections: ['储能放电 320 kW 支撑母线电压（预案）', '空压站旁通阀保持供气，避免全线停产（预案）'],
      protectionsSynthetic: true,
      verdict: verdicts[id] ?? null,
    })
  }
  const order = (kind: QueueItemView['kind']) => KIND_ORDER.indexOf(kind)
  return items.sort((a, b) => order(a.kind) - order(b.kind) || a.at - b.at)
}

/** 泳道：每次工具调用一个块；没有 started_at / duration_ms 的用运行耗时推算并标“推算”。 */
export function laneViews(day: Day, cursor: number): LaneView[] {
  const runs = day.agent_runs.filter(run => {
    const trigger = run.binding ? time(run.binding.trigger_at) : run.timing ? time(run.timing.trigger_at) : null
    if (trigger === null) return false
    return trigger <= cursor && cursor - trigger <= 45 * 60_000
  })
  const selectedIds = new Set(day.decisions.filter(decision => time(decision.at) <= cursor).flatMap(decision => decision.result.selected_proposal_ids))
  const lanes = new Map<string, LaneView>()
  for (const run of runs) {
    // 泳道时间轴用场景触发时刻（binding.trigger_at）铺开，工具调用的 started_at 只用来算相对偏移与耗时。
    const trigger = time(run.binding?.trigger_at ?? run.timing?.trigger_at ?? new Date(cursor).toISOString())
    const requested = run.timing?.requested_at ? time(run.timing.requested_at) : trigger
    const latency = Number(run.timing?.latency_ms ?? 0)
    const calls = run.tool_calls ?? []
    const steps: LaneStepView[] = calls.length
      ? calls.map((call, index) => {
        const calledAt = call.started_at ? time(call.started_at) : null
        const estimated = calledAt === null || call.duration_ms == null
        const offset = calledAt === null ? index * 1000 : calledAt - requested
        const durationMs = call.duration_ms ?? Math.max(0, latency - offset)
        const conflict = call.tool === 'resolve_conflict' || call.tool === '冲突裁决'
        const compare = conflict
          ? proposalsOfTicket(day, run.binding?.ticket_id)
            .filter(proposal => proposal.binding && time(proposal.binding.trigger_at) <= cursor)
            .map(proposal => ({
              label: proposal.actions?.[0]
                ? describeAction({
                  entityId: proposal.actions[0].entity_id, command: proposal.actions[0].command,
                  params: proposal.actions[0].params, start: 0, end: 0,
                })
                : proposal.expert,
              note: `${money(proposal.cost.cny)} · 置信 ${proposal.confidence.toFixed(2)}`,
              chosen: proposal.proposal_id ? selectedIds.has(proposal.proposal_id) : false,
            }))
          : []
        return {
          id: call.call_id,
          label: toolLabel(call.tool),
          tool: call.tool,
          start: trigger + Math.max(0, offset),
          durationMs,
          status: call.business_status === 'running' ? 'running' : 'done',
          estimated,
          input: call.summary ?? '—',
          compare,
          result: call.summary ?? '—',
        }
      })
      : [{
        id: `${run.agent_run_id}-run`,
        label: /main/i.test(run.agent_name) ? '本次运行' : /watchdog/i.test(run.agent_name) ? '核对回执' : '专家提案',
        tool: /main/i.test(run.agent_name) ? '本次运行' : /watchdog/i.test(run.agent_name) ? '核对回执' : '专家提案',
        start: trigger,
        durationMs: latency || 0,
        // 契约里没有 running 状态，watchdog 的“进行中”按 agent 名判断，别改回合法的 finished 后显示成已完成。
        status: /watchdog/i.test(run.agent_name) ? 'running' : 'done',
        estimated: true,
        input: run.final_answer ?? '—',
        compare: [],
        result: /watchdog/i.test(run.agent_name) ? '进行中' : seconds(latency),
      }]
    const agent = run.agent_name
    const lane = lanes.get(agent) ?? { agent, label: EXPERT_LABEL[agent] ?? agent, steps: [] }
    lane.steps.push(...steps)
    lanes.set(agent, lane)
  }
  return [...lanes.values()].sort((a, b) => {
    const index = (agent: string) => { const at = LANE_ORDER.indexOf(agent); return at < 0 ? LANE_ORDER.length : at }
    return index(a.agent) - index(b.agent)
  })
}

/** 事件流：工具调用与原始事件混排，点一行定位到泳道里的块。 */
export function streamRows(day: Day, cursor: number, lanes: LaneView[]): StreamRow[] {
  const steps = new Map<string, LaneStepView>()
  for (const lane of lanes) for (const step of lane.steps) steps.set(step.id, step)
  const rows: StreamRow[] = []
  for (const lane of lanes) {
    for (const step of lane.steps) {
      if (step.start > cursor) continue
      rows.push({
        id: `step:${step.id}`,
        at: step.start,
        type: step.status === 'running' ? 'tool_running' : 'tool_result',
        agent: lane.label,
        summary: step.input,
        stepId: step.id,
      })
    }
  }
  for (const event of day.events) {
    const at = time(event.at)
    if (at > cursor || cursor - at > 3 * 3600_000) continue
    rows.push({ id: `event:${event.event_id}`, at, type: event.event_type, agent: '事件', summary: event.summary, stepId: null })
  }
  return rows.sort((a, b) => b.at - a.at).slice(0, 12)
}

/** 告警：事件 + 处置状态。 */
export function alertViews(day: Day, cursor: number): AlertView[] {
  const handled = new Map<string, string>()
  for (const decision of day.decisions) {
    if (time(decision.at) > cursor) continue
    if (decision.result.outcome === 'executed') handled.set(decision.ticket_id, '已自动处置')
    if (decision.result.outcome === 'work_order' || decision.result.outcome === 'to_human') handled.set(decision.ticket_id, '已知会值班')
  }
  for (const ticket of day.tickets) {
    if (ticket.closed_at && time(ticket.closed_at) <= cursor) handled.set(ticket.ticket_id, '已闭环')
  }
  return day.events
    .filter(event => time(event.at) <= cursor && cursor - time(event.at) <= 3 * 3600_000)
    .sort((a, b) => time(b.at) - time(a.at))
    .slice(0, 6)
    .map(event => {
      const ticket = day.tickets.find(item => item.event_ids?.includes(event.event_id))
      return {
        id: event.event_id,
        at: time(event.at),
        title: event.summary,
        state: (ticket && handled.get(ticket.ticket_id)) ?? '待处理',
        severity: event.severity ?? 1,
      }
    })
}

/** 能源策略与后续计划：没有现成字段，从提案动作和电价推出来。 */
export function energyState(day: Day, cursor: number, actions: ConsoleActionView[]): EnergyState {
  const minutes = day.ledger_minutes.filter(minute => time(minute.interval.end) <= cursor)
  const minute = minutes[minutes.length - 1]
  const price = minute?.price_cny_per_kwh ?? null
  const tier = price === null ? '—' : price >= 0.9 ? '峰段' : price >= 0.6 ? '平段' : '谷段'
  const devices = devicesAt(day, cursor)
  const battery = Object.entries(devices).find(([id]) => spotOf(id) === 'battery')?.[1]
  const soc = Number(battery?.soc)
  const discharge = minute ? minute.discharge_kwh * 60 : null
  const charge = minute ? minute.charge_kwh * 60 : null
  const power = discharge === null || charge === null ? null : discharge - charge
  const batteryAction = actions.find(action => spotOf(action.entityId) === 'battery')
  const strategy = batteryAction
    ? `${batteryAction.label.split(' ')[0]}，至 ${fmtClock(batteryAction.endsAt, day.timezone)}`
    : '按日前计划待机'

  const future = day.ledger_minutes
    .filter(item => time(item.interval.start) >= cursor)
    .sort((a, b) => time(a.interval.start) - time(b.interval.start))
  const plan: { at: string; text: string }[] = []
  const priceChange = future.find(item => item.price_cny_per_kwh !== price)
  if (priceChange) {
    const next = priceChange.price_cny_per_kwh ?? 0
    plan.push({ at: fmtClock(time(priceChange.interval.start), day.timezone), text: `电价转${next >= 0.9 ? '峰段' : next >= 0.6 ? '平段' : '谷段'}` })
  }
  const pvPeak = [...future].sort((a, b) => b.pv_available_kwh - a.pv_available_kwh)[0]
  if (pvPeak && pvPeak.pv_available_kwh > 0) plan.push({ at: fmtClock(time(pvPeak.interval.start), day.timezone), text: '光伏高发' })
  const nextBattery = day.proposals
    .flatMap(proposal => (proposal.actions ?? []).filter(action => spotOf(action.entity_id) === 'battery'))
    .map(action => ({ action, at: time(action.interval.start) }))
    .filter(item => item.at > cursor)
    .sort((a, b) => a.at - b.at)[0]
  if (nextBattery) plan.push({ at: fmtClock(nextBattery.at, day.timezone), text: describeAction({ entityId: nextBattery.action.entity_id, command: nextBattery.action.command, params: nextBattery.action.params, start: 0, end: 0 }) })

  const capacity = day.metrics.find(metric => metric.metric === 'contract_capacity_kw' && metric.group === 'config')?.value ?? null
  return {
    strategy,
    price: price === null ? '—' : money(price),
    priceTier: tier,
    soc: Number.isFinite(soc) ? `${Math.round(soc * 100)}%` : '—',
    power: power === null ? '—' : `${power > 0 ? '−' : power < 0 ? '+' : ''}${Math.abs(Math.round(power))} kW`,
    plan: plan.slice(0, 3),
    load: minute
      ? {
        total: Math.round(minute.load_kwh * 60),
        grid: Math.round(minute.grid_import_kwh * 60),
        pv: Math.round(minute.pv_used_kwh * 60),
        storage: Math.round(minute.discharge_kwh * 60),
        capacity,
      }
      : null,
  }
}

/** T1 上限口径统一在 shared/readings，控制台（Wall）仍从 console/data 取。 */
export { transformerLimit } from '../shared/readings'

/** 一屏一眼能看懂的厂区模型：实体状态 + 三个读数（读数口径统一在 shared/readings）。 */
export function campusOf(day: Day, cursor: number) {
  const devices = devicesAt(day, cursor)
  return buildCampus({
    at: cursor,
    events: day.events
      .filter(event => time(event.at) <= cursor && cursor - time(event.at) <= 20 * 60_000 && Boolean(event.entity_id))
      .map(event => ({ at: time(event.at), entityId: event.entity_id, summary: event.summary, eventType: event.event_type })),
    actions: activeActions(day, cursor).map(action => ({
      entityId: action.entityId, command: action.command, params: action.params, start: action.startedAt, end: action.endsAt,
    })),
    devices,
    readings: campusReadings(day, cursor),
  })
}

export function loadPoints(day: Day): LoadPoint[] {
  return day.state_series
    .flatMap(frame => {
      const transformer = Object.entries(frame.devices).find(([id]) => spotOf(id) === 'transformer')
      const ratio = Number((transformer?.[1] as Record<string, unknown> | undefined)?.load_ratio)
      // 帧内的逐分钟变化也画进曲线：展示页这样画，控制台同一份样例要同口径。
      const changes = (frame.changes ?? [])
        .filter(change => change.entity_id === transformer?.[0] && change.field === 'load_ratio')
        .map(change => ({ at: time(change.at), value: Number(change.value) * 100 }))
      return [...(Number.isFinite(ratio) ? [{ at: time(frame.at), value: ratio * 100 }] : []), ...changes]
    })
    .filter(point => Number.isFinite(point.value))
    .sort((a, b) => a.at - b.at)
}

/** 顶栏“数据延迟”：最后一个状态帧或帧内 change 距回放头的秒数。 */
export function dataDelaySeconds(day: Day, cursor: number): number | null {
  const frame = day.state_series.filter(item => time(item.at) <= cursor).slice(-1)[0]
  if (!frame) return null
  let last = time(frame.at)
  for (const change of frame.changes ?? []) {
    const at = time(change.at)
    if (at <= cursor && at > last) last = at
  }
  return Math.max(0, Math.round((cursor - last) / 1000))
}

export function buildConsoleModel(day: Day, cursor: number, source: string, state: {
  playing: boolean; paused: boolean; pausedAt: number | null; mode: 'replay' | 'live'
  verdicts: Record<string, Verdict>; retracted: string[]; cancelledRules: string[]
}, watchdog: WatchdogTicket[] = []): ConsoleModel {
  const lanes = laneViews(day, cursor)
  const queue = queueItems(day, cursor, state.verdicts, watchdog)
  const actions = activeActions(day, cursor).map(action => ({ ...action, retracted: state.retracted.includes(action.key) }))
  const rules: RuleView[] = day.temp_rules
    .filter(rule => time(rule.effective.start) <= cursor && time(rule.effective.end) > cursor)
    .map(rule => ({
      id: rule.rule_id,
      text: rule.text,
      until: fmtClock(time(rule.effective.end), day.timezone),
      cancelled: state.cancelledRules.includes(rule.rule_id),
    }))
  const live = queue.filter(item => !item.verdict)
  const counts = {
    approve: live.filter(item => item.kind === 'approve').length,
    work_order: live.filter(item => item.kind === 'work_order').length,
    review: live.filter(item => item.kind === 'review').length,
    correction: live.filter(item => item.kind === 'correction').length,
    question: live.filter(item => item.kind === 'question').length,
    publish: live.filter(item => item.kind === 'publish').length,
    pending: live.length,
  }
  // 没有 binding 的主运行也按回放头过滤：只认仿真触发时刻（binding / timing 的 trigger_at）；工具调用的 started_at 是墙钟，不用；都没有才不过滤。
  const runAt = (run: Day['agent_runs'][number]) => {
    const iso = run.binding?.trigger_at ?? run.timing?.trigger_at
    return iso ? time(iso) : null
  }
  const mainRun = [...day.agent_runs].reverse().find(run => /main/i.test(run.agent_name)
    && (runAt(run) ?? Number.NEGATIVE_INFINITY) <= cursor)
  const latestDecision = [...day.decisions].filter(decision => time(decision.at) <= cursor).slice(-1)[0]
  // 运行通道按 agent_run_id → binding(ticket, round) → 该轮 decision 判层，不再按触发时间猜（#239 方案 3）。
  const binding = mainRun?.binding
  const runDecision = binding
    ? day.decisions
      .filter(decision => decision.ticket_id === binding.ticket_id && decision.round === binding.round
        && time(decision.at) <= cursor)
      .sort((a, b) => time(a.at) - time(b.at))[0]
    : undefined
  const rehearsed = latestDecision && (latestDecision.conversion.notes ?? []).some(note => note.includes('预演'))
  const start = dayStart(day)
  const end = dayEnd(day)
  return {
    day,
    cursor,
    now: fmtClock(cursor, day.timezone),
    playing: state.playing,
    paused: state.paused,
    pausedAt: state.pausedAt,
    mode: state.mode,
    source,
    progress: state.mode === 'replay' ? Math.min(1, Math.max(0, (cursor - start) / Math.max(1, end - start))) : null,
    campus: campusOf(day, cursor),
    loadPoints: loadPoints(day),
    actions,
    rules,
    queue,
    counts,
    lanes,
    stream: streamRows(day, cursor, lanes),
    alerts: alertViews(day, cursor),
    energy: energyState(day, cursor, actions),
    run: mainRun
      ? {
        id: mainRun.agent_run_id,
        trigger: binding ? fmtClock(time(binding.trigger_at), day.timezone) : '—',
        channel: runDecision ? (runDecision.layer <= 2 ? '快通道' : '慢通道') : '—',
        elapsed: mainRun.timing?.latency_ms ? runSeconds(mainRun.timing.latency_ms) : '进行中',
      }
      : null,
    rehearsal: rehearsed
      ? { text: '实际处置与预演一致，所以走了快通道', ok: true }
      : latestDecision ? { text: '这次处置没有对应的预演记录', ok: false } : null,
  }
}

/** 控制台三块屏共用的取数入口。 */
export function useConsole(watchdog: WatchdogTicket[] = []): ConsoleModel {
  const { day, cursor, source, playing, paused, pausedAt, mode, verdicts, retracted, cancelledRules } = useDay()
  return useMemo(
    () => buildConsoleModel(day, cursor, source, { playing, paused, pausedAt, mode, verdicts, retracted, cancelledRules }, watchdog),
    [day, cursor, source, playing, paused, pausedAt, mode, verdicts, retracted, cancelledRules, watchdog],
  )
}


