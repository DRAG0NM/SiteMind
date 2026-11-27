// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { sample, useDay } from '../../store'
import { buildConsoleModel } from '../data'
import { AgentRunsScreen } from './AgentRuns'

const cursor = Date.parse('2026-07-01T00:05:30Z')
const state = {
  playing: false, paused: false, pausedAt: null, mode: 'replay' as const,
  verdicts: {}, retracted: [], cancelledRules: [],
}
const model = () => buildConsoleModel(structuredClone(sample), cursor, '回归样例', state)
let container: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  useDay.getState().load(structuredClone(sample), '回归样例')
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

const click = (element: Element) => act(async () => {
  element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
})

it('泳道名与块可见，点块右栏显示工具与耗时，点事件流能定位', async () => {
  await act(async () => root.render(<AgentRunsScreen model={model()} />))
  expect(container.textContent).toContain('能源专家')
  expect(container.textContent).toContain('电梯专家')

  // 内部工具名不再上屏：电梯专家的块写中文名，耗时写在块里。
  const lane = [...container.querySelectorAll('.ar-lane')]
    .find(element => element.querySelector('.ar-lane-name')?.textContent?.includes('电梯专家'))!
  const elevator = lane.querySelector<HTMLButtonElement>('.ar-step')!
  expect(elevator).toBeTruthy()
  const elevatorLabel = elevator.querySelector('.ar-step-tool')?.textContent ?? ''
  expect(elevatorLabel.length).toBeGreaterThan(0)
  expect(elevatorLabel).not.toContain('elevator_expert')
  await click(elevator)
  const detail = container.querySelector('.ar-detail')!
  expect(detail.textContent).toContain(elevatorLabel)
  expect(detail.textContent).toContain('9.1 s')
  expect(elevator.classList.contains('sel')).toBe(true)

  // 右栏“选中步骤”写中文动作名（内部工具名只留在“工具”一项），块用像素宽度放全文。
  const mainLane = [...container.querySelectorAll('.ar-lane')]
    .find(element => element.querySelector('.ar-lane-name')?.textContent?.includes('主 Agent'))!
  const conflict = [...mainLane.querySelectorAll<HTMLButtonElement>('.ar-step')]
    .find(element => element.querySelector('.ar-step-tool')?.textContent === '冲突裁决')!
  expect(conflict).toBeTruthy()
  expect(conflict.getAttribute('title')).toContain('冲突裁决')
  expect(conflict.getAttribute('title')).toContain('3.0 s')
  expect(conflict.style.width).toMatch(/px$/)
  await click(conflict)
  expect(container.querySelector('.ar-tool')?.textContent).toBe('冲突裁决')
  expect(container.querySelector('.ar-tool')?.textContent).not.toContain('resolve_conflict')
  expect(detail.textContent).toContain('resolve_conflict')

  const row = [...container.querySelectorAll<HTMLButtonElement>('.ar-line')]
    .find(element => element.textContent?.includes('工具结果'))!
  expect(row).toBeTruthy()
  await click(row)
  const selectedBlock = container.querySelector('.ar-step.sel')
  const tool = container.querySelector('.ar-tool')?.textContent ?? ''
  expect(selectedBlock).toBeTruthy()
  expect(tool.length).toBeGreaterThan(0)
  expect(selectedBlock?.textContent).toContain(tool)
  expect(container.querySelector('.ar-line.sel')).toBe(row)
})

it('运行席头部显示场景触发时刻与耗时', async () => {
  await act(async () => root.render(<AgentRunsScreen model={model()} />))
  expect(container.querySelector('.ar-trigger')?.textContent).toBe('08:05')
  expect(container.querySelector('.ar-elapsed')?.textContent).toBe('23.6 秒')
})

it('真实导出日没有 agent_runs 时才回退 day.run_id，有运行记录但还没到主 Agent 时显示 —', async () => {
  const empty = structuredClone(sample)
  empty.agent_runs = []
  const emptyModel = buildConsoleModel(empty, cursor, '回归样例', state)
  expect(emptyModel.run).toBeNull()
  await act(async () => root.render(<AgentRunsScreen model={emptyModel} />))
  expect(container.querySelector('.ar-runid')?.textContent).toBe(empty.run_id)

  const expertsOnly = structuredClone(sample)
  // 合约里主 Agent 的 kind 是 triage，不是 main；按 agent_name 去掉主 Agent 运行。
  expertsOnly.agent_runs = expertsOnly.agent_runs.filter(run => !/main/i.test(run.agent_name))
  const pendingModel = buildConsoleModel(expertsOnly, cursor, '回归样例', state)
  expect(pendingModel.run).toBeNull()
  expect(expertsOnly.agent_runs.length).toBeGreaterThan(0)
  await act(async () => root.render(<AgentRunsScreen model={pendingModel} />))
  expect(container.querySelector('.ar-runid')?.textContent).toBe('—')
})

it('同一轮连续 Enter 与运行中的 Enter 只发一次请求，结束后允许再次提问', async () => {
  let stream!: ReadableStreamDefaultController<Uint8Array>
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(new ReadableStream({
    start(controller) { stream = controller },
  }))))
  vi.stubGlobal('fetch', fetchMock)
  await act(async () => root.render(<AgentRunsScreen model={model()} />))
  const input = container.querySelector<HTMLInputElement>('input[placeholder="向主 Agent 提问"]')!
  const send = container.querySelector<HTMLButtonElement>('button[aria-label="发送"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '检查厂区')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  const enter = () => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
  await act(async () => { enter(); enter() })
  await act(async () => { enter() })
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(JSON.parse(fetchMock.mock.calls[0][1].body).as_of).toBe(new Date(cursor).toISOString())
  expect(send.disabled).toBe(true)

  const finish = (id: string) => {
    stream.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({
      type: 'run_finished', agent_run_id: id, seq: 1, ts: '2026-07-01T00:00:00Z',
      payload: { terminal_reason: 'completed' },
    })}\n\n`))
    stream.close()
  }
  await act(async () => finish('ar-1'))
  expect(send.disabled).toBe(false)
  await act(async () => { enter() })
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await act(async () => finish('ar-2'))
})
