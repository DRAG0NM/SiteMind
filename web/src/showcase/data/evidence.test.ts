import { expect, it } from 'vitest'
import { decisionChain, evidenceFor } from './evidence'
import { sampleDay } from './loadDay'
import { dayAtHour } from './time'
import type { Day } from './types'

const at = (day: Day, hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
const T1 = 'entity://demo-electronics/transformer/T1'

it('T1 晚高峰的依据取当晚储能提案，不会错配到早高峰事件单', () => {
  const day = sampleDay()
  const evidence = evidenceFor(day, at(day, 19, 40), T1, 'T1 负载与储能动作')

  expect(evidence.title).toContain('储能放电')
  expect(evidence.subtitle).toContain('能源专家提案')
  expect(evidence.experts.map(item => item.expert)).toEqual(['energy'])
  // 早高峰事件单的运行记录不能出现在晚高峰的依据里。
  expect(evidence.runs.some(run => run.ticket === 'tk-0001')).toBe(false)
})

it('回拖到储能提案生效前，T1 的依据退回当天早些时候仍可见的提案', () => {
  const day = sampleDay()
  const evidence = evidenceFor(day, at(day, 19, 20), T1, 'T1 负载与储能动作')

  expect(evidence.title).not.toContain('储能放电')
  expect(evidence.experts.some(item => item.expert === 'elevator')).toBe(true)
})

it('早高峰 08:10 的 T1 依据仍是早高峰事件单', () => {
  const day = sampleDay()
  const evidence = evidenceFor(day, at(day, 8, 10), T1, 'T1 负载与储能动作')

  expect(evidence.experts.map(item => item.expert)).toContain('elevator')
  expect(evidence.runs.every(run => run.ticket === 'tk-0001')).toBe(true)
})

it('按 ID 查找先查当日、再查 context：跨日提案与事件单事件都能补上，运行时刻用场景触发时刻', () => {
  const day = sampleDay()
  const cursor = at(day, 8, 10)
  const ticketId = 'ticket://tk-0001'
  const ticket = day.tickets.find(item => item.ticket_id === ticketId)!
  const eventIds = new Set(ticket.event_ids ?? [])
  const crossDayEvents = day.events.filter(event => eventIds.has(event.event_id))
  const crossDayProposal = day.proposals.find(item => item.proposal_id === 'proposal://pp-demo-hvac')!
  day.events = day.events.filter(event => !eventIds.has(event.event_id))
  day.proposals = day.proposals.filter(item => item.proposal_id !== crossDayProposal.proposal_id)
  day.context = { events: crossDayEvents, proposals: [crossDayProposal] }

  const evidence = evidenceFor(day, cursor, null, '跨日依据', ticketId)

  // 事件单的事件从 context 按 event_id 找回，早高峰触发时刻仍是 08:05。
  expect(evidence.subtitle).toContain('08:05')
  // 跨日提案按 proposal_id / ticket_id 补上，依据里的原文引用不悬空。
  expect(evidence.experts.some(item => item.expert === 'hvac')).toBe(true)
  // 场景触发时刻与耗时分开显示，不拿墙钟 requested_at 顶替。
  expect(evidence.runs[0].trigger).toBe('08:05')
  expect(evidence.runs[0].latency).toMatch(/秒$/)
})

it('时间轴只用当日小节：context 里的跨日记录不参与按设备的依据选择', () => {
  const day = sampleDay()
  const cursor = at(day, 8, 10)
  const base = evidenceFor(day, cursor, T1, 'T1 负载与储能动作')
  const crossDay = { ...day.proposals.find(item => item.proposal_id === 'proposal://pp-demo-elev')!, proposal_id: 'proposal://context-elev', ticket_id: 'ticket://other-day' }
  day.context = { proposals: [crossDay] }

  const after = evidenceFor(day, cursor, T1, 'T1 负载与储能动作')
  expect(after.title).toBe(base.title)
  expect(after.experts).toEqual(base.experts)
})

it('决策链里的选中提案按 ID 从 context 取，跨日提案不会被丢掉', () => {
  const day = sampleDay()
  const proposal = day.proposals.find(item => item.proposal_id === 'proposal://pp-0001')!
  day.proposals = day.proposals.filter(item => item.proposal_id !== proposal.proposal_id)
  day.context = { proposals: [proposal] }

  const chain = decisionChain(day, at(day, 8, 7))
  expect(chain?.selected.some(item => item.proposal_id === 'proposal://pp-0001')).toBe(true)
})
