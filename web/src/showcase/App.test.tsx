// @vitest-environment jsdom
import { StrictMode, act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import sampleJson from '../../../contracts/examples/day.sample.json'
import { App } from './App'
import { EvidenceDrawer } from './components/EvidenceDrawer'
import { OnlineRunsHistory } from './components/OnlineRuns'
import { withShowcaseDemo } from './data/demoDay'
import { evidenceFor, decisionChain } from './data/evidence'
import { loadDay } from './data/loadDay'
import { buildEvidenceRuns } from './data/online'
import { readings, todayStats } from './data/rollup'
import { humanHandoffs } from './data/selectors'
import { dayAtHour } from './data/time'
import type { Day } from './data/types'
import { MainAgentPage } from './pages/MainAgent'
import { Overview } from './pages/Overview'

const sample = sampleJson as unknown as Day
let container: HTMLDivElement
let root: Root

beforeEach(() => {
  // 在线模式按 location.search 判断，测试之间必须清掉上一次的 ?run_id。
  window.history.replaceState({}, '', '/')
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

const text = (selector: string) => container.querySelector(selector)?.textContent ?? ''

async function click(selector: string) {
  const element = container.querySelector(selector)
  expect(element).toBeTruthy()
  await act(async () => { element!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
}

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

it('day.sample.json 下总览、播放条与知识版本可用，点小时和知识激活时刻都能跳转', async () => {
  await act(async () => root.render(<App initialDay={structuredClone(sample)} />))

  expect(text('.overview')).toContain('此刻')
  expect(text('.overview')).toContain('当日累计统计')
  expect(text('.overview')).toContain('厂区数字孪生')
  expect(container.querySelectorAll('.hour')).toHaveLength(24)
  expect(text('.playtime b')).toBe('07:30')
  expect(text('.knowledge-badge')).toContain('知识 v1')

  await click('button[aria-label="跳到 16:00"]')
  expect(text('.playtime b')).toBe('16:00')
  expect(container.querySelector('.hour.current')?.getAttribute('aria-label')).toBe('跳到 16:00')
  expect(text('.knowledge-badge')).toContain('知识 v2')

  await click('button[aria-label="跳到知识 v2 激活时刻"]')
  expect(text('.playtime b')).toBe('14:00')
  expect(text('.knowledge-badge')).toContain('知识 v2')
})

it('读不到 day.json 时回退到内置样例，页头显示内置样例并标出合成补全', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('.source')).toContain('标准演示场景 · 含合成补全')
  expect(text('.overview')).toContain('厂区数字孪生')
  expect(container.querySelectorAll('.knowledge-mark').length).toBeGreaterThanOrEqual(2)
})

it('没有事件的合法 day.json 仍按导出数据加载，不回退成合成样例', async () => {
  const empty = { schema_version: '1', plant_id: 'empty-plant', local_date: '2026-07-01', timezone: 'Asia/Shanghai', run_id: 'run-empty', runs: [{ run_id: 'run-empty' }] }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(empty), { status: 200, headers: { 'Content-Type': 'application/json' } })))
  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('.source')).toContain('day.json · 2026-07-01')
  expect(text('.overview')).toContain('empty-plant')
  expect(text('.datanote')).toContain('0 条事件')
  expect(container.querySelectorAll('.hour')).toHaveLength(24)
})

it('day.json 返回 404 时回退内置样例并保留样例标识', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 404 })))
  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('.source')).toContain('标准演示场景 · 含合成补全')
  expect(text('.overview')).toContain('厂区数字孪生')
})

it('后端 5xx 时显示“后端出错（HTTP 5xx）”错误条与重试，不回退样例', async () => {
  window.history.replaceState({}, '', '/showcase.html?run_id=run-down')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 502 })))
  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('[role="alert"]')).toContain('后端出错（HTTP 502）')
  expect(text('.boot')).toContain('重试')
  expect(container.querySelector('.overview')).toBeNull()
  expect(container.textContent).not.toContain('内置样例')
})

it('day.json 5xx 后点重试能重新加载数据', async () => {
  const retryDay = { schema_version: '1', plant_id: 'retry-plant', local_date: '2026-07-01', timezone: 'Asia/Shanghai', run_id: 'run-retry', runs: [{ run_id: 'run-retry' }] }
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response('', { status: 503 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(retryDay), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetcher)
  await act(async () => { root.render(<App />) })
  await settle()
  expect(text('[role="alert"]')).toContain('后端出错（HTTP 503）')

  await click('.retrybtn')
  await settle()
  expect(text('.source')).toContain('day.json · 2026-07-01')
  expect(text('.overview')).toContain('retry-plant')
})

it('200 但不是合法 JSON 时显示错误条与导入/重试，不回退样例', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{broken', { status: 200 })))
  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('[role="alert"]')).toContain('day.json')
  expect(container.querySelector('input[type="file"]')).toBeTruthy()
  expect(text('.boot')).toContain('重试')
  expect(container.querySelector('.overview')).toBeNull()
})

it('200 但事件时间坏时错误条给出字段路径，不白屏', async () => {
  const bad = structuredClone(sample) as unknown as Record<string, any>
  bad.events[0].at = 'bad'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(bad), { status: 200 })))
  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('[role="alert"]')).toContain('events[0].at')
  expect(container.querySelector('.overview')).toBeNull()
})

it('200 但时区非法时错误条指出 timezone，不白屏', async () => {
  const bad = structuredClone(sample) as unknown as Record<string, any>
  bad.timezone = 'Bad/Zone'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(bad), { status: 200 })))
  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('[role="alert"]')).toContain('timezone')
  expect(container.querySelector('.overview')).toBeNull()
})

it('离线打开时可以导入 day.json 回放导出的数据', async () => {
  await act(async () => root.render(<App initialDay={structuredClone(sample)} />))
  const imported = structuredClone(sample)
  imported.plant_id = 'imported-plant'
  imported.run_id = 'run-imported'
  const file = new File([JSON.stringify(imported)], 'day.json', { type: 'application/json' })
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')
  expect(input).toBeTruthy()
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  await act(async () => {
    input!.dispatchEvent(new Event('change', { bubbles: true }))
    await new Promise(resolve => setTimeout(resolve, 30))
  })

  expect(text('.source')).toContain('导入 · day.json')
  expect(text('.overview')).toContain('imported-plant')
  expect(text('.datanote')).toContain('7 月 1 日回放')
})

it('导入不合法的 day.json 时保留当前数据并显示错误', async () => {
  await act(async () => root.render(<App initialDay={structuredClone(sample)} />))
  const bad = structuredClone(sample) as unknown as Record<string, any>
  bad.events[0].at = 'bad'
  const file = new File([JSON.stringify(bad)], 'bad.json', { type: 'application/json' })
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  await act(async () => {
    input.dispatchEvent(new Event('change', { bubbles: true }))
    await new Promise(resolve => setTimeout(resolve, 30))
  })

  expect(text('.importerror')).toContain('events[0].at')
  expect(text('.overview')).toContain('厂区数字孪生')
})

it('切换标签页会暂停自动巡演、显示对应页面并出现回到剧情', async () => {
  await act(async () => root.render(<App initialDay={structuredClone(sample)} />))

  expect(text('.tourchip')).toContain('自动巡演 1 / 8')
  expect(container.querySelector('.returnbtn')).toBeNull()

  await click('[data-tab="agent"]')
  expect(container.querySelector('.tab[aria-selected="true"]')?.getAttribute('data-tab')).toBe('agent')
  expect(container.querySelector('[data-page="agent"]')).toBeTruthy()
  expect(text('.tourchip')).toContain('自由探索')
  expect(text('.returnbtn')).toContain('回到剧情')
})

it('第三幕最终已确认或完成的工单在确认前仍显示，确认后退出待人工，回看可恢复', async () => {
  const day = structuredClone(sample)
  const at = (minute: number) => dayAtHour(day, 14) + minute * 60_000
  day.tickets = [{ ticket_id: 'TK-003', opened_at: new Date(at(20)).toISOString(), closed_at: new Date(at(50)).toISOString(), status: 'closed', round: 1, event_ids: ['act3-trapped'] }]
  day.events = [
    { event_id: 'act3-trapped', at: new Date(at(20)).toISOString(), event_type: 'elevator_trapped', source: 'script', summary: '货梯困人', nature: 'synthetic' },
    { event_id: 'operator-confirm', at: new Date(at(30)).toISOString(), event_type: 'operator_confirm', source: 'operator_message', summary: '确认救援', payload: { event: 'act3-trapped' }, nature: 'synthetic' },
  ]

  const renderOverview = async (cursor: number) => {
    await act(async () => root.render(
      <Overview
        day={day}
        cursor={cursor}
        selected={null}
        onSelect={() => { /* 测试里不点场景 */ }}
        campusRef={{ current: null }}
        openEvidence={() => { /* 测试里不开抽屉 */ }}
      />,
    ))
  }

  for (const status of ['confirmed', 'done']) {
    day.work_orders = [{ work_order_id: 'WO-003', ticket_id: 'TK-003', created_at: new Date(at(23)).toISOString(), reason: '值班确认救援方案', status }]
    for (const [minute, pending] of [[22, 0], [23, 1], [29, 1], [30, 0], [50, 0], [24, 1]]) {
      await renderOverview(at(minute))
      expect(container.querySelectorAll('.handoff-row')).toHaveLength(pending)
      if (pending) expect(text('.handoff-row')).toContain('值班确认救援方案')
    }
    expect(humanHandoffs(day, at(24)).orders).toHaveLength(1)
    expect(humanHandoffs(day, at(50)).orders).toHaveLength(0)
  }
})

it('总览和主 Agent 页的结论取主事件单的裁决，08:07 的转人工不覆盖主结论', async () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const cursor = dayAtHour(day, 8) + 7 * 60_000
  const noop = () => { /* 测试里不跳转、不开抽屉 */ }

  await act(async () => root.render(
    <Overview day={day} cursor={cursor} selected={null} onSelect={noop}
      campusRef={{ current: null }} openEvidence={noop} />,
  ))
  expect(text('.conclusion-flag')).toContain('自动执行')
  expect(text('.conclusion-flag')).not.toContain('转人工')
  expect(text('.conclusion-summary')).toContain('三方合并')
  expect(text('.handoff')).toContain('待确认事项')

  await act(async () => root.render(<MainAgentPage day={day} cursor={cursor} openEvidence={noop} />))
  expect(text('.ex-verdict')).toContain('自动执行')
  expect(text('.ex-verdict')).toContain('采纳 3 / 3')
  expect(text('.ex-pending')).toContain('转人工')
  expect(container.querySelectorAll('.ex-table tbody tr')).toHaveLength(3)
})

it('URL 带 run_id 时从后端读这一天，来源标为后端运行', async () => {
  window.history.replaceState({}, '', '/showcase.html?run_id=run-online')
  const online = { schema_version: '1', plant_id: 'online-plant', local_date: '2026-07-02', timezone: 'Asia/Shanghai', run_id: 'run-online', runs: [{ run_id: 'run-online' }] }
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(online), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetcher)

  await act(async () => { root.render(<App />) })
  await settle()

  expect(fetcher).toHaveBeenCalledWith('/api/v1/demo/day/run-online', expect.objectContaining({ cache: 'no-store' }))
  expect(text('.source')).toContain('后端 · 运行 run-online')
  expect(container.querySelector('.sourcenote')).toBeNull()
  expect(text('.overview')).toContain('online-plant')
})

it('后端没有这次运行、day.json 也读不到时回退内置样例，来源旁给出白话原因', async () => {
  window.history.replaceState({}, '', '/showcase.html?run_id=run-missing')
  const errorBody = { error: { code: 'not_found', message: '未找到导出的一天', retryable: false, details: {} } }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) =>
    String(input).includes('/api/v1/demo/day/')
      ? Promise.resolve(new Response(JSON.stringify(errorBody), { status: 404, headers: { 'Content-Type': 'application/json' } }))
      : Promise.reject(new TypeError('fetch failed'))))

  await act(async () => { root.render(<App />) })
  await settle()

  expect(text('.source')).toContain('标准演示场景 · 含合成补全')
  expect(text('.sourcenote')).toContain('后端没有这次运行')
  expect(text('.overview')).toContain('厂区数字孪生')
})

it('file:// 打开带 run_id 时说明离线连不上后端，回退内置样例且不请求后端', async () => {
  const fetcher = vi.fn().mockRejectedValue(new TypeError('fetch failed'))
  const result = await loadDay(fetcher, { search: '?run_id=run-file', protocol: 'file:' })

  expect(result.source.kind).toBe('sample')
  expect(result.source.note).toBe('离线打开，连不上后端')
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/api/'))).toBe(false)
})

it('后端 400 时显示 ErrorResponse 里的具体原因，502 时报错不回退样例', async () => {
  const detail = '文件与运行编号不一致：文件中的 run_id 为 run-other，请用该编号命名并加载'
  const fetcher400 = vi.fn((input: RequestInfo | URL) =>
    String(input).includes('/api/v1/demo/day/')
      ? Promise.resolve(new Response(JSON.stringify({ error: { code: 'invalid_request', message: detail } }), { status: 400, headers: { 'Content-Type': 'application/json' } }))
      : Promise.resolve(new Response('', { status: 404 })))
  const result400 = await loadDay(fetcher400, { search: '?run_id=run-x', protocol: 'http:' })
  expect(result400.source.kind).toBe('sample')
  expect(result400.source.note).toBe(detail)

  const fetcher502 = vi.fn((input: RequestInfo | URL) =>
    String(input).includes('/api/v1/demo/day/')
      ? Promise.resolve(new Response('', { status: 502 }))
      : Promise.resolve(new Response('', { status: 404 })))
  await expect(loadDay(fetcher502, { search: '?run_id=run-x', protocol: 'http:' })).rejects.toThrow('后端出错（HTTP 502）')
})

it('后端来源同样按 208 的余量、引用资料过滤与慢通道裁决口径计算', async () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(day), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
  const loaded = await loadDay(fetcher, { search: '?run_id=run-208', protocol: 'http:' })
  expect(loaded.source.kind).toBe('backend')

  const morning = dayAtHour(loaded.day, 8) + 7 * 60_000
  expect(readings(loaded.day, morning).find(item => item.key === 'transformer')?.note).toContain('超上限 11 个百分点')

  const allRefs = new Set(day.proposals.flatMap(proposal => proposal.evidence_refs ?? [])).size
  expect(todayStats(loaded.day, morning).evidence).toBeGreaterThan(0)
  expect(todayStats(loaded.day, morning).evidence).toBeLessThan(allRefs)

  const chain = decisionChain(loaded.day, dayAtHour(loaded.day, 16) + 30 * 60_000)
  expect(chain?.decision.decision_id).toBe('decision://dc-0005')
})

it('在线模式证据抽屉按运行分组显示中文事件、工具与依据，不露内部编号', async () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const cursor = dayAtHour(day, 9)
  const evidence = evidenceFor(day, cursor, null, '测试依据', 'ticket://tk-0001')
  evidence.runs = ['ar-main', 'ar-hvac'].map(runId => ({ ...evidence.runs[0], runId }))
  const events = [
    { event_id: 'event://ev-1', seq: 1, occurred_at: '2026-07-01T01:31:05Z', agent_run_id: 'ar-main', run_id: 'run-evidence', trace_id: 'tr-1', call_id: 'call-1', event_type: 'tool_called', tool: 'ts_query', path: 'reasoning', rule_ids: [], sources: [], result_status: 'ok', summary: '查询 T1 变压器负载与峰段电价' },
    { event_id: 'event://ev-2', seq: 2, occurred_at: '2026-07-01T01:31:07Z', agent_run_id: 'ar-main', run_id: 'run-evidence', trace_id: 'tr-1', call_id: 'call-1', event_type: 'rule_applied', tool: null, path: 'reasoning', rule_ids: ['rule://tou-2026#peak'], sources: [{ evidence_id: 'document://maintenance-manual' }], result_status: 'degraded', summary: '峰段电价规则适用，峰值时段前移' },
    { event_id: 'event://ev-3', seq: 3, occurred_at: '2026-07-01T01:35:00Z', agent_run_id: 'ar-hvac', run_id: 'run-evidence', trace_id: 'tr-2', call_id: null, event_type: 'action_decided', tool: null, path: 'reasoning', rule_ids: [], sources: [], result_status: 'ok', summary: 'u-reviewer confirmed：提议 work_order' },
  ]
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    const items = url.includes('agent_run_id=ar-main') ? events.slice(0, 2) : events
    return Promise.resolve(new Response(JSON.stringify({ items, next_cursor: null }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  })
  vi.stubGlobal('fetch', fetcher)

  await act(async () => root.render(
    <EvidenceDrawer
      evidence={evidence}
      cursor={cursor}
      timezone={day.timezone}
      initialTab="rule"
      source={{ kind: 'backend', label: '后端 · 运行 run-evidence' }}
      onlineRunId="run-evidence"
      onClose={() => { /* 测试里不关抽屉 */ }}
    />,
  ))
  expect(fetcher).not.toHaveBeenCalled()

  const runsTab = [...container.querySelectorAll<HTMLButtonElement>('.drawer-tab')]
    .find(button => button.textContent === 'Agent 运行记录')
  expect(runsTab).toBeTruthy()
  await act(async () => { runsTab!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await settle()

  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(String(fetcher.mock.calls[0][0])).toContain('/api/v1/evidence?run_id=run-evidence')
  const body = text('.drawer-body')
  expect(body).toContain('第 1 次运行 · 09:31:05 开始 · 2 条记录')
  expect(body).toContain('第 2 次运行 · 09:35:00 开始 · 1 条记录')
  expect(body).toContain('调用工具')
  expect(body).toContain('查询时序数据')
  expect(body).toContain('适用规则')
  expect(body).toContain('降级完成')
  expect(body).toContain('分时电价·峰段')
  expect(body).toContain('空压机运行维护手册')
  expect(body).toContain('值班人员已确认：提请确认操作工单')
  for (const internal of ['tool_called', 'rule_applied', 'plan_created', 'ts_query', 'agent_run_', 'ar-main', 'event://', 'rule://', 'document://', 'run-evidence', 'u-reviewer', 'work_order', 'confirmed']) {
    expect(body).not.toContain(internal)
  }

  const group = container.querySelector<HTMLButtonElement>('.rungroup')
  await act(async () => { group!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await settle()
  expect(String(fetcher.mock.calls[1][0])).toContain('agent_run_id=ar-main')
  expect(text('.drawer-body')).toContain('第 1 次运行 · 09:31:05 开始 · 2 条记录')
  expect(text('.drawer-body')).toContain('看全部')
  expect(text('.drawer-body')).not.toContain('第 2 次运行')

  const showAll = container.querySelector<HTMLButtonElement>('.allruns')
  await act(async () => { showAll!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await settle()
  expect(text('.drawer-body')).toContain('第 2 次运行')
  expect(container.querySelector('.allruns')).toBeNull()

  // 无关联记录时，在线接口返回的其他事件单也不能显示。
  await act(async () => root.render(
    <EvidenceDrawer evidence={{ ...evidence, runs: [] }} cursor={cursor} timezone={day.timezone} initialTab="runs"
      source={{ kind: 'backend', label: '后端 · 运行 run-evidence' }} onlineRunId="run-evidence" onClose={() => {}} />,
  ))
  expect(container.querySelectorAll('.onlinerun')).toHaveLength(0)
})

it('点某组后请求失败时错误态也能看全部，点它重新取全部', async () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const cursor = dayAtHour(day, 9)
  const evidence = evidenceFor(day, cursor, null, '测试依据', 'ticket://tk-0001')
  evidence.runs = [{ ...evidence.runs[0], runId: 'ar-main' }]
  const events = [
    { event_id: 'event://ev-1', seq: 1, occurred_at: '2026-07-01T01:31:05Z', agent_run_id: 'ar-main', event_type: 'tool_called', result_status: 'ok', summary: null },
    { event_id: 'event://ev-2', seq: 2, occurred_at: '2026-07-01T01:31:07Z', agent_run_id: 'ar-main', event_type: 'rule_applied', result_status: 'ok', summary: null },
  ]
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('agent_run_id=')) return Promise.reject(new TypeError('fetch failed'))
    return Promise.resolve(new Response(JSON.stringify({ items: events, next_cursor: null }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  })
  vi.stubGlobal('fetch', fetcher)

  await act(async () => root.render(
    <EvidenceDrawer
      evidence={evidence}
      cursor={cursor}
      timezone={day.timezone}
      initialTab="runs"
      source={{ kind: 'backend', label: '后端 · 运行 run-fallback' }}
      onlineRunId="run-fallback"
      onClose={() => { /* 测试里不关抽屉 */ }}
    />,
  ))
  await settle()
  expect(fetcher).toHaveBeenCalledTimes(1)

  const group = container.querySelector<HTMLButtonElement>('.rungroup')
  expect(group).toBeTruthy()
  await act(async () => { group!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await settle()
  expect(text('.onlineerror')).toContain('连不上后端')

  const showAll = container.querySelector<HTMLButtonElement>('.allruns')
  expect(showAll?.textContent).toBe('看全部')
  await act(async () => { showAll!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await settle()
  expect(String(fetcher.mock.calls[fetcher.mock.calls.length - 1][0])).not.toContain('agent_run_id=')
})

it('点某组返回空时也给出“看全部”', async () => {
  const onShowAll = vi.fn()
  await act(async () => root.render(
    <OnlineRunsHistory
      state={{ state: 'ready', groups: [] }}
      timezone="Asia/Shanghai"
      only={{ runId: 'ar-main', ordinal: 1, startedAt: '2026-07-01T01:31:00Z', events: [] }}
      onPick={() => { /* 测试里不点 */ }}
      onShowAll={onShowAll}
      onRetry={() => { /* 测试里不用 */ }}
    />,
  ))

  const showAll = container.querySelector<HTMLButtonElement>('.allruns')
  expect(showAll?.textContent).toBe('看全部')
  await act(async () => { showAll!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  expect(onShowAll).toHaveBeenCalledTimes(1)
})

it('回退到 day.json 或内置样例时证据抽屉用 day 里的 agent_runs，不请求在线证据', async () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const cursor = dayAtHour(day, 9)
  const evidence = evidenceFor(day, cursor, null, '测试依据', 'ticket://tk-0001')
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)

  await act(async () => root.render(
    <EvidenceDrawer
      evidence={evidence}
      cursor={cursor}
      timezone={day.timezone}
      initialTab="runs"
      source={{ kind: 'exported', label: 'day.json · 2026-07-01' }}
      onlineRunId="run-fallback"
      onClose={() => { /* 测试里不关抽屉 */ }}
    />,
  ))
  await settle()

  expect(fetcher).not.toHaveBeenCalled()
  expect(container.querySelectorAll('.runrow').length).toBeGreaterThan(0)
  expect(text('.drawer-body')).not.toContain('正在读取运行记录')
  expect(text('.drawer-body')).not.toContain('这次运行 ·')
})

it('在线证据摘要为空时不占一行，也不写“没有补充说明”', async () => {
  const groups = buildEvidenceRuns(
    [{ occurred_at: '2026-07-01T01:31:05Z', event_type: 'tool_called', result_status: 'ok', summary: null }],
    'Asia/Shanghai',
  )
  expect(groups[0].events[0].summary).toBe('')

  await act(async () => root.render(
    <OnlineRunsHistory
      state={{ state: 'ready', groups }}
      timezone="Asia/Shanghai"
      only={null}
      onPick={() => { /* 测试里不点 */ }}
      onShowAll={() => { /* 测试里不点 */ }}
      onRetry={() => { /* 测试里不用 */ }}
    />,
  ))

  expect(container.querySelector('.eventsum')).toBeNull()
  expect(container.textContent).not.toContain('没有补充说明')
})

it('StrictMode 下在线证据只请求一次', async () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const cursor = dayAtHour(day, 9)
  const evidence = evidenceFor(day, cursor, null, '测试依据', 'ticket://tk-0001')
  evidence.runs = [{ ...evidence.runs[0], runId: 'ar-main' }]
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
    items: [{ seq: 1, occurred_at: '2026-07-01T01:31:05Z', agent_run_id: 'ar-main', event_type: 'tool_called', result_status: 'ok', summary: '查询 T1 负载' }],
    next_cursor: null,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetcher)

  await act(async () => root.render(
    <StrictMode>
      <EvidenceDrawer
        evidence={evidence}
        cursor={cursor}
        timezone={day.timezone}
        initialTab="rule"
        source={{ kind: 'backend', label: '后端 · 运行 run-strict' }}
        onlineRunId="run-strict"
        onClose={() => { /* 测试里不关抽屉 */ }}
      />
    </StrictMode>,
  ))
  const runsTab = [...container.querySelectorAll<HTMLButtonElement>('.drawer-tab')]
    .find(button => button.textContent === 'Agent 运行记录')
  await act(async () => { runsTab!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await settle()

  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(text('.drawer-body')).toContain('第 1 次运行')
})

it('总览右栏的曲线卡固定在滚动区外，结论、动作与按钮在滚动区内', async () => {
  const day = withShowcaseDemo(structuredClone(sample))
  const cursor = dayAtHour(day, 8) + 7 * 60_000
  const noop = () => { /* 测试里不跳转、不开抽屉 */ }

  await act(async () => root.render(
    <Overview day={day} cursor={cursor} selected={null} onSelect={noop}
      campusRef={{ current: null }} openEvidence={noop} />,
  ))
  const rail = container.querySelector('.rail.right')
  expect(rail).toBeTruthy()
  const kids = Array.from(rail!.children)
  expect(kids.map(element => element.className)).toEqual(['railscroll', 'sec curvecard'])
  const scroll = kids[0]
  expect(scroll.querySelector('.conclusion')).toBeTruthy()
  expect(scroll.querySelectorAll('.actionrow')).toHaveLength(3)
  expect(scroll.querySelector('.actionbtns')).toBeTruthy()
  expect(scroll.querySelector('.curvecard')).toBeNull()
})
