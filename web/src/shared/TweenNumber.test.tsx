// @vitest-environment jsdom
// #218-7 / #239：TweenNumber 的冒烟用例——数字滚动、减弱动效直接落值、非数字原样显示。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { TweenNumber } from './TweenNumber'

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  document.documentElement.dataset.motion = 'reduce'
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  delete document.documentElement.dataset.motion
  container.remove()
})

it('数字换值时保留小数位滚动，减弱动效时直接落值，非数字原样显示', async () => {
  await act(async () => root.render(<TweenNumber value="96" />))
  expect(container.textContent).toBe('96')

  await act(async () => root.render(<TweenNumber value="81" />))
  expect(container.textContent).toBe('81')

  await act(async () => root.render(<TweenNumber value="0.72" />))
  expect(container.textContent).toBe('0.72')

  await act(async () => root.render(<TweenNumber value="—" />))
  expect(container.textContent).toBe('—')
})
