// @vitest-environment jsdom
// #239 验收：同一 cursor 下，总览、能源页与园区标注的 T1 / 光伏读数必须相等，且与统一读数层一致。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { buildCampus } from '../../shared/campus'
import { pv, t1 } from '../../shared/readings'
import { withShowcaseDemo } from '../data/demoDay'
import { campusInput } from '../data/rollup'
import { dayAtHour } from '../data/time'
import type { Day } from '../data/types'
import { EnergyPage } from './Energy'
import { Overview } from './Overview'

// 厂区图渲染在本用例里不参与断言，换空组件，别让它的动画拖慢。
vi.mock('../../shared/iso/IsoCampus', () => ({ IsoCampus: () => null }))

let container: HTMLDivElement
let root: Root
const noop = () => { /* 测试里不开抽屉 */ }

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

/** 按标签找读数：总览读数的值是 TweenNumber 渲染的纯文本。 */
function readingValue(label: string): string | undefined {
  for (const row of container.querySelectorAll('.reading')) {
    if (row.querySelector('.reading-head .lb')?.textContent?.trim() !== label) continue
    return row.querySelector('.reading-head b')?.textContent?.replace(/\s+/g, '') ?? undefined
  }
  return undefined
}

/** 能源页 KPI 的值。 */
function kpiValue(label: string): string | undefined {
  for (const row of container.querySelectorAll('.ex-kpi')) {
    if (row.querySelector('.ex-kpi-label')?.textContent?.trim() !== label) continue
    return row.querySelector('.ex-kpi-line b')?.textContent?.trim() ?? undefined
  }
  return undefined
}

it('同一 cursor 下总览、能源页、园区标注的 T1 与光伏读数一致', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const cursor = dayAtHour(day, 12)
  const t1Reading = t1(day, cursor)
  const pvReading = pv(day, cursor)

  await act(async () => root.render(
    <Overview day={day} cursor={cursor} selected={null} onSelect={noop} campusRef={{ current: null }} openEvidence={noop} />,
  ))
  expect(readingValue('T1 负载')).toBe(String(t1Reading.loadPct))
  expect(readingValue('光伏出力')).toBe(String(pvReading.kwNow))

  await act(async () => root.render(<EnergyPage day={day} cursor={cursor} openEvidence={noop} source="sample" />))
  expect(kpiValue('T1 负载率')).toBe(String(t1Reading.loadPct))
  expect(kpiValue('光伏今日发电')).toBe(pvReading.kwhToday?.toFixed(1))

  const campus = buildCampus(campusInput(day, cursor))
  const transformer = campus.find(entity => entity.spot === 'transformer')!
  expect(transformer.value).toContain(`负载 ${t1Reading.loadPct}%`)
  const solar = campus.find(entity => entity.spot === 'pv')!
  expect(solar.value).toBe(`出力 ${pvReading.kwNow} kW`)
}, 20000)

it('T1 正好等于上限 85% 时，总览、能源页、园区标注文案一致：已到上限', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  for (const frame of day.state_series) {
    for (const [id, device] of Object.entries(frame.devices)) if (/transformer/i.test(id)) (device as Record<string, unknown>).load_ratio = 0.85
    frame.changes = []
  }
  const cursor = dayAtHour(day, 12)
  expect(t1(day, cursor)).toMatchObject({ loadPct: 85, limitPct: 85, state: 'at_limit' })

  await act(async () => root.render(
    <Overview day={day} cursor={cursor} selected={null} onSelect={noop} campusRef={{ current: null }} openEvidence={noop} />,
  ))
  const row = [...container.querySelectorAll('.reading')].find(item => item.querySelector('.reading-head .lb')?.textContent?.trim() === 'T1 负载')
  expect(row?.textContent).toContain('已到上限')
  expect(row?.textContent).not.toContain('超上限')

  await act(async () => root.render(<EnergyPage day={day} cursor={cursor} openEvidence={noop} source="sample" />))
  const kpi = [...container.querySelectorAll('.ex-kpi')].find(item => item.querySelector('.ex-kpi-label')?.textContent?.trim() === 'T1 负载率')
  expect(kpi?.textContent).toContain('已到上限')

  const transformer = buildCampus(campusInput(day, cursor)).find(entity => entity.spot === 'transformer')!
  expect(transformer.value).toContain('已到上限')
}, 20000)
