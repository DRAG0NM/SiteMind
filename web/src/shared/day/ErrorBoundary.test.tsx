// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

let container: HTMLDivElement
let root: Root

function Broken({ fail }: { fail: boolean }) {
  if (fail) throw new Error('坏时间')
  return <p>回放正常</p>
}

const render = (fail: boolean, resetKey: string) => root.render(
  <ErrorBoundary title="回放渲染出错" resetKey={resetKey}><Broken fail={fail} /></ErrorBoundary>,
)

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  // React 会打印被 error boundary 接住的异常，测试里不需要这些红字。
  vi.spyOn(console, 'error').mockImplementation(() => { /* 忽略被捕获的渲染异常 */ })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.restoreAllMocks()
})

it('切换标签页或换一天（resetKey 变化）后错误卡片自动复位', async () => {
  await act(async () => render(true, 'overview:run-1'))
  expect(container.textContent).toContain('回放渲染出错：坏时间')

  // resetKey 不变时错误卡片保留，不会偷偷重渲染坏数据。
  await act(async () => render(false, 'overview:run-1'))
  expect(container.textContent).toContain('回放渲染出错：坏时间')

  await act(async () => render(false, 'energy:run-1'))
  expect(container.textContent).toBe('回放正常')

  await act(async () => render(true, 'overview:run-2'))
  expect(container.textContent).toContain('回放渲染出错：坏时间')
})
