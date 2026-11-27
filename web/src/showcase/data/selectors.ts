import type { Day, Decision, KnowledgeVersion, Ticket, WorkOrder } from './types'
import { localHour, resultVisibleAt, time } from './time'
import { devicesAt as readingsDevicesAt } from '../../shared/readings'

/** 当前回放时刻生效的知识版本（按版本号取最新一次激活）。 */
export function latestKnowledge(day: Day, cursor: number): KnowledgeVersion | undefined {
  let best: KnowledgeVersion | undefined
  for (const version of day.knowledge_versions) {
    if (time(version.activated_at) > cursor) continue
    if (!best || version.version > best.version) best = version
  }
  return best
}

/** 播放条上的进化时刻标记，按激活时间排序。 */
export function knowledgeMarkers(day: Day): KnowledgeVersion[] {
  return [...day.knowledge_versions].sort((a, b) => time(a.activated_at) - time(b.activated_at))
}

/** 回放头附近仍在“发生中”的事件，供 3D 楼层变红与结论面板使用。 */
export function activeEvents(day: Day, cursor: number, windowMs = 15 * 60_000) {
  return day.events
    .filter(event => {
      const at = time(event.at)
      return at <= cursor && cursor - at <= windowMs
    })
    .sort((a, b) => time(b.at) - time(a.at))
}

/** A decision cannot precede any proposal or linked agent result it uses. */
export function decisionVisible(day: Day, decision: Decision, cursor: number): boolean {
  if (time(decision.at) > cursor) return false
  const ids = new Set(decision.conflict.proposal_ids ?? [])
  return day.proposals.filter(item => item.proposal_id && ids.has(item.proposal_id))
    .every(item => resultVisibleAt(item) <= cursor)
    && day.agent_runs.filter(run => run.proposal_ids?.some(id => ids.has(id)))
      .every(run => resultVisibleAt(run) <= cursor)
}

export function latestDecision(day: Day, cursor: number): Decision | undefined {
  let latest: Decision | undefined
  for (const decision of day.decisions) {
    if (!decisionVisible(day, decision, cursor)) continue
    if (!latest || time(decision.at) >= time(latest.at)) latest = decision
  }
  return latest
}

/** 事件单是否仍在处理：已开启，且未关闭或关闭时刻在回放头之后。 */
export function openTickets(day: Day, cursor: number): Ticket[] {
  return day.tickets.filter(ticket => {
    if (time(ticket.opened_at) > cursor) return false
    if (ticket.closed_at) return time(ticket.closed_at) > cursor
    return ticket.status !== 'closed'
  })
}

/** 用 B17 导出的 operator_confirm 事件恢复已处理工单的待确认阶段。 */
export function openWorkOrders(day: Day, cursor: number) {
  return day.work_orders.filter(order => {
    const created = time(order.created_at)
    if (created > cursor) return false
    if (order.status === 'proposed') return true
    if (order.status !== 'confirmed' && order.status !== 'done') return false
    const ticket = day.tickets.find(ticket => ticket.ticket_id === order.ticket_id)
    const confirmations = day.events.filter(event => event.event_type === 'operator_confirm'
      && ticket?.event_ids?.includes(String(event.payload?.event)) && time(event.at) >= created)
    // 旧导出缺确认记录时沿用最终状态，不猜确认时间。
    if (!confirmations.length) return false
    const confirmedAt = Math.min(...confirmations.map(event => time(event.at)))
    return cursor < confirmedAt
  }).map(order => ({ ...order, status: 'proposed' }))
}

/**
 * 转人工唯一入口：工单按 ticket_id 去重，只算 proposed（及确认前的 confirmed/done）；
 * expired/rejected 不算人工打扰；仍 waiting/blocked 的事件单无论工单状态都列一条；count 按事件单去重。
 */
export function humanHandoffs(day: Day, cursor: number): { orders: WorkOrder[]; tickets: Ticket[]; count: number } {
  const orders: WorkOrder[] = []
  const covered = new Set<string>()
  for (const order of openWorkOrders(day, cursor)) {
    if (covered.has(order.ticket_id)) continue
    covered.add(order.ticket_id)
    orders.push(order)
  }
  const tickets = openTickets(day, cursor).filter(ticket =>
    (ticket.status === 'waiting' || ticket.status === 'blocked') && !covered.has(ticket.ticket_id))
  return { orders, tickets, count: orders.length + tickets.length }
}

/** 每小时的活跃程度，供播放条按小时着色。 */
export function hourActivity(day: Day) {
  const hours = Array.from({ length: 24 }, (_, hour) => ({ hour, count: 0, maxSeverity: 0 }))
  for (const event of day.events) {
    const hour = localHour(day, time(event.at))
    if (hour < 0 || hour > 23) continue
    hours[hour].count += 1
    hours[hour].maxSeverity = Math.max(hours[hour].maxSeverity, event.severity ?? 1)
  }
  return hours
}

/** 设备快照取数统一在 shared/readings，页面与厂区图不再各自解析。 */
export function devicesAt(day: Day, cursor: number) {
  return readingsDevicesAt(day, cursor)
}

export function metricValue(day: Day, metric: string, group?: string): number | undefined {
  const found = day.metrics.find(item => item.metric === metric && (group === undefined || item.group === group))
  return found?.value ?? undefined
}

export function sumLedger(day: Day, cursor: number, field: 'load_kwh' | 'cost_cny') {
  let total = 0
  for (const minute of day.ledger_minutes) {
    if (time(minute.interval.end) > cursor) continue
    total += Number(minute[field] ?? 0)
  }
  return total
}
