// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EvaluationReportRaw, MetricSeriesRaw } from '../../api'
import { MetricsScreen } from './MetricsScreen'

let container: HTMLDivElement
let root: Root

const ok = (body: unknown) => Promise.resolve({ ok: true, status: 200, json: async () => body } as unknown as Response)

const REPORT: EvaluationReportRaw = {
  report_id: 'report://b33-smoke',
  policies: ['baseline', 'independent', 'coordinated'],
  metrics: {
    baseline: { cost_cny: 20000, energy_kwh: 30000, transformer_peak_pct: 120, avg_wait_min: 8.5, comfort_violation_min: 300, manual_interventions: 25 },
    independent: { cost_cny: 19000, energy_kwh: 28500, transformer_peak_pct: 110, avg_wait_min: 6, comfort_violation_min: 180, manual_interventions: 12 },
    coordinated: { cost_cny: 18000, energy_kwh: 27000, transformer_peak_pct: 95, avg_wait_min: 4, comfort_violation_min: 60, manual_interventions: 3 },
  },
  comparable: true,
}

const SERIES: MetricSeriesRaw = {
  metric: 'review_f1',
  dataset: 'demo',
  split_id: 'b35-smoke',
  points: [[0, 0.6], [50, 0.75], [200, 0.8]],
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

/** 等两次微任务：接口 promise 落定后组件还要再渲染一次。 */
async function render() {
  await act(async () => { root.render(<MetricsScreen />) })
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('接口全部失败时渲染三组表、合成徽章与退回说明', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('接口不可用'))))
  await render()

  const heads = [...container.querySelectorAll('.mx-table thead th')].map(node => node.textContent ?? '')
  expect(heads.some(text => text.includes('基线组'))).toBe(true)
  expect(heads.some(text => text.includes('独立组'))).toBe(true)
  expect(heads.some(text => text.includes('协同组'))).toBe(true)

  for (const label of ['用电量', '变压器峰值', '平均候梯', '舒适度越限', '人工干预']) {
    expect(container.textContent).toContain(label)
  }
  // 演示日记录里用电量的三组数值 23180 / 22430 / 21460
  expect(container.textContent).toContain('23,180')
  expect(container.textContent).toContain('22,430')
  expect(container.textContent).toContain('21,460')
  expect(container.textContent).toContain('当前展示标准演示场景数据')
  expect(container.querySelectorAll('.nature-badge.live')).toHaveLength(0)
  expect(container.textContent).toContain('合成')
  // 六张柱状图 + 一张复核曲线，jsdom 里量不到尺寸，只断言容器在
  expect(container.querySelectorAll('.mx-chart-body')).toHaveLength(7)
})

it('接口返回契约形状时显示真实数值与推算徽章', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/baselines')) return ok({ items: [REPORT], next_cursor: null })
    if (url.includes('/review-metrics')) return ok(SERIES)
    return Promise.reject(new Error(`未预期的请求 ${url}`))
  }))
  await render()

  // 接口报告里用电量的三组数值 30000 / 28500 / 27000，电费协同组 18000
  expect(container.textContent).toContain('30,000')
  expect(container.textContent).toContain('28,500')
  expect(container.textContent).toContain('27,000')
  expect(container.textContent).toContain('18,000')
  expect(container.textContent).toContain('数据来自实时评测接口')
  expect(container.textContent).toContain('数据来自实时评测接口')
  expect(container.textContent).not.toContain('当前展示标准演示场景数据')
  expect(container.querySelectorAll('.nature-badge.live')).toHaveLength(2)
  expect(container.textContent).toContain('推算')
})
