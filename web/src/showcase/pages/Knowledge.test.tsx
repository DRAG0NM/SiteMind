// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from '../data/demoDay'
import { dayAtHour } from '../data/time'
import type { Day } from '../data/types'
import { KnowledgePage } from './Knowledge'

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

/** 维护手册在知识版本与提案引用里是同一份资料，只出一张卡并计入资产激活率。 */
it('资料清单按标题合并维护手册，资产激活率与实际引用一致', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  // 10:40：泄漏提案已经提出，引用计数与激活率都应当跟上；10:00 时还不能算未来的引用。
  await act(async () => {
    root.render(<KnowledgePage day={day} cursor={dayAtHour(day, 10) + 40 * 60_000} openEvidence={() => { /* 测试里不开抽屉 */ }} />)
  })

  const cards = [...container.querySelectorAll('.kn-doc')]
  expect(cards).toHaveLength(2)
  expect(cards.some(card => (card.textContent ?? '').includes('暖通手册'))).toBe(true)
  const manual = cards.find(card => (card.textContent ?? '').includes('空压机运行维护手册'))
  expect(manual).toBeTruthy()
  expect(manual!.textContent).toContain('被 1 条提案引用')
  expect(container.querySelector('.kn-assets b')?.textContent).toBe('1 / 1')
  expect(container.textContent).not.toContain('maintenance-manual')

  // 版本时间线悬停保留导出的原编号，页面上仍只写知识 vN。
  const versionRows = [...container.querySelectorAll('.kn-vrow')] as HTMLElement[]
  expect(versionRows.every(row => row.title.startsWith('知识版本 vm-'))).toBe(true)
})

it('资料引用和图谱等提案结果生效，回拖后重新隐藏', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const sensor = day.proposals.find(item => item.proposal_id === 'proposal://pp-demo-sensor')!
  sensor.evidence_refs!.push('rule://sensor-result-only')
  const renderAt = async (minute: number) => {
    await act(async () => root.render(<KnowledgePage day={day} cursor={dayAtHour(day, 15) + minute * 60_000} openEvidence={() => {}} />))
    return {
      manual: [...container.querySelectorAll('.kn-doc')].find(item => item.textContent?.includes('空压机运行维护手册'))?.textContent ?? '',
      graph: container.querySelector('svg[aria-label^="依据图谱"]')?.getAttribute('aria-label'),
      page: container.textContent ?? '',
    }
  }
  const early = await renderAt(11)
  expect(early.manual).toContain('被 1 条提案引用')
  const later = await renderAt(14)
  expect(later.manual).toContain('被 2 条提案引用')
  expect(later.graph).not.toBe(early.graph)
  const back = await renderAt(11)
  expect(back.manual).toBe(early.manual)
  expect(back.graph).toBe(early.graph)
})

/** 两条真实资产：A 被规则引用（被引用），B 只有入库（沉睡）。 */
function withAssets(day: Day) {
  const at = '2026-07-01T01:00:00Z'
  const base = { content_hash: 'a'.repeat(64), industry: 'factory', asset_type: 'manual', media_type: 'text/plain', nature: 'synthetic' as const, available_at: at, ingested_at: at }
  day.assets = [
    { ...base, asset_version_id: 'asset://factory/manual-a', source_uri: 'document://manual-a', title: '手册 A' },
    { ...base, asset_version_id: 'asset://factory/manual-b', source_uri: 'document://manual-b', title: '手册 B' },
  ]
  day.evidence = [{
    event_id: 'evidence://asset-a', seq: 1, occurred_at: at, trace_id: 'trace-a', event_type: 'rule_applied',
    sources: [{ asset_version_id: 'asset://factory/manual-a' }], nature: 'synthetic', result_status: 'ok',
  }]
  return day
}

it('图谱生长：游标未到的阶段只显示待生成，不提前显示未来快照的节点与边', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  // 16:00 时「纠错附资料」要到 16:20 才发生。
  await act(async () => root.render(
    <KnowledgePage day={day} cursor={dayAtHour(day, 16)} openEvidence={() => { /* 测试里不开抽屉 */ }} />,
  ))
  const phases = [...container.querySelectorAll('.kn-phase')] as HTMLElement[]
  expect(phases).toHaveLength(3)
  expect(phases[0].textContent).toContain('节点')
  expect(phases[1].textContent).toContain('节点')
  expect(phases[2].textContent).toContain('待生成')
  expect(phases[2].textContent).not.toContain('节点')
  expect(phases[2].textContent).not.toContain('条边')
})

it('没有全量目录时资产四态与激活率标证据覆盖子集，有 asset_total 时按全量显示', async () => {
  const day = withAssets(withShowcaseDemo(structuredClone(sampleJson) as unknown as Day))
  const render = () => act(async () => root.render(
    <KnowledgePage day={day} cursor={dayAtHour(day, 10) + 40 * 60_000} openEvidence={() => { /* 测试里不开抽屉 */ }} />,
  ))

  await render()
  expect(container.textContent).toContain('证据覆盖子集 · 推断值')
  expect(container.textContent).not.toContain('全厂资产激活率')
  expect(container.querySelector('.kn-assets b')?.textContent).toBe('1 / 2')

  // 只有 asset_total、没有 asset_activated 时仍算子集：分子不能拿子集里的“被引用”数顶替
  day.metrics.push({ metric: 'asset_total', group: 'knowledge', value: 20, unit: '份', nature: 'measured' })
  await render()
  expect(container.textContent).toContain('证据覆盖子集 · 推断值')
  expect(container.textContent).not.toContain('全厂资产激活率')

  day.metrics.push({ metric: 'asset_activated', group: 'knowledge', value: 14, unit: '份', nature: 'measured' })
  for (const [stage, value] of [['dormant', 3], ['identified', 2], ['in_graph', 1], ['referenced', 14]] as const) {
    day.metrics.push({ metric: `asset_state_counts.${stage}`, group: 'knowledge', value, unit: '份', nature: 'measured' })
  }
  await render()
  expect(container.textContent).not.toContain('证据覆盖子集 · 推断值')
  expect(container.textContent).toContain('全厂资产激活率')
  expect(container.querySelector('.kn-assets b')?.textContent).toBe('14 / 20')
  expect(container.querySelector('.kn-assets')?.textContent).toContain('全天汇总')
  expect(container.querySelector('.kn-stages-inline')?.textContent).toContain('全天汇总')
  expect(container.querySelector('.kn-assetlist')?.textContent).toContain('（推断）')
  const counts = [...container.querySelectorAll('.kn-stages-inline b')].map(item => item.textContent)
  expect(counts).toEqual(['3', '2', '1', '14'])
})

it('官方汇总用单条 asset_state_counts + counts 时四态与激活率同样按全量显示（PR268 形状）', async () => {
  const day = withAssets(withShowcaseDemo(structuredClone(sampleJson) as unknown as Day))
  // 官方全量汇总到齐、但当天没有被证据引用的资产：四态仍显示官方数字，不退回子集列表。
  day.assets = []
  day.evidence = []
  day.metrics.push({ metric: 'asset_total', group: 'knowledge', value: 20, unit: '份', nature: 'measured' })
  day.metrics.push({ metric: 'asset_activated', group: 'knowledge', value: 14, unit: '份', nature: 'measured' })
  day.metrics.push({
    metric: 'asset_state_counts', group: 'knowledge', value: null, unit: '份', nature: 'measured',
    counts: { dormant: 3, identified: 2, in_graph: 1, referenced: 14 },
  } as unknown as (typeof day.metrics)[number])
  await act(async () => root.render(
    <KnowledgePage day={day} cursor={dayAtHour(day, 10) + 40 * 60_000} openEvidence={() => { /* 测试里不开抽屉 */ }} />,
  ))
  expect(container.textContent).not.toContain('证据覆盖子集 · 推断值')
  expect(container.textContent).toContain('全厂资产激活率')
  expect(container.querySelector('.kn-assets b')?.textContent).toBe('14 / 20')
  const counts = [...container.querySelectorAll('.kn-stages-inline b')].map(item => item.textContent)
  expect(counts).toEqual(['3', '2', '1', '14'])
})
