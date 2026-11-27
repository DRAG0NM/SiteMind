import { expect, it } from 'vitest'
import baseSample from '../../../../contracts/examples/day.sample.json'
import { humanHandoffs, openTickets } from './selectors'
import { dayAtHour } from './time'
import type { Day, Ticket } from './types'

const sample = baseSample as unknown as Day
const at = (hour: number, minute = 0) => dayAtHour(sample, hour) + minute * 60_000

it('事件单按 closed_at 判断历史时刻是否仍在处理', () => {
  const early = openTickets(sample, at(8, 6)).map(ticket => ticket.ticket_id)
  expect(early).toContain('ticket://tk-0001') // 08:05 开启、08:40 关闭
  const later = openTickets(sample, at(9, 0)).map(ticket => ticket.ticket_id)
  expect(later).not.toContain('ticket://tk-0001')
})

it('转人工按事件单去重：有工单的受阻单只算一次，普通开放事件单不算', () => {
  const day = structuredClone(sample)
  day.tickets.push(
    { ticket_id: 'ticket://tk-running', opened_at: new Date(at(8, 0)).toISOString(), status: 'running', round: 1 } satisfies Ticket,
    { ticket_id: 'ticket://tk-blocked', opened_at: new Date(at(14, 0)).toISOString(), status: 'blocked', round: 1 } satisfies Ticket,
  )

  const handoffs = humanHandoffs(day, at(15, 0))
  expect(handoffs.orders.map(order => order.work_order_id)).toEqual(['wo-0001'])
  expect(handoffs.tickets.map(ticket => ticket.ticket_id)).toEqual(['ticket://tk-blocked'])
  expect(handoffs.count).toBe(2)
})

it('expired 工单不算人工打扰，waiting/blocked 事件单仍按事件单去重计数', () => {
  const day = structuredClone(sample)
  day.events = []
  day.tickets = [
    { ticket_id: 'ticket://tk-a', opened_at: new Date(at(8, 0)).toISOString(), status: 'waiting', round: 1 } satisfies Ticket,
    { ticket_id: 'ticket://tk-b', opened_at: new Date(at(8, 0)).toISOString(), status: 'blocked', round: 1 } satisfies Ticket,
  ]
  day.work_orders = [
    { work_order_id: 'wo-a-expired', ticket_id: 'ticket://tk-a', created_at: new Date(at(8, 5)).toISOString(), reason: '上一轮已过期', status: 'expired' },
    { work_order_id: 'wo-b-round1', ticket_id: 'ticket://tk-b', created_at: new Date(at(8, 6)).toISOString(), reason: '第一轮工单', status: 'proposed' },
    { work_order_id: 'wo-b-round2', ticket_id: 'ticket://tk-b', created_at: new Date(at(8, 7)).toISOString(), reason: '第二轮工单', status: 'proposed' },
    { work_order_id: 'wo-b-expired', ticket_id: 'ticket://tk-b', created_at: new Date(at(8, 8)).toISOString(), reason: '过期工单', status: 'expired' },
  ]

  const handoffs = humanHandoffs(day, at(9, 0))
  expect(handoffs.count).toBe(2)
  expect(handoffs.orders).toHaveLength(1)
  expect(handoffs.orders[0].ticket_id).toBe('ticket://tk-b')
  expect(handoffs.tickets.map(ticket => ticket.ticket_id)).toEqual(['ticket://tk-a'])
})
