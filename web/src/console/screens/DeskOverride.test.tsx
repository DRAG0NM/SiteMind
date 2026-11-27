// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { buildConsoleModel } from '../data'
import { matchApplied, useAutonomy, type AppliedAction } from '../override'
import { sample, time, useDay, type Day } from '../../store'
import { DeskScreen } from './Desk'

let container: HTMLDivElement
let root: Root

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const settle = () => new Promise(resolve => setTimeout(resolve, 0))
const textButton = (text: string) => [...container.querySelectorAll('button')].find(node => node.textContent?.trim() === text) ?? null
const click = async (target: HTMLElement | null) => {
  expect(target).toBeTruthy()
  await act(async () => { target!.dispatchEvent(new MouseEvent('click', { bubbles: true })); await settle() })
}

function Harness() {
  const { day, cursor, source, playing, paused, pausedAt, mode, verdicts, retracted, cancelledRules } = useDay()
  return <DeskScreen model={buildConsoleModel(day, cursor, source, { playing, paused, pausedAt, mode, verdicts, retracted, cancelledRules })} />
}

const status = (over: Record<string, unknown> = {}) => ({
  paused: false, scope: 'plant', run_id: null, by: null, since: null, reason: null, resumed_by: null, resumed_at: null, backlog: 0, ...over,
})

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const day: Day = structuredClone(sample)
  useDay.getState().load(day, '干预冒烟')
  useDay.getState().seek(time('2026-07-01T00:07:00Z'))
  useAutonomy.setState({ status: null, applied: [], error: '' })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

const rowOf = () => {
  const { day, cursor, source } = useDay.getState()
  const row = buildConsoleModel(day, cursor, source, { playing: false, paused: false, pausedAt: null, mode: 'replay', verdicts: {}, retracted: [], cancelledRules: [] }).actions[0]
  expect(row).toBeTruthy()
  return row
}

it('matchApplied 按对象与命令匹配，结束时刻一致优先，已结束的不匹配', () => {
  const row = rowOf()
  const make = (id: string, stage: AppliedAction['stage'], end: number): AppliedAction => ({
    action_id: id, run_id: 'r', entity_id: row.entityId, command: row.command, params: {},
    interval: { start: new Date(row.startedAt).toISOString(), end: new Date(end).toISOString() },
    stage, protection: false, ticket_id: null, proposal_id: null, temp_rule_id: null,
  })
  const items = [make('A-1', 'active', row.endsAt + 60000), make('A-2', 'active', row.endsAt), make('A-3', 'ended', row.endsAt)]
  expect(matchApplied(row, items)?.action_id).toBe('A-2')
  expect(matchApplied(row, [items[2]])).toBeNull()
})

it('后端暂停状态显示谁暂停、积压几条；暂停与恢复走真接口', async () => {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push(`${init?.method ?? 'GET'} ${url}`)
    if (url.includes('autonomy/status')) return Promise.resolve(json(status({ paused: true, by: 'u-reviewer', since: '2026-07-01T00:00:00Z', reason: '巡检', backlog: 3 })))
    if (url.includes('autonomy/resume')) return Promise.resolve(json(status()))
    return Promise.resolve(json({ items: [] }))
  }))
  await act(async () => { root.render(<Harness />); await settle() })
  expect(container.textContent).toContain('由 u-reviewer 暂停')
  expect(container.textContent).toContain('积压 3 条')
  await click(textButton('恢复自动执行'))
  expect(calls).toContain('POST /api/v1/autonomy/resume')
})

it('撤销已生效动作：先显示预计影响，原因二选一，确认后带原因类型调撤销接口', async () => {
  const row = rowOf()
  const applied = {
    action_id: 'A-00009', run_id: 'run-1', entity_id: row.entityId, command: row.command, params: {},
    interval: { start: new Date(row.startedAt).toISOString(), end: new Date(row.endsAt).toISOString() },
    stage: 'active', protection: false, ticket_id: null, proposal_id: null, temp_rule_id: null,
  }
  const preview = {
    action_id: 'A-00009', run_id: 'run-1', window: { start: '2026-07-01T00:07:00Z', end: '2026-07-01T00:37:00Z' },
    revoke: { cost_cny: 10, violation_minutes: 0, t1_peak_kw: 1400 }, keep: { cost_cny: 12.5, violation_minutes: 2, t1_peak_kw: 1450 },
    cost_delta_cny: -2.5, violation_minutes_delta: -2, t1_peak_delta_kw: -50, protection: false, note: '',
  }
  const fetcher = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('autonomy/status')) return Promise.resolve(json(status()))
    if (url.includes('autonomy/actions')) return Promise.resolve(json({ items: [applied] }))
    if (url.includes('revoke-preview')) return Promise.resolve(json(preview))
    if (url.includes('/revoke')) return Promise.resolve(json({ kind: 'revoked' }))
    return Promise.resolve(json({ items: [] }))
  })
  vi.stubGlobal('fetch', fetcher)
  await act(async () => { root.render(<Harness />); await settle() })
  await click(textButton('撤销'))
  expect(document.body.textContent).toContain('−2.50 元')
  expect(document.body.textContent).toContain('−2 分钟')
  expect(document.body.textContent).toContain('−50.0 kW')
  const other = document.body.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1]
  await click(other)
  await click([...document.body.querySelectorAll('button')].find(node => node.textContent === '确认撤销') ?? null)
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 300)) })  // 对话框关闭动画 160ms 后才回调
  const post = fetcher.mock.calls.find(call => String(call[0]).endsWith('/actions/A-00009/revoke'))
  expect(post).toBeTruthy()
  expect(JSON.parse(String((post![1] as RequestInit).body))).toMatchObject({ reason_kind: 'other', confirm_protection: false, run_id: 'run-1' })
  expect(container.textContent).toContain('已撤销')
})

it('取消还没生效的保护动作：要写说明并勾二次确认，请求体带 confirm_protection', async () => {
  const row = rowOf()
  const applied = {
    action_id: 'A-00010', run_id: 'run-1', entity_id: row.entityId, command: row.command, params: {},
    interval: { start: new Date(row.startedAt).toISOString(), end: new Date(row.endsAt).toISOString() },
    stage: 'scheduled', protection: true, ticket_id: null, proposal_id: null, temp_rule_id: null,
  }
  const fetcher = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    if (url.includes('autonomy/status')) return Promise.resolve(json(status()))
    if (url.includes('autonomy/actions')) return Promise.resolve(json({ items: [applied] }))
    if (url.includes('/cancel')) return Promise.resolve(json({ kind: 'cancelled' }))
    return Promise.resolve(json({ items: [] }))
  })
  vi.stubGlobal('fetch', fetcher)
  await act(async () => { root.render(<Harness />); await settle() })
  await click(textButton('取消'))
  const confirm = () => [...document.body.querySelectorAll('button')].find(node => node.textContent === '确认取消') as HTMLButtonElement
  expect(confirm().disabled).toBe(true)
  expect(document.body.textContent).toContain('我确认要取消这条保护动作')
  const input = document.body.querySelector<HTMLInputElement>('.dlg-input')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '现场已处理')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(confirm().disabled).toBe(true) // 只写说明还不够，还要勾二次确认
  await click(document.body.querySelector<HTMLInputElement>('input[type="checkbox"]'))
  expect(confirm().disabled).toBe(false)
  await click(confirm())
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 300)) })
  const post = fetcher.mock.calls.find(call => String(call[0]).endsWith('/actions/A-00010/cancel'))
  expect(JSON.parse(String((post![1] as RequestInit).body))).toMatchObject({ reason: '现场已处理', confirm_protection: true })
})
