// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { QueueItemView } from '../../console/types'
import { ApproveCard, CorrectionCard, QuestionCard, SamplingCard, WorkOrderCard } from './ReviewCards'

let container: HTMLDivElement
let root: Root

const json = (body: unknown) => new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
})
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

function item(partial: Partial<QueueItemView>): QueueItemView {
  return {
    id: 'item-1', kind: 'work_order', title: '演示件', subtitle: '演示', detail: '演示', facts: [], notes: [],
    source: '演示', at: 0, entityId: null, actionId: null, verdict: null, ...partial,
  }
}

function handlers() {
  return { busy: false, confirm: vi.fn().mockResolvedValue(undefined), reject: vi.fn().mockResolvedValue(undefined) }
}

const render = (node: ReactNode) => act(async () => { root.render(node); await settle() })

async function click(target: HTMLElement | null) {
  expect(target).toBeTruthy()
  await act(async () => { target!.dispatchEvent(new MouseEvent('click', { bubbles: true })); await settle() })
}

async function setValue(target: HTMLInputElement | HTMLTextAreaElement | null, value: string) {
  expect(target).toBeTruthy()
  const prototype = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')!.set!
  await act(async () => {
    setter.call(target, value)
    target!.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

const button = (label: string) => [...container.querySelectorAll('button')].find(node => node.textContent?.trim().includes(label)) ?? null
const byLabel = (label: string) => container.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[aria-label="${label}"]`)

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
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

it('操作工单卡区分伤亡与重大损失，显示预案保护动作，已到场确认识别原工单', async () => {
  const callbacks = handlers()
  await render(<WorkOrderCard item={item({
    kind: 'work_order', title: '货梯困人涉及安全', workOrderSource: 'casualty',
    protections: ['其他货梯接管生产任务', '暖通给井道通风'],
  })} {...callbacks} />)
  expect(container.textContent).toContain('伤亡（人员安全）')
  expect(container.textContent).toContain('其他货梯接管生产任务')
  expect(container.textContent).not.toContain('合成示例')

  await click(button('已到场确认'))
  expect(callbacks.confirm).toHaveBeenCalledWith(expect.objectContaining({ title: '货梯困人涉及安全' }), expect.stringContaining('已到场确认'))

  await click(button('退回'))
  expect(callbacks.reject).toHaveBeenCalled()
})

it('操作工单卡把确认后执行的处置动作与已执行保护动作分开展示，没有保护动作就不补写', async () => {
  const withProtections = handlers()
  await render(<WorkOrderCard item={item({
    kind: 'work_order', title: '重大损失工单', workOrderSource: 'major_loss', synthetic: true,
    disposalActions: ['空压机 AIR-1 · open_work_order（停机检修，确认后才下发）'],
    protections: ['储能放电 320 kW 支撑母线电压'],
    protectionsSynthetic: true,
  })} {...withProtections} />)
  expect(container.textContent).toContain('确认后执行的处置动作')
  expect(container.textContent).toContain('空压机 AIR-1 · open_work_order')
  expect(container.textContent).toContain('已自动执行的预案保护动作')
  expect(container.textContent).toContain('储能放电 320 kW')
  expect(container.textContent).toContain('合成')

  // 真实 payload 没有保护动作字段：如实写没有，不用合成示例顶替
  await render(<WorkOrderCard item={item({ kind: 'work_order', title: '货梯困人', workOrderSource: 'casualty' })} {...handlers()} />)
  expect(container.textContent).toContain('后端没有返回已自动执行的预案保护动作')
  expect(container.textContent).not.toContain('合成示例')
})

it('纠错卡把一句话、步骤与附资料写进确认原因，提交后只显示本次新增的进化记录', async () => {
  const callbacks = handlers()
  // 确认纠错卡后后端写 feedback_recorded（ticket_id / candidate_id 常为 null / outcome）：提交前已有的（含同事件单旧记录）不算本次
  const old = [
    { seq: 1, event: 'feedback_recorded', occurred_at: '2026-07-01T07:00:00Z', details: { ticket_id: 'ticket://tk-0001', candidate_id: 'b32-cand-other', outcome: 'corrected', system_error: true } },
    { seq: 2, event: 'feedback_recorded', occurred_at: '2026-07-01T07:30:00Z', details: { ticket_id: 'ticket://tk-0005', candidate_id: 'b32-cand-old', outcome: 'corrected', system_error: true } },
  ]
  const fresh = { seq: 3, event: 'feedback_recorded', occurred_at: '2026-07-01T08:00:00Z', details: { ticket_id: 'ticket://tk-0005', candidate_id: null, outcome: 'corrected', system_error: true } }
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce(json({ items: old }))
    .mockResolvedValue(json({ items: [...old, fresh] })))
  await render(<CorrectionCard item={item({
    kind: 'correction', title: '照明与空压同时异常', actionId: 'act-c1', payload: { ticket_id: 'ticket://tk-0005' },
  })} {...callbacks} />)

  await setValue(byLabel('一句话纠正'), '先查配电回路')
  await setValue(byLabel('排查步骤'), '查配电回路\n查照明回路')
  await setValue(byLabel('资料文件名'), '现场记录.txt')
  await setValue(byLabel('资料内容'), '配电柜 B 相跳闸')
  await click(button('附资料'))
  await click(button('提交纠错'))

  expect(callbacks.confirm).toHaveBeenCalledTimes(1)
  const reason = callbacks.confirm.mock.calls[0][1] as string
  expect(reason).toContain('先查配电回路')
  expect(reason).toContain('排查步骤：查配电回路 → 查照明回路')
  expect(reason).toContain('附资料《现场记录.txt》：配电柜 B 相跳闸')
  expect(container.textContent).toContain('feedback_recorded')
  expect(container.textContent).toContain('outcome corrected')
  expect(container.textContent).not.toContain('b32-cand-other')
  expect(container.textContent).not.toContain('b32-cand-old')
})

it('纠错卡在时间线没有新增记录时如实写等待进化记录，不拿同事件单旧记录冒充本次结果', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json({
    items: [{ seq: 1, event: 'candidate_proposed', occurred_at: '2026-07-01T07:00:00Z', details: { ticket_id: 'ticket://tk-0005', candidate_id: 'b32-cand-other', outcome: 'corrected' } }],
  }))))
  await render(<CorrectionCard item={item({
    kind: 'correction', title: '照明与空压同时异常', actionId: 'act-c1', payload: { ticket_id: 'ticket://tk-0005' },
  })} {...handlers()} />)
  await setValue(byLabel('一句话纠正'), '先查配电回路')
  await click(button('提交纠错'))
  expect(container.textContent).toContain('等待进化记录')
  expect(container.textContent).not.toContain('b32-cand-other')
})

it('纠错卡提交前读不到进化时间线时写“读取进化记录失败”，不做匹配', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('offline'))))
  await render(<CorrectionCard item={item({
    kind: 'correction', title: '照明与空压同时异常', actionId: 'act-c1', payload: { ticket_id: 'ticket://tk-0005' },
  })} {...handlers()} />)
  await setValue(byLabel('一句话纠正'), '先查配电回路')
  await click(button('提交纠错'))
  expect(container.textContent).toContain('读取进化记录失败')
})

it('纠错卡在演示模式（没有动作编号）接口失败时只给演示沉淀，不编造候选', async () => {
  await render(<CorrectionCard item={item({ kind: 'correction', title: '低置信事件' })} {...handlers()} />)
  await setValue(byLabel('一句话纠正'), '先查配电回路')
  await click(button('提交纠错'))
  expect(container.textContent).toContain('离线演示')
  expect(container.textContent).toContain('纠错沉淀')
})

it('抽查卡判错时复核项把“不是系统判断有误”记在前端，sampling 不允许勾选', async () => {
  const review = handlers()
  await render(<SamplingCard item={item({ kind: 'review', backendKind: 'review', actionId: 'act-r1' })} {...review} />)
  expect(container.textContent).toContain('仅对复核项生效')
  const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]')
  expect(checkbox?.disabled).toBe(false)
  await click(checkbox)
  await click(button('判错'))
  // 拒绝请求只发 reason：勾选值走前端处置记录，不生成 edits
  expect(review.reject).toHaveBeenCalledWith(expect.objectContaining({ actionId: 'act-r1' }), expect.any(String), { systemError: true })
  expect(review.reject.mock.calls[0]).toHaveLength(3)

  const sampling = handlers()
  await render(<SamplingCard item={item({ kind: 'review', backendKind: 'sampling', actionId: 'act-s1' })} {...sampling} />)
  const disabled = container.querySelector<HTMLInputElement>('input[type="checkbox"]')
  expect(disabled?.disabled).toBe(true)
  await click(button('判错'))
  expect(sampling.reject).toHaveBeenCalledWith(expect.objectContaining({ actionId: 'act-s1' }), expect.any(String), undefined)
})

it('回答卡把答案原文交给确认', async () => {
  const callbacks = handlers()
  await render(<QuestionCard item={item({ kind: 'question', title: '轿厢里还有人吗？' })} {...callbacks} />)
  expect(container.textContent).toContain('轿厢里还有人吗？')
  await setValue(byLabel('回答'), '轿厢里有人')
  await click(button('提交回答'))
  expect(callbacks.confirm).toHaveBeenCalledWith(expect.any(Object), '轿厢里有人')
})

it('批准卡填了原因就带进 reason，没填给默认批准说明', async () => {
  const callbacks = handlers()
  await render(<ApproveCard item={item({ kind: 'approve', title: '分区运行延长' })} {...callbacks} />)
  await click(button('批准'))
  expect(callbacks.confirm).toHaveBeenCalledWith(expect.any(Object), '值班批准：分区运行延长')

  callbacks.confirm.mockClear()
  await setValue(byLabel('批准原因'), '延长到 09:30')
  await click(button('批准'))
  expect(callbacks.confirm).toHaveBeenCalledWith(expect.any(Object), '延长到 09:30')
})
