// @vitest-environment jsdom
import { expect, it } from 'vitest'
import { prefersReducedMotion } from './motion'

it('?motion=reduce 设 data-motion="reduce" 后真正减弱，不受系统设置影响', () => {
  document.documentElement.dataset.motion = 'reduce'
  expect(prefersReducedMotion()).toBe(true)
  document.documentElement.dataset.motion = 'full'
  expect(prefersReducedMotion()).toBe(false)
  delete document.documentElement.dataset.motion
})
