// @vitest-environment jsdom
// B49 验收冒烟：效果对比页的知识侧/编排对照/Harness/越学越省，以及设备监测页的「传感器读数异常」回放。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { EvidenceDrawer } from '../components/EvidenceDrawer'
import { withShowcaseDemo } from '../data/demoDay'
import { evidenceFor } from '../data/evidence'
import { dayAtHour, time } from '../data/time'
import type { Day } from '../data/types'
import { buildCampus } from '../../shared/campus'
import { campusInput, readings } from '../data/rollup'
import { ComparePage } from './Compare'
import { EquipmentPage } from './Equipment'
import { HvacPage } from './Hvac'

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

const text = (selector: string) => container.querySelector(selector)?.textContent ?? ''
const noop = () => { /* 测试里不开抽屉 */ }

it('B17 区域温度和百分数湿度按运行快照展示', async () => {
  const day = structuredClone(sampleJson) as unknown as Day
  day.state_series = [{ at: day.state_series[0].at,
    devices: { 'entity://demo-electronics/zone/admin': { temperature: 24.3, humidity: 55.1 } }, changes: [] }]
  await act(async () => root.render(<HvacPage day={day} cursor={dayAtHour(day, 16)} openEvidence={noop} />))
  expect(text('.ex-kpis')).toContain('24.3')
  expect(text('.ex-temp-grid')).toContain('55%')
  expect(container.querySelector('[aria-label^="行政楼平均室温全天曲线"]')).toBeTruthy()
  day.state_series[0].devices['entity://demo-electronics/zone/admin'].humidity = 1
  await act(async () => root.render(<HvacPage day={day} cursor={dayAtHour(day, 16)} openEvidence={noop} />))
  expect(text('.ex-temp-grid')).toContain('湿度 1（单位未标注）')
})

it('效果对比页有知识侧、编排对照、Harness 与越学越省；缺指标时明确空态', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  await act(async () => root.render(
    <ComparePage day={day} cursor={dayAtHour(day, 12)} openEvidence={noop} source="sample" />,
  ))
  expect(text('.cmp-right')).toContain('知识侧')
  expect(text('.cmp-right')).toContain('标准对齐率')
  expect(text('.cmp-right')).toContain('编排对照')
  expect(text('.cmp-right')).toContain('Harness')
  expect(text('.cmp-right')).toContain('越学越省')

  const bare = structuredClone(day)
  bare.metrics = bare.metrics.filter(item => item.group !== 'knowledge')
  await act(async () => root.render(
    <ComparePage day={bare} cursor={dayAtHour(bare, 12)} openEvidence={noop} source="exported" />,
  ))
  expect(text('.cmp-right')).toContain('暂未包含知识侧评测指标')
})

it.each([
  ['electricity_cost', 100.29, '¥0.29', '增加（↑0.29%）', '增加（↑25.36%）'],
  ['electricity_cost', 99.71, '¥-0.29', '减少（↓0.29%）', '增加（↑24.64%）'],
  ['cost_cny', 100, '¥0', '持平', '增加（↑25%）'],
] as const)('电费差额与方向使用同一指标和基线：%s / %s', async (metric, cost, amount, direction, independent) => {
  const day = structuredClone(sampleJson) as unknown as Day
  day.metrics = [
    { metric, group: 'coordinated', value: cost, unit: 'CNY' },
    { metric, group: 'independent', value: 80, unit: 'CNY' },
    { metric, group: 'baseline', value: 100, unit: 'CNY' },
    { metric: 'peak_import', group: 'coordinated', value: 97, unit: 'kW' },
    { metric: 'peak_import', group: 'baseline', value: 100, unit: 'kW' },
  ]
  await act(async () => root.render(<ComparePage day={day} cursor={dayAtHour(day, 12)} openEvidence={noop} />))
  expect(text('.cmp-saved')).toBe(amount)
  expect(text('.cmp-hero')).toContain(`较基线组${direction}`)
  expect(text('.cmp-hero')).toContain(`较独立组${independent}`)
  if (metric === 'electricity_cost') expect(text('.cmp-kpis')).toContain('↓3%购电分钟峰值')
})

it('问答差值按各自单位换算：unit=% 的 86 与 71 显示 15 个百分点，不是 1500', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const qa = day.metrics.find(item => item.metric === 'doc_qa_accuracy')!
  const rag = day.metrics.find(item => item.metric === 'doc_qa_accuracy_rag')!
  qa.value = 86
  qa.unit = '%'
  rag.value = 71
  rag.unit = '%'
  await act(async () => root.render(
    <ComparePage day={day} cursor={dayAtHour(day, 12)} openEvidence={noop} source="sample" />,
  ))
  expect(text('.cmp-right')).toContain('跨文档问答较普通 RAG 高 15 个百分点')
  expect(text('.cmp-right')).not.toContain('1500')
})

it('真实导出的空压站（air_system/AIR、pressure_bar）也能在设备页显示压力列', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const site = 'entity://demo-electronics/'
  for (const frame of day.state_series) {
    // 真实状态帧：air_compressor/AC-1..3（只有 running）排在前，带 pressure_bar 的 air_system/AIR 在后
    const rest = Object.fromEntries(Object.entries(frame.devices).filter(([id]) => !/compressor/i.test(id)))
    frame.devices = {
      [`${site}air_compressor/AC-1`]: { running: true },
      [`${site}air_compressor/AC-2`]: { running: false },
      [`${site}air_compressor/AC-3`]: { running: false },
      ...rest,
      [`${site}air_system/AIR`]: { pressure_bar: 7.2 },
    }
  }
  await act(async () => root.render(
    <EquipmentPage day={day} cursor={dayAtHour(day, 12)} openEvidence={noop} source="exported" />,
  ))
  expect(text('.ex-table')).toContain('0.72')
  expect(readings(day, dayAtHour(day, 12)).find(item => item.key === 'air')?.value).toBe('0.72')
  const air = buildCampus(campusInput(day, dayAtHour(day, 12))).find(entity => entity.spot === 'compressor')!
  expect(air.value).toBe('运行中 · 0.72 MPa')
})

it('设备监测页回放传感器读数异常：15:20 显示读数质量、候选假设、试探过程与判读结论', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  await act(async () => root.render(
    <EquipmentPage day={day} cursor={dayAtHour(day, 15) + 20 * 60_000} openEvidence={noop} source="sample" />,
  ))
  expect(text('.ex-sensor')).toContain('读数质量')
  expect(text('.ex-sensor')).toContain('候选故障假设')
  expect(text('.ex-sensor')).toContain('Agent 试探过程')
  expect(text('.ex-sensor')).toContain('判定为传感器读数异常（探头漂移）')
  expect(text('.ex-sensor')).toContain('相邻测点')

  await act(async () => root.render(
    <EquipmentPage day={day} cursor={dayAtHour(day, 14)} openEvidence={noop} source="exported" />,
  ))
  expect(text('.ex-sensor')).toContain('暂无传感器读数异常事件')
})

it('传感器读数异常回放：15:11 不显示 15:13:40 的工具与 15:14 的判读，15:14 后显示，回拖再隐藏', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const cursor = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const render = (at: number) => act(async () => root.render(
    <EquipmentPage day={day} cursor={at} openEvidence={noop} source="sample" />,
  ))

  await render(cursor(15, 11))
  expect(container.querySelector('.ex-probe')).toBeNull()
  expect(text('.ex-sensor')).not.toContain('派探头校准工单')
  expect(text('.ex-judge')).not.toContain('传感器读数异常')

  await render(cursor(15, 14))
  expect(container.querySelectorAll('.ex-probe-row')).toHaveLength(5)
  expect(text('.ex-probe')).toContain('派探头校准工单')
  expect(text('.ex-judge')).toContain('探头读数漂移')

  await render(cursor(15, 11))
  expect(container.querySelector('.ex-probe')).toBeNull()
  expect(text('.ex-judge')).not.toContain('传感器读数异常')
})

it('证据抽屉按仿真生效时刻裁剪：15:11 不显示 15:14 的运行与裁决，10:30 不显示 10:31 的裁决', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const cursor = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const entity = 'entity://demo-electronics/compressor/AIR-1'

  const early = evidenceFor(day, cursor(15, 11), entity, '依据')
  expect(early.runs).toHaveLength(0)
  expect(early.notes.join('\n')).not.toContain('判定为传感器读数异常')
  expect(early.refs.join('\n')).not.toContain('读数质量 0.42')

  const later = evidenceFor(day, cursor(15, 14), entity, '依据')
  expect(later.runs.map(run => run.finalAnswer)).toContain('温度探头读数漂移，不是真实过热；按相邻测点控温并派校准工单。')
  expect(later.notes.join('\n')).toContain('裁决：判定为传感器读数异常')
  expect(later.notes.join('\n')).toContain('权衡：温度读数越限')
  expect(later.refs.join('\n')).toContain('读数质量 0.42 低于 0.6')

  const back = evidenceFor(day, cursor(15, 11), entity, '依据')
  expect(back.runs).toHaveLength(0)
  expect(back.notes.join('\n')).not.toContain('判定为传感器读数异常')

  const beforeDecision = evidenceFor(day, cursor(10, 30), entity, '依据')
  expect(beforeDecision.notes.join('\n')).not.toContain('设备监测开维修工单')
  expect(beforeDecision.experts.some(item => item.selected)).toBe(false)

  const atDecision = evidenceFor(day, cursor(10, 31), entity, '依据')
  expect(atDecision.notes.join('\n')).toContain('设备监测开维修工单')
  expect(atDecision.experts.some(item => item.selected)).toBe(true)
})

it('传感器报警只采用同事件单、设备和测点的判读', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  day.events.push({
    event_id: 'event://other-sensor-check', at: '2026-07-01T07:19:00Z', event_type: 'equipment_sensor_check',
    source: 'script', entity_id: 'entity://other-equipment', summary: '另一设备结论', nature: 'synthetic',
    payload: { conclusion: '另一设备轴承故障', verified_by: ['另一设备的依据'] },
  })
  const render = async () => act(async () => root.render(
    <EquipmentPage day={day} cursor={dayAtHour(day, 15) + 20 * 60_000} openEvidence={noop} source="sample" />,
  ))
  await render()
  expect(text('.ex-sensor')).toContain('判定为传感器读数异常（探头漂移）')
  expect(text('.ex-sensor')).not.toContain('另一设备轴承故障')
  expect(text('.ex-sensor')).not.toContain('另一设备的依据')

  // 即使设备相同，没有共同事件单也不能借用结论。
  day.events.find(item => item.event_id === 'event://demo-sensor-verified')!.entity_id = 'entity://other-equipment'
  await render()
  expect(text('.ex-sensor')).not.toContain('判读结论')
})

it('证据抽屉仅显示正式关联的事件单运行，无法关联时为空', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const cursor = dayAtHour(day, 16)
  expect(evidenceFor(day, cursor, null, '没有关联').runs).toEqual([])
  expect(evidenceFor(day, cursor, 'entity://unknown', '未知设备').runs).toEqual([])
  const document = evidenceFor(day, cursor, 'document://maintenance-manual', '手册')
  expect(document.runs.map(item => item.ticket)).toEqual(['tk-0007'])
  const decision = evidenceFor(day, cursor, null, '指定事件单', 'ticket://tk-0001')
  expect(decision.runs.length).toBeGreaterThan(0)
  expect(decision.runs.every(item => item.ticket === 'tk-0001')).toBe(true)
  day.events.push({ event_id: 'unrelated-null', at: '2026-07-01T07:59:00Z', event_type: 'operator_correct',
    source: 'script', entity_id: null, summary: '不相关的人工纠错', nature: 'synthetic', payload: {} })
  day.events.push({ event_id: 'associated-null', at: '2026-07-01T07:30:00Z', event_type: 'operator_instruction',
    source: 'script', entity_id: null, summary: '关联事件', nature: 'synthetic', payload: {} })
  day.tickets.push({ ...day.tickets[0], ticket_id: 'ticket://no-proposal', event_ids: ['associated-null'] })
  const associated = evidenceFor(day, cursor, null, '指定事件单', 'ticket://no-proposal')
  expect(associated.title).toBe('关联事件')
  expect(associated.subtitle).toContain('15:30')
})

// 真实导出（day.json）与内置样例的形状不同：candidates 是 {id,label,…} 对象、quality 是文本（suspect）、
// fault 是布尔值，且带压力的 air_system/AIR 没有 current_a。页面必须显示名称而不是 [object Object]，
// 也不能把空序列算成 NaN 或给列表重复 key。
it('真实导出的候选故障假设为对象、质量为文本、fault 为布尔值时设备页不出脏值', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const site = 'entity://demo-electronics/'
  for (const frame of day.state_series) {
    frame.devices = {
      [`${site}air_compressor/AC-1`]: { running: true, fault: false, current_a: 60.65, oil_c: 76.06 },
      [`${site}air_compressor/AC-2`]: { running: true, fault: false, current_a: 60.65, oil_c: 76.06 },
      [`${site}air_system/AIR`]: { pressure_bar: 5.5, on: true },
    }
  }
  const at = '2026-07-01T02:30:00Z'
  day.events = day.events.filter(event => event.event_id !== 'act2-leak')
  day.events.push({
    event_id: 'act2-leak', at, event_type: 'compressor_pressure_drop', source: 'script',
    entity_id: `${site}air_compressor/AC-2`, summary: '母管压力下降', nature: 'simulated',
    payload: {
      sensor: 'AIR-HDR.P', reading: 5.5, baseline: 6.365, residual_c: -0.865, residual_limit_c: 0.25,
      quality: 'suspect',
      candidates: [
        { id: 'pressure_sensor_stuck', label: '压力传感器卡死或偏置', category: 'sensor' },
        { id: 'air_leak', label: '管网泄漏', category: 'equipment' },
        { id: 'compressor_fault', label: '空压机性能下降', category: 'equipment' },
      ],
    },
  })

  const errors: unknown[][] = []
  const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => { errors.push(args) })
  try {
    await act(async () => root.render(
      <EquipmentPage day={day} cursor={time(at) + 60_000} openEvidence={noop} source="exported" />,
    ))
    const sensor = text('.ex-sensor')
    expect(sensor).toContain('压力传感器卡死或偏置')
    expect(sensor).toContain('管网泄漏')
    expect(sensor).toContain('空压机性能下降')
    expect(sensor).toContain('可疑')
    expect(sensor).not.toContain('[object Object]')
    expect(text('.ex-table')).toContain('运行')
    expect(text('.ex-table')).not.toContain('故障')
    const page = container.textContent ?? ''
    expect(page).not.toContain('[object Object]')
    expect(page).not.toContain('NaN')
    expect(errors.flat().join(' ')).not.toMatch(/same key/i)
  } finally {
    spy.mockRestore()
  }
})

// 内置样例的 quality 是数值：只有低于 0.6 才追加「低于 0.6」，0.9 这类正常读数不能被误标成偏低。
it('质量为数值时只有低于 0.6 才提示偏低', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const at = '2026-07-01T02:30:00Z'
  const render = async (quality: number) => {
    day.events = day.events.filter(event => event.event_id !== 'act2-leak')
    day.events.push({
      event_id: 'act2-leak', at, event_type: 'compressor_pressure_drop', source: 'script',
      entity_id: 'entity://demo-electronics/air_compressor/AC-2', summary: '母管压力下降',
      nature: 'simulated',
      payload: {
        sensor: 'AIR-HDR.P', reading: 5.5, baseline: 6.365, residual_c: -0.865, residual_limit_c: 0.25,
        quality,
      },
    })
    await act(async () => root.render(
      <EquipmentPage day={day} cursor={time(at) + 60_000} openEvidence={noop} source="exported" />,
    ))
    return text('.ex-sensor')
  }

  const normal = await render(0.9)
  expect(normal).toContain('0.9')
  expect(normal).not.toContain('低于 0.6')

  const low = await render(0.42)
  expect(low).toContain('0.42')
  expect(low).toContain('低于 0.6')
})

// 合入 main 的证据抽屉重构后，B49 的运行记录明细仍要完整显示（关联事件单、调用层级、工具 Agent、工具状态、最终回答）。
it('证据抽屉运行记录显示关联事件单、Agent 层级、工具 Agent 与状态、最终回答，且能关闭', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const cursor = dayAtHour(day, 15) + 14 * 60_000
  const evidence = evidenceFor(day, cursor, 'entity://demo-electronics/compressor/AIR-1', 'AIR-1 依据')
  let closed = 0
  await act(async () => root.render(
    <EvidenceDrawer
      evidence={evidence}
      cursor={cursor}
      timezone={day.timezone}
      initialTab="runs"
      source={{ kind: 'sample', label: '内置样例 · 2026-07-01' }}
      onClose={() => { closed += 1 }}
    />,
  ))

  expect(text('.runlink')).toContain('关联事件单 tk-')
  expect(container.querySelectorAll('.agenttree .agentnode').length).toBeGreaterThan(0)
  expect(text('.agenttree')).toContain('设备监测专家')
  expect(text('.toolagent')).toContain('设备监测专家')
  expect(container.querySelectorAll('.toolrow .runstatus').length).toBeGreaterThan(0)
  expect(text('.drawer-body')).toContain('降级结果')
  expect(text('.runanswer')).toContain('温度探头读数漂移')
  expect(container.querySelectorAll('.drawer')).toHaveLength(1)

  const close = container.querySelector<HTMLButtonElement>('.drawer-close')!
  await act(async () => { close.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  // 关闭带 200 ms 退场动画，动画结束才回调 onClose。
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 250)) })
  expect(closed).toBe(1)
})
