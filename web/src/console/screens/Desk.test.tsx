// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { buildConsoleModel } from '../data'
import { sample, time, useDay, type Day } from '../../store'
import { DeskScreen } from './Desk'

let container: HTMLDivElement
let root: Root

const json = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
})
const page = (items: unknown[]) => json({ items })
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

function Harness() {
  const { day, cursor, source, playing, paused, pausedAt, mode, verdicts, retracted, cancelledRules } = useDay()
  const model = buildConsoleModel(day, cursor, source, { playing, paused, pausedAt, mode, verdicts, retracted, cancelledRules })
  return <DeskScreen model={model} />
}

const click = async (target: HTMLElement | null) => {
  expect(target).toBeTruthy()
  await act(async () => { target!.dispatchEvent(new MouseEvent('click', { bubbles: true })); await settle() })
}

const button = (label: string) => [...container.querySelectorAll('button')].find(node => node.textContent?.trim() === label) ?? null
const textButton = (text: string) => [...container.querySelectorAll('button')].find(node => node.textContent?.includes(text)) ?? null
const byLabel = (label: string) => container.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[aria-label="${label}"]`)

async function setValue(target: HTMLInputElement | HTMLTextAreaElement | null, value: string) {
  expect(target).toBeTruthy()
  const prototype = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')!.set!
  await act(async () => {
    setter.call(target, value)
    target!.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

const bodyOf = (call: unknown[] | undefined) => JSON.parse(String((call?.[1] as RequestInit | undefined)?.body ?? '{}')) as Record<string, unknown>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const day: Day = structuredClone(sample)
  day.decisions[0].result.outcome = 'to_human'
  useDay.getState().load(day, '处置席冒烟')
  useDay.getState().seek(time('2026-07-01T00:07:00Z'))
  // 默认没有后端：/actions 读取失败，退回 day 记录合成的演示卡片
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

it('处置席显示队列，批准后保留裁定并从待办计数里减去', async () => {
  await act(async () => { root.render(<Harness/>); await settle() })
  const pending = () => Number(container.querySelector('.desk-left .desk-sub')?.textContent?.match(/(\d+) 件/)?.[1])
  expect(container.textContent).toContain('待处理事项')
  expect(container.textContent).toContain('按优先级排序')
  expect(container.textContent).toContain('储能放电削峰')
  const before = pending()
  expect(before).toBeGreaterThan(0)

  await click(button('批准'))

  expect(useDay.getState().verdicts['decision:decision://dc-0001']).toBe('approved')
  expect(container.textContent).toContain('已批准')
  expect(pending()).toBe(before - 1)
  expect(container.textContent).not.toContain('从左边选一件处理')
})

it('真实 /actions 返回时以真接口为准：回答卡把答案放进确认 reason', async () => {
  const question = {
    action_id: 'act-q1', kind: 'question', status: 'proposed', revision: 0,
    payload: { target_ref: 'question://tk-0004-1', question_id: 'question://tk-0004-1', question: '轿厢里还有人吗？', ticket_id: 'ticket://tk-0004' },
    target_ref: 'question://tk-0004-1', proposed_at: '2026-07-01T00:07:00Z', reason: null,
  }
  const fetcher = vi.fn((input: RequestInfo | URL) => Promise.resolve(
    String(input).startsWith('/api/v1/actions?') ? page([question]) : page([])))
  vi.stubGlobal('fetch', fetcher)

  await act(async () => { root.render(<Harness/>); await settle() })
  expect(container.textContent).toContain('真实接口 · question')
  await click(textButton('轿厢里还有人吗？'))
  await setValue(byLabel('回答'), '轿厢里有人')
  await click(button('提交回答'))

  const post = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-q1/confirm')
  expect(post).toBeTruthy()
  expect(bodyOf(post)).toMatchObject({ reason: '轿厢里有人', expected_revision: 0 })
  expect(String(bodyOf(post).idempotency_key)).toContain('act-q1')
  expect(useDay.getState().verdicts['action:act-q1']).toBe('approved')
  expect(container.textContent).toContain('已回答')
})

it('复核项判错走普通 reject：不带 edits，勾选值只记前端处置记录', async () => {
  const review = {
    action_id: 'act-r1', kind: 'review', status: 'proposed', revision: 2,
    payload: { target_ref: 'review-tk-0001-1-manual-0', item_id: 'review-tk-0001-1-manual-0', ticket_id: 'ticket://tk-0001', kind: 'review' },
    target_ref: 'review-tk-0001-1-manual-0', proposed_at: '2026-07-01T00:07:00Z', reason: null,
  }
  const fetcher = vi.fn((input: RequestInfo | URL) => Promise.resolve(
    String(input).startsWith('/api/v1/actions?') ? page([review]) : page([])))
  vi.stubGlobal('fetch', fetcher)

  await act(async () => { root.render(<Harness/>); await settle() })
  await click(textButton('核对 tk-0001 的处置'))
  await click(container.querySelector<HTMLInputElement>('input[type="checkbox"]'))
  await click(textButton('判错（要复核）'))

  const post = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-r1/reject')
  expect(post).toBeTruthy()
  const body = bodyOf(post)
  expect(body).toMatchObject({ expected_revision: 2 })
  expect('edits' in body).toBe(false)
  expect(useDay.getState().verdicts['action:act-r1']).toBe('rejected')
  expect(container.textContent).toContain('不是系统判断有误 · 仅前端处置记录')
})

it('纠错卡提交后保留演示结果：离线演示记下裁定并给演示沉淀', async () => {
  await act(async () => { root.render(<Harness/>); await settle() })
  await click(textButton('照明与空压同时异常'))
  await setValue(byLabel('一句话纠正'), '先查配电回路')
  await click(button('提交纠错'))

  expect(useDay.getState().verdicts['correction:ticket://tk-0005']).toBe('approved')
  expect(container.textContent).toContain('已提交纠错')
  expect(container.textContent).toContain('离线演示')
  expect(container.textContent).toContain('纠错沉淀')
})

it('真实动作确认失败时不记裁定：保留待办、错误一直显示，切换到别的条目也不清', async () => {
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/actions?')) {
      return Promise.resolve(page([{
        action_id: 'act-w9', kind: 'work_order', status: 'proposed', revision: 0,
        payload: { work_order_id: 'wo-9', ticket_id: 'ticket://tk-0004', reason: '货梯困人涉及安全' },
        target_ref: 'wo-9', proposed_at: '2026-07-01T00:07:00Z', reason: null,
      }]))
    }
    return Promise.reject(new Error('后端 500'))
  })
  vi.stubGlobal('fetch', fetcher)
  await act(async () => { root.render(<Harness/>); await settle() })
  await click(textButton('货梯困人涉及安全'))
  await click(button('已到场确认'))

  expect(useDay.getState().verdicts['action:act-w9']).toBeUndefined()
  expect(container.textContent).toContain('后端接口失败')
  expect(container.textContent).toContain('仍待处理')

  // 切到别的条目再切回来，失败原因还在
  await click(textButton('照明与空压同时异常'))
  await click(textButton('货梯困人涉及安全'))
  expect(container.textContent).toContain('后端接口失败')
})

it('真接口失败时退回离线演示卡片，合成件在队列里标出来', async () => {
  await act(async () => { root.render(<Harness/>); await settle() })
  expect(container.textContent).toContain('货梯困人：轿厢里还有人吗？')
  expect(container.textContent).toContain('照明与空压同时异常')
  expect(container.textContent).toContain('试算损失 ¥62,000 超重大损失线')
  expect(container.textContent).toContain('离线演示')
})

function mockActions(actions: unknown[]) {
  const fetcher = vi.fn((input: RequestInfo | URL) => Promise.resolve(
    String(input).startsWith('/api/v1/actions?') ? page(actions) : page([])))
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

it('真实操作工单卡的“已到场确认”走确认接口，带修订且不带 edits', async () => {
  const fetcher = mockActions([{
    action_id: 'act-w1', kind: 'work_order', status: 'proposed', revision: 1,
    payload: { work_order_id: 'wo-1', ticket_id: 'ticket://tk-0004', reason: '货梯困人涉及安全，需值班确认救援方案' },
    target_ref: 'wo-1', proposed_at: '2026-07-01T00:07:00Z', reason: null,
  }])
  await act(async () => { root.render(<Harness/>); await settle() })
  await click(textButton('货梯困人涉及安全'))
  await click(button('已到场确认'))

  const post = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-w1/confirm')
  expect(post).toBeTruthy()
  const body = bodyOf(post)
  expect(String(body.reason)).toContain('已到场确认')
  expect(body.expected_revision).toBe(1)
  expect('edits' in body).toBe(false)
})

it('真实批准卡把可选原因带进确认 reason', async () => {
  const fetcher = mockActions([{
    action_id: 'act-p1', kind: 'plan_confirmation', status: 'proposed', revision: 0,
    payload: { target_ref: 'plan://p-1@2' }, target_ref: 'plan://p-1@2', proposed_at: '2026-07-01T00:07:00Z', reason: null,
  }])
  await act(async () => { root.render(<Harness/>); await settle() })
  await click(textButton('批准计划'))
  await setValue(byLabel('批准原因'), '延长到 09:30')
  await click(button('批准'))

  const post = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-p1/confirm')
  expect(post).toBeTruthy()
  expect(bodyOf(post)).toMatchObject({ reason: '延长到 09:30', expected_revision: 0 })
})

it('真实纠错卡把一句话纠正放进确认 reason，提交后只显示本次事件单的进化记录', async () => {
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/v1/actions?')) {
      return Promise.resolve(page([{
        action_id: 'act-c1', kind: 'correction', status: 'proposed', revision: 0,
        payload: { ticket_id: 'ticket://tk-0005', kind: 'correction', item_id: 'correction-tk-0005-1-manual-0' },
        target_ref: 'correction-tk-0005-1-manual-0', proposed_at: '2026-07-01T00:07:00Z', reason: null,
      }]))
    }
    if (url.startsWith('/api/v1/evolution/timeline')) {
      // 后端确认纠错卡写 feedback_recorded（真实字段 ticket_id / candidate_id / outcome / system_error）；提交前时间线里只有旧记录
      const old = [{ seq: 1, event: 'feedback_recorded', occurred_at: '2026-07-01T08:00:00Z', details: { ticket_id: 'ticket://tk-0005', candidate_id: 'b32-cand-old', outcome: 'corrected', system_error: true } }]
      const confirmed = fetcher.mock.calls.some(call => String(call[0]) === '/api/v1/actions/act-c1/confirm')
      return Promise.resolve(page(confirmed
        ? [...old, { seq: 2, event: 'feedback_recorded', occurred_at: '2026-07-01T08:10:00Z', details: { ticket_id: 'ticket://tk-0005', candidate_id: 'b32-cand-9', outcome: 'corrected', system_error: true } }]
        : old))
    }
    return Promise.resolve(page([]))
  })
  vi.stubGlobal('fetch', fetcher)
  await act(async () => { root.render(<Harness/>); await settle() })
  await click(textButton('纠正 tk-0005'))
  await setValue(byLabel('一句话纠正'), '先查配电回路')
  await click(button('提交纠错'))

  const post = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-c1/confirm')
  expect(post).toBeTruthy()
  expect(String(bodyOf(post).reason)).toContain('先查配电回路')
  expect(container.textContent).toContain('b32-cand-9')
  expect(container.textContent).not.toContain('b32-cand-old')
})

it('默认演示工单的确认走演示处理：不请求确认端点，仍记到场确认并标数据性质', async () => {
  const fetcher = vi.fn((_input: RequestInfo | URL) => Promise.reject(new TypeError('offline')))
  vi.stubGlobal('fetch', fetcher)
  await act(async () => { root.render(<Harness/>); await settle() })
  await click(textButton('巡检 T1 变压器温升'))
  expect(container.textContent).toContain('记录里的待确认动作编号 act-demo-t1')
  await click(button('已到场确认'))

  expect(fetcher.mock.calls.some(call => String(call[0]).includes('/confirm'))).toBe(false)
  expect(useDay.getState().verdicts['work_order:wo-demo-t1']).toBe('approved')
  expect(container.textContent).toContain('已到场确认')
  expect(container.textContent).toContain('离线演示')
})

it('真实 human_decision 待办映射成一键批准卡，批准与驳回都走确认端点', async () => {
  const fetcher = mockActions([
    {
      action_id: 'act-hd1', kind: 'human_decision', status: 'proposed', revision: 0,
      payload: { work_order_id: 'HD-1', ticket_id: 'ticket://tk-0003', reason: '人工确认：3 号空压机停机检修', actions: [] },
      target_ref: 'HD-1', proposed_at: '2026-07-01T00:07:00Z', reason: null,
    },
    {
      action_id: 'act-hd2', kind: 'human_decision', status: 'proposed', revision: 2,
      payload: { work_order_id: 'HD-2', ticket_id: 'ticket://tk-0003', reason: '人工确认：夜间巡检是否增派' },
      target_ref: 'HD-2', proposed_at: '2026-07-01T00:08:00Z', reason: null,
    },
  ])
  await act(async () => { root.render(<Harness/>); await settle() })
  expect(container.textContent).toContain('真实接口 · human_decision')
  expect(container.textContent).toContain('人工确认：3 号空压机停机检修')

  await click(textButton('人工确认：3 号空压机停机检修'))
  await click(button('批准'))
  const confirmed = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-hd1/confirm')
  expect(confirmed).toBeTruthy()
  expect(bodyOf(confirmed)).toMatchObject({ expected_revision: 0 })
  expect(useDay.getState().verdicts['action:act-hd1']).toBe('approved')

  await click(textButton('人工确认：夜间巡检是否增派'))
  await click(button('驳回'))
  const rejected = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-hd2/reject')
  expect(rejected).toBeTruthy()
  expect(bodyOf(rejected)).toMatchObject({ expected_revision: 2 })
  expect(useDay.getState().verdicts['action:act-hd2']).toBe('rejected')
})

it('真实发布动作走确认 / 驳回接口，不再只在前端记裁定', async () => {
  const publish = {
    action_id: 'act-pub1', kind: 'publish', status: 'proposed', revision: 3,
    payload: { target_ref: 'vm-0004' }, target_ref: 'vm-0004', proposed_at: '2026-07-01T08:00:00Z', reason: null,
  }
  const fetcher = mockActions([publish])
  await act(async () => { root.render(<Harness/>); await settle() })
  const item = [...container.querySelectorAll('.desk-item')].find(node => node.textContent?.includes('真实接口 · publish'))
  await click(item as HTMLElement)
  await click(button('发布'))

  const confirmed = fetcher.mock.calls.find(call => String(call[0]) === '/api/v1/actions/act-pub1/confirm')
  expect(confirmed).toBeTruthy()
  expect(bodyOf(confirmed)).toMatchObject({ expected_revision: 3 })
  expect(useDay.getState().verdicts['action:act-pub1']).toBe('published')
})
