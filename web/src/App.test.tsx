// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { App } from './App'
import { sample, useDay, type Day } from './store'

let container: HTMLDivElement
let root: Root

const json = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
})

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  useDay.getState().load(structuredClone(sample), '回归样例')
  window.history.replaceState(null, '', '#wall')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ items: [] })))
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

const settle = () => new Promise(resolve => setTimeout(resolve, 0))

it('空绑定或缺少绑定的合法提案不会让态势墙白屏', async () => {
  const day: Day = structuredClone(sample)
  day.proposals = day.proposals.map((proposal, index) => {
    if (index === 0) return { ...proposal, binding: null }
    const { binding, ...unbound } = proposal
    return unbound
  })
  useDay.getState().load(day, '空绑定回归')
  await act(async () => { root.render(<App />); await settle() })
  expect(container.textContent).toContain('厂区态势')
  expect(container.textContent).toContain('全厂负荷')
  expect(container.textContent).toContain('待处理事项')
})

it('午夜跳过的时刻显示实际日界时间', async () => {
  useDay.getState().load({
    ...structuredClone(sample),
    ledger_minutes: [],
    timezone: 'America/Santiago',
    local_date: '2026-09-06',
  }, '夏令时回归')
  await act(async () => { root.render(<App />); await settle() })
  expect(container.querySelector('.replay-start')?.textContent).toBe('01:00')
  expect(container.querySelector('.replay-end')?.textContent).toBe('24:00')
})

it('工作台显示 Watchdog 受阻原因和恢复记录', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({
    items: [{
      ticket_id: 'ticket://TK-0003',
      ticket_status: 'blocked',
      reason: '模型断开',
      checked_at: '2026-11-07T00:00:00Z',
      recovery: '重新检查失败',
    }],
  })))
  await act(async () => { root.render(<App />); await settle() })
  const deskTab = container.querySelector<HTMLButtonElement>('[data-screen="desk"]')!
  await act(async () => { deskTab.click(); await settle() })
  expect(container.textContent).toContain('ticket://TK-0003')
  expect(container.textContent).toContain('模型断开')
  expect(container.textContent).toContain('重新检查失败')
})

it('只有 Watchdog 受阻时计数和空状态正确', async () => {
  // 只留 watchdog 一条待办：事件单、工单、裁决队列与知识发布都清掉，避免 08:07 样例里的其它待办。
  useDay.getState().load({
    ...structuredClone(sample), tickets: [], work_orders: [], decisions: [], knowledge_versions: [],
  }, '空待办')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({
    items: [{
      ticket_id: 'ticket://TK-0003',
      ticket_status: 'blocked',
      reason: '模型断开',
      checked_at: '',
      recovery: '重新检查失败',
    }],
  })))
  await act(async () => { root.render(<App />); await settle() })
  expect(container.querySelector('[data-screen="desk"] .badge')?.textContent).toBe('1')
  expect(container.querySelector('.wall-todo')?.textContent).not.toContain('暂无')
})

it('Watchdog 502 时轮询退避，界面继续显示暂不可用且只打一次调试日志', async () => {
  vi.useFakeTimers()
  const debug = vi.spyOn(console, 'debug').mockImplementation(() => { /* 只断言调用次数 */ })
  const fetchMock = vi.fn().mockResolvedValue(new Response('', { status: 502 }))
  vi.stubGlobal('fetch', fetchMock)
  // #245 之后挂载时还会发两个独立接口请求，退避只看真正的 Watchdog 请求。
  const watchdogCalls = () => fetchMock.mock.calls
    .filter(([input]) => String(input).includes('/api/v1/watchdog/blocked')).length
  try {
    await act(async () => { root.render(<App />); await vi.advanceTimersByTimeAsync(0) })
    expect(container.textContent).toContain('Watchdog · 回放模式')
    expect(watchdogCalls()).toBe(1)

    // 第一次失败后先等 60 秒再查。
    await act(async () => { await vi.advanceTimersByTimeAsync(59_000) })
    expect(watchdogCalls()).toBe(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(watchdogCalls()).toBe(2)

    // 第二次失败后间隔拉到 2 分钟，之后继续逐步退避。
    await act(async () => { await vi.advanceTimersByTimeAsync(119_000) })
    expect(watchdogCalls()).toBe(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(watchdogCalls()).toBe(3)
    await act(async () => { await vi.advanceTimersByTimeAsync(180_000) })
    expect(watchdogCalls()).toBe(4)

    expect(debug.mock.calls.filter(([message]) => String(message).includes('Watchdog'))).toHaveLength(1)
    expect(container.textContent).toContain('Watchdog · 回放模式')
  } finally {
    debug.mockRestore()
    vi.useRealTimers()
  }
})

