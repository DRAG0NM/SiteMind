/**
 * 按 ID 查找的跨日规则（#238 方案 3）：先查当日小节，再查 context 里原样携带的跨日记录。
 * 只有明确引用某个编号时才用这里；时间轴与计数仍只读当日小节。
 *
 * 这里只要求四段数组的最小形状，展示页的 Day 和控制台 store 的 Day 都能直接传，不强制同一个 Day 类型。
 */
type ProposalLookup = { proposal_id?: string | null; ticket_id: string }
type EventLookup = { event_id: string }
type TicketLookup = { ticket_id: string; event_ids?: string[] }

export function findProposal<P extends ProposalLookup>(
  day: { proposals: P[]; context?: { proposals?: P[] } | null },
  proposalId: string | null | undefined,
): P | undefined {
  if (!proposalId) return undefined
  return day.proposals.find(item => item.proposal_id === proposalId)
    ?? day.context?.proposals?.find(item => item.proposal_id === proposalId)
}

export function findEvent<E extends EventLookup>(
  day: { events: E[]; context?: { events?: E[] } | null },
  eventId: string,
): E | undefined {
  return day.events.find(item => item.event_id === eventId)
    ?? day.context?.events?.find(item => item.event_id === eventId)
}

export function findTicket<T extends TicketLookup>(
  day: { tickets: T[]; context?: { tickets?: T[] } | null },
  ticketId: string | null | undefined,
): T | undefined {
  if (!ticketId) return undefined
  return day.tickets.find(item => item.ticket_id === ticketId)
    ?? day.context?.tickets?.find(item => item.ticket_id === ticketId)
}

/** 事件单自身的全部事件：按 event_ids 顺序，先查当日、再查 context。 */
export function eventsOfTicket<E extends EventLookup, T extends TicketLookup>(
  day: { events: E[]; tickets: T[]; context?: { events?: E[]; tickets?: T[] } | null },
  ticketId: string | null | undefined,
): E[] {
  const ticket = findTicket(day, ticketId)
  if (!ticket) return []
  return (ticket.event_ids ?? []).flatMap(eventId => findEvent(day, eventId) ?? [])
}

/** 同一事件单的提案：当日在前、context 的后补，只用来按事件单编号聚合，不进时间轴。 */
export function proposalsOfTicket<P extends ProposalLookup>(
  day: { proposals: P[]; context?: { proposals?: P[] } | null },
  ticketId: string | null | undefined,
): P[] {
  if (!ticketId) return []
  const proposals = day.proposals.filter(item => item.ticket_id === ticketId)
  for (const proposal of day.context?.proposals ?? []) {
    if (proposal.ticket_id === ticketId && !proposals.some(item => item.proposal_id === proposal.proposal_id)) {
      proposals.push(proposal)
    }
  }
  return proposals
}
