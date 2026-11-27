import { expect, it } from 'vitest'
import sampleJson from '../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from '../showcase/data/demoDay'
import type { Day } from '../showcase/data/types'
import { dayAtHour, devicesAt, type Day as StoreDay } from '../store'
import { buildConsoleModel, dataDelaySeconds, laneViews, loadPoints } from './data'

const sample = sampleJson as unknown as Day
const state = {
  playing: false, paused: false, pausedAt: null, mode: 'replay' as const,
  verdicts: {}, retracted: [], cancelledRules: [],
}

it('控制台 T1 曲线纳入帧内 changes，与展示页同口径', () => {
  const day = withShowcaseDemo(structuredClone(sample)) as unknown as StoreDay
  const at = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const points = loadPoints(day)

  expect(points.find(point => point.at === at(8, 5))?.value).toBe(96)
  expect(Math.max(...points.map(point => point.value))).toBe(96)
  for (let index = 1; index < points.length; index += 1) {
    expect(points[index].at).toBeGreaterThanOrEqual(points[index - 1].at)
  }

  const device = devicesAt(day, at(8, 7))['entity://demo-electronics/transformer/T1'] as Record<string, unknown>
  expect(Math.round(Number(device.load_ratio) * 100)).toBe(96)
})

it('顶栏数据延迟把帧内 changes 也算作新数据', () => {
  const day = withShowcaseDemo(structuredClone(sample)) as unknown as StoreDay
  const cursor = dayAtHour(day, 8) + 7 * 60_000 + 30_000
  // 最后一个 change 在 08:07:00，回放头 08:07:30。
  expect(dataDelaySeconds(day, cursor)).toBe(30)
})

it('08:10 的过渡点与读数同为 90，08:15 起回到样例的 81', () => {
  const day = withShowcaseDemo(structuredClone(sample)) as unknown as StoreDay
  const at = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const points = loadPoints(day)

  expect(points.find(point => point.at === at(8, 10))?.value).toBe(90)
  expect(points.find(point => point.at === at(8, 15))?.value).toBe(81)

  const device = devicesAt(day, at(8, 10))['entity://demo-electronics/transformer/T1'] as Record<string, unknown>
  expect(Math.round(Number(device.load_ratio) * 100)).toBe(90)
})

it('运行席按 binding 的事件单与轮次取裁决判层，没有 binding 显示 —，不再按触发时间猜', () => {
  const day = withShowcaseDemo(structuredClone(sample)) as unknown as StoreDay
  const cursor = dayAtHour(day, 8) + 7 * 60_000
  const model = buildConsoleModel(day, cursor, '回归样例', state)
  // 主 Agent 绑定第 1 轮 → 08:06 的 L2 裁决，通道快；触发时刻与耗时分开显示。
  expect(model.run?.trigger).toBe('08:05')
  expect(model.run?.channel).toBe('快通道')
  expect(model.run?.elapsed).toBe('23.6 秒')

  // 同一时刻附近还有 L3 裁决，但绑定的是第 3 轮：通道只能看该轮裁决。
  const bound = withShowcaseDemo(structuredClone(sample)) as unknown as StoreDay
  bound.agent_runs.find(run => /main/i.test(run.agent_name))!.binding!.round = 3
  expect(buildConsoleModel(bound, cursor, '回归样例', state).run?.channel).toBe('慢通道')

  // 没有 binding：通道与触发时刻都写 —，不拿时间猜。
  const unbound = withShowcaseDemo(structuredClone(sample)) as unknown as StoreDay
  const main = unbound.agent_runs.find(run => /main/i.test(run.agent_name))!
  Object.assign(main, { binding: null })
  const noBinding = buildConsoleModel(unbound, cursor, '回归样例', state)
  expect(noBinding.run?.channel).toBe('—')
  expect(noBinding.run?.trigger).toBe('—')
  expect(noBinding.run?.elapsed).toBe('23.6 秒')

  // 没有 binding 也要按回放头过滤：用 timing.trigger_at（仿真时刻）；回放头在它之前主运行不出现，之后出现
  const triggerAt = dayAtHour(unbound, 12)
  Object.assign(main, { timing: { ...(main.timing ?? {}), trigger_at: new Date(triggerAt).toISOString() } })
  expect(buildConsoleModel(unbound, triggerAt - 60_000, '回归样例', state).run).toBeNull()
  expect(buildConsoleModel(unbound, triggerAt + 60_000, '回归样例', state).run?.id).toBe(main.agent_run_id)
})

it('泳道冲突比较按事件单编号补上 context 里的跨日提案', () => {
  const showcase = withShowcaseDemo(structuredClone(sample)) as unknown as Day
  const cursor = dayAtHour(showcase as unknown as StoreDay, 8) + 10 * 60_000
  const crossDay = showcase.proposals.find(item => item.proposal_id === 'proposal://pp-demo-elev')!
  showcase.proposals = showcase.proposals.filter(item => item.proposal_id !== crossDay.proposal_id)
  showcase.context = { proposals: [crossDay] }
  const day = showcase as unknown as StoreDay

  const conflict = laneViews(day, cursor)
    .flatMap(lane => lane.steps)
    .find(step => step.tool === 'resolve_conflict')
  expect(conflict?.compare).toHaveLength(4)
  const labels = (conflict?.compare ?? []).map(row => row.label)
  expect(labels.some(label => label.includes('电梯'))).toBe(true)
  expect(labels.some(label => label.includes('暖通推迟预冷'))).toBe(true)
})
