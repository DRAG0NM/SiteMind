import { expect, it } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from './demoDay'
import { decisionEffect, transformerLoadPoints } from './rollup'
import { devicesAt } from './selectors'
import { dayAtHour, time } from './time'
import type { Day } from './types'

it('内置样例补全的裁决 outcome 全部是 contracts 枚举', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const outcomes = ['executed', 'work_order', 'to_human', 'sampling', 'blocked']
  expect(day.decisions.length).toBeGreaterThan(0)
  for (const decision of day.decisions) expect(outcomes).toContain(decision.result.outcome)
})

it('效果对比的协同组峰值等于回放实际峰值，08:07 结论不把 96% 说成已压回安全线', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  // 状态帧和帧内 changes 都是实际回放会显示的状态，峰值要连带 changes 的时刻一起看。
  const instants = new Set<number>()
  for (const frame of day.state_series) {
    instants.add(time(frame.at))
    for (const change of frame.changes ?? []) instants.add(time(change.at))
  }
  const peaks = [...instants].map(at => {
    const transformer = Object.entries(devicesAt(day, at))
      .find(([id]) => id.includes('/transformer/T1'))?.[1]
    return Number(transformer?.load_ratio) * 100
  }).filter((value): value is number => Number.isFinite(value))
  const peak = Math.max(...peaks)
  const value = (group: string) =>
    day.metrics.find(metric => metric.metric === 'transformer_peak_pct' && metric.group === group)?.value

  expect(value('coordinated')).toBe(peak)
  expect(value('baseline')!).toBeGreaterThanOrEqual(value('independent')!)
  expect(value('independent')!).toBeGreaterThanOrEqual(value('coordinated')!)

  const summary = day.decisions.find(decision => decision.decision_id === 'decision://dc-demo-0806')?.result.summary ?? ''
  expect(summary).not.toContain('峰值压回安全线')
  expect(summary).toContain('压回安全线内')
})

it('08:10 的左栏读数与曲线同点一致，08:15 起取样例自带的动作后读数，裁决效果 96→81', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const at = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const loadAt = (instant: number) => {
    const device = Object.entries(devicesAt(day, instant)).find(([id]) => id.includes('/transformer/T1'))?.[1]
    return Math.round(Number(device?.load_ratio) * 100)
  }

  expect(loadAt(at(8, 10))).toBe(90)
  expect(loadAt(at(8, 15))).toBe(81)
  expect(loadAt(at(8, 20))).toBe(81)

  const points = transformerLoadPoints(day)
  expect(points.find(point => point.at === at(8, 10))?.value).toBe(90)
  for (let index = 1; index < points.length; index += 1) expect(points[index].at).toBeGreaterThanOrEqual(points[index - 1].at)
  // 合成帧只留 08:10 的过渡点；0.86/0.82 会和 08:15 帧的 81% 打架。
  expect(points.some(point => point.value === 86 || point.value === 82)).toBe(false)

  const decision = day.decisions.find(item => item.decision_id === 'decision://dc-demo-0806')
  // 裁决后（08:07）还没有读数可看：不给未来数字；T1 实际降下来后（08:15 帧 81%）才给实际值。
  expect(decisionEffect(day, decision, at(8, 7))).toEqual({ before: 96, after: null, limit: 85, done: false })
  expect(decisionEffect(day, decision, at(8, 10))).toEqual({ before: 96, after: 90, limit: 85, done: true })
  expect(decisionEffect(day, decision, at(8, 21))).toEqual({ before: 96, after: 81, limit: 85, done: true })
})

it('合成补全的 agent_runs 用 contracts 枚举，账本分钟带非负储能电量且负荷分项自洽', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  // group:'config' 是 Issue #216 契约缺口，本测试不覆盖。
  const kinds = ['expert', 'triage', 'arbitration', 'follow_up', 'investigate']
  const statuses = ['finished', 'failed', 'timeout', 'blocked', 'cancelled']
  expect(day.agent_runs.length).toBeGreaterThan(0)
  for (const run of day.agent_runs) {
    expect(kinds).toContain(run.kind)
    expect(statuses).toContain(run.status)
  }
  const minutes = day.ledger_minutes as Array<Day['ledger_minutes'][number] & { energy_start_kwh: number; energy_end_kwh: number }>
  expect(minutes.length).toBeGreaterThan(0)
  for (const minute of minutes) {
    expect(minute.energy_start_kwh).toBeGreaterThanOrEqual(0)
    expect(minute.energy_end_kwh).toBeGreaterThanOrEqual(0)
    const total = Object.values(minute.load_breakdown ?? {}).reduce((sum, value) => sum + value, 0)
    expect(Math.abs(total - minute.load_kwh)).toBeLessThan(1e-2)
  }
})

it('动作窗口不足两分钟时，窗口结束后也能给出实际读数，不会一直待观察', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const at = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const decision = day.decisions.find(item => item.decision_id === 'decision://dc-demo-0806')!
  const end = new Date(time(decision.at) + 90_000).toISOString()
  for (const proposal of day.proposals) {
    if (!decision.result.selected_proposal_ids.includes(proposal.proposal_id ?? '')) continue
    for (const action of proposal.actions ?? []) action.interval.end = end
  }
  // 窗口内没有整分钟采样点：回放头过了窗口就落定，after 取窗口后的第一个采样点，不再返回 after=null。
  expect(decisionEffect(day, decision, at(8, 10))).toEqual({ before: 96, after: 96, limit: 85, done: true })
})

it('裁决后读数没有改善：窗口内一直待观察，窗口结束后如实显示实际读数，卡片不隐藏', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  for (const frame of day.state_series) {
    for (const [id, device] of Object.entries(frame.devices)) if (/transformer/i.test(id)) (device as Record<string, unknown>).load_ratio = 0.96
    frame.changes = []
  }
  const at = (hour: number, minute = 0) => dayAtHour(day, hour) + minute * 60_000
  const decision = day.decisions.find(item => item.decision_id === 'decision://dc-demo-0806')
  expect(decisionEffect(day, decision, at(8, 10))).toEqual({ before: 96, after: null, limit: 85, done: false })
  expect(decisionEffect(day, decision, at(8, 25))).toEqual({ before: 96, after: 96, limit: 85, done: true })
})
