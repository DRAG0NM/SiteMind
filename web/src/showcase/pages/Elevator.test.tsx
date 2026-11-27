// @vitest-environment jsdom
// 电梯页故障列：真实导出的 fault 是布尔值，旧样例用非空文本描述故障，两种都要兼容。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from '../data/demoDay'
import { dayAtHour } from '../data/time'
import type { Day } from '../data/types'
import { ElevatorPage } from './Elevator'

const PROD = 'entity://demo-electronics/elevator_group/EG-PROD'
const ADMIN = 'entity://demo-electronics/elevator_group/EG-ADMIN'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
})

const noop = () => { /* 测试里不开抽屉 */ }

const row = (label: string) => {
  const found = [...container.querySelectorAll('tbody tr')].find(item => item.textContent?.includes(label))
  if (!found) throw new Error(`没有找到「${label}」这一行`)
  return found
}

/** 只保留一帧电梯设备快照，避免内置样例其他时刻的 fault 干扰断言。 */
function dayWith(prod: Record<string, unknown>, admin: Record<string, unknown> = {
  running: 5, queue: 6, zone_operation: false, fault: null,
}): Day {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  day.state_series = [{
    at: new Date(dayAtHour(day, 0)).toISOString(),
    devices: { [PROD]: prod, [ADMIN]: admin },
    changes: [],
  }]
  return day
}

const render = async (day: Day) => {
  await act(async () => root.render(
    <ElevatorPage day={day} cursor={dayAtHour(day, 10)} openEvidence={noop} />,
  ))
}

it('布尔 fault=false 不显示故障，也不因此标为忙', async () => {
  await render(dayWith({ running: 4, queue: 6, fault: false }))
  const prod = row('生产楼货梯')
  expect(prod.textContent).not.toContain('故障')
  expect(prod.textContent).not.toContain('false')
  expect(prod.className).toBe('')
})

it('布尔 fault=true 显示故障并标为忙，不把 true 当描述', async () => {
  await render(dayWith({ running: 4, queue: 6, fault: true }))
  const prod = row('生产楼货梯')
  expect(prod.textContent).toContain('故障')
  expect(prod.textContent).not.toContain('true')
  expect(prod.className).toBe('on')
})

it('非空文本故障描述保持原来的显示', async () => {
  await render(dayWith({ running: 4, queue: 6, fault: 'car-2-between-floors' }))
  const prod = row('生产楼货梯')
  expect(prod.textContent).toContain('故障 · car-2-between-floors')
  expect(prod.className).toBe('on')
})

it('排队人数达到阈值仍标为忙，且不误报故障', async () => {
  await render(dayWith({ running: 4, queue: 42, fault: false }))
  const prod = row('生产楼货梯')
  expect(prod.className).toBe('on')
  expect(prod.textContent).not.toContain('故障')
})
