// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from '../data/demoDay'
import { dayAtHour } from '../data/time'
import type { Day } from '../data/types'
import { TOUR_FRAMES, tourFrameAt } from '../tour'
import { StageView } from './Stage'

// 这条只测副标题与数字的出场时机，厂区 SVG 用空组件替身，别让它的动画拖慢用例。
vi.mock('../../shared/iso/IsoCampus', () => ({ IsoCampus: () => null }))

let container: HTMLDivElement
let root: Root

const wait = (ms: number) => act(async () => { await new Promise(resolve => setTimeout(resolve, ms)) })

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  document.documentElement.dataset.motion = 'full'
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  delete document.documentElement.dataset.motion
  container.remove()
})

it('裁决刚做出、T1 读数还没降下来时，舞台③只写“已裁决，效果待观察”，不给未来数字', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const frame = TOUR_FRAMES[1]
  await act(async () => {
    root.render(<StageView day={day} cursor={tourFrameAt(day, frame)} frame={frame} campusRef={{ current: null }} onSelect={() => {}} />)
  })
  expect(container.querySelector('.effect-big')?.textContent).toBe('96%')
  expect(container.querySelector('.effect-sub')?.textContent).toContain('已裁决，效果待观察')
  expect(container.textContent).not.toContain('81%')
})

it('舞台③副标题等大数字落定才出现，前 1.8 秒不写“降 15 个百分点”', async () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const frame = TOUR_FRAMES[1]
  await act(async () => {
    // T1 实际降到 81% 之后（08:15 帧起）才有“实际”读数
    root.render(<StageView day={day} cursor={dayAtHour(day, 8) + 16 * 60_000} frame={frame} campusRef={{ current: null }} onSelect={() => {}} />)
  })

  expect(container.querySelector('.effect-big')?.textContent).toContain('96%')
  expect(container.querySelector('.effect-sub')?.getAttribute('style') ?? '').toContain('hidden')

  // 数字开始滚落后副标题仍不抢跑
  await wait(1900)
  expect(container.querySelector('.effect-sub')?.getAttribute('style') ?? '').toContain('hidden')

  await wait(1200)
  expect(container.querySelector('.effect-sub')?.textContent).toContain('降 15 个百分点')
  expect(container.querySelector('.effect-sub')?.getAttribute('style') ?? '').toContain('visible')
}, 15000)
