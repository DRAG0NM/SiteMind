import { expect, it } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from './demoDay'
import { readings, todayStats, transformerLoadPoints } from './rollup'
import { dayAtHour } from './time'
import type { Day } from './types'

const sample = sampleJson as unknown as Day

it('T1 全天曲线包含帧内 changes 的 96% 峰值，并按时刻排序', () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const at = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const points = transformerLoadPoints(day)

  expect(points.find(point => point.at === at(8, 5))?.value).toBe(96)
  expect(Math.max(...points.map(point => point.value))).toBe(96)
  for (let index = 1; index < points.length; index += 1) {
    expect(points[index].at).toBeGreaterThanOrEqual(points[index - 1].at)
  }
})

it('“当日累计统计”的引用资料数随回放头过滤，00:00 与事件/事件单一起为 0', () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const midnight = todayStats(day, dayAtHour(day, 0))
  expect(midnight.events).toBe(0)
  expect(midnight.tickets).toBe(0)
  expect(midnight.layer12Executed + midnight.layer3).toBe(0)
  expect(midnight.evidence).toBe(0)

  const morning = todayStats(day, dayAtHour(day, 8) + 7 * 60_000)
  expect(morning.events).toBeGreaterThan(0)
  expect(morning.evidence).toBeGreaterThan(0)
})

it('T1 读数余量按上限算：08:07 是超上限 11 个百分点，不是余量 4%', () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const transformer = readings(day, dayAtHour(day, 8) + 7 * 60_000).find(item => item.key === 'transformer')!
  expect(transformer.value).toBe('96')
  expect(transformer.note).toContain('上限 85%')
  expect(transformer.note).toContain('超上限 11 个百分点')
  expect(transformer.note).not.toContain('余量 4%')
})
