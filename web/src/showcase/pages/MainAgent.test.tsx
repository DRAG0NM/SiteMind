// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from '../data/demoDay'
import { dayAtHour } from '../data/time'
import type { Day } from '../data/types'
import { MainAgentPage } from './MainAgent'

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

/** 16:25 的慢通道执行裁决没有候选方案，主 Agent 页要显示 dc-0005 而不是退回更早的提案裁决。 */
it('16:30 主 Agent 页显示慢通道执行裁决，14:30 仍显示早期自动执行裁决', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const noop = () => { /* 测试里不开抽屉 */ }

  await act(async () => root.render(
    <MainAgentPage day={day} cursor={dayAtHour(day, 16) + 30 * 60_000} openEvidence={noop} />,
  ))
  expect(text('.ex-verdict')).toContain('无选中动作')
  expect(text('.ex-verdict')).toContain('裁决记录未含自主等级')
  expect(text('.ex-verdict')).not.toContain('慢通道')
  expect(text('.ex-chain')).toContain('慢通道：多跳追溯')
  expect(text('.ex-empty-cell')).toContain('请查看运行记录和回执')
  expect(text('.ex-page')).not.toContain('直接转人工')

  await act(async () => root.render(
    <MainAgentPage day={day} cursor={dayAtHour(day, 14) + 30 * 60_000} openEvidence={noop} />,
  ))
  expect(text('.ex-verdict')).toContain('自动执行')
  expect(text('.ex-page')).toContain('开维修工单')
  expect(text('.ex-page')).toContain('照明切备用回路')
  expect(text('.ex-empty-cell')).toBe('')
})

it('事件已到达但专家结果未生效时，不提前显示裁决', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const decision = day.decisions.find(item => item.result.selected_proposal_ids.length > 0)!
  const at = dayAtHour(day, 16)
  decision.at = new Date(at).toISOString()
  day.decisions = [decision]
  for (const proposal of day.proposals) {
    if (!decision.conflict.proposal_ids.includes(proposal.proposal_id ?? '')) continue
    proposal.timing = { origin_event_at: new Date(at).toISOString(), trigger_at: new Date(at).toISOString(),
      effective_at: new Date(at + 60_000).toISOString(), requested_at: new Date(at).toISOString() }
  }
  day.agent_runs = []
  const noop = () => { /* 测试里不开抽屉 */ }
  await act(async () => root.render(<MainAgentPage day={day} cursor={at} openEvidence={noop} />))
  expect(text('.ex-verdict')).toBe('')
  await act(async () => root.render(<MainAgentPage day={day} cursor={at + 60_000} openEvidence={noop} />))
  expect(text('.ex-verdict')).toContain('自动执行')
})
