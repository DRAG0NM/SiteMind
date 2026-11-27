import { expect, it } from 'vitest'
import sampleJson from '../../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from './data/demoDay'
import { dayAtHour, time } from './data/time'
import type { Day } from './data/types'
import { TOUR_FRAMES, tourFrameAt } from './tour'

/** 巡演第 3/5/7 段停在数据生效那一刻，进入页面就能看到对应的知识、规则与裁决。 */
it('巡演的新资料、暖通规则与纠错裁决三段都落在数据生效时刻', () => {
  const day = withShowcaseDemo(structuredClone(sampleJson) as unknown as Day)
  const at = (hour: number, minute: number) => dayAtHour(day, hour) + minute * 60_000

  const materialAt = tourFrameAt(day, TOUR_FRAMES[2])
  expect(materialAt).toBe(at(9, 35))
  const knowledge = day.knowledge_versions.find(version => version.version === 2)
  expect(knowledge).toBeTruthy()
  expect(time(knowledge!.activated_at)).toBeLessThanOrEqual(materialAt)

  const hvacAt = tourFrameAt(day, TOUR_FRAMES[4])
  expect(hvacAt).toBe(at(13, 0))
  const rule = day.temp_rules.find(item => item.rule_id === 'temp://tr-demo-1330')
  expect(rule).toBeTruthy()
  expect(time(rule!.effective.start)).toBeLessThanOrEqual(hvacAt)

  const correctionAt = tourFrameAt(day, TOUR_FRAMES[6])
  expect(correctionAt).toBe(at(16, 25))
  const decision = day.decisions.find(item => item.decision_id === 'decision://dc-0005')
  expect(decision).toBeTruthy()
  expect(time(decision!.at)).toBeLessThanOrEqual(correctionAt)
})
