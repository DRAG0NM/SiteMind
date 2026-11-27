import { expect, it } from 'vitest'
import sampleJson from '../../../../contracts/examples/day.sample.json'
import { parseDay } from '../../showcase/data/loadDay'

type Raw = Record<string, any>
const sample = () => structuredClone(sampleJson) as unknown as Raw

it('合法的 day.sample.json 通过校验', () => {
  expect(() => parseDay(sample())).not.toThrow()
})

it('runs 为空数组时拒绝，对齐 schema 的 minItems:1', () => {
  const day = sample()
  day.runs = []
  expect(() => parseDay(day)).toThrow(/runs/)
})

it('local_date 不是 YYYY-MM-DD 时拒绝', () => {
  const day = sample()
  day.local_date = '2026/07/01'
  expect(() => parseDay(day)).toThrow(/local_date/)
})

it('timezone 无法被 Intl 构造时拒绝', () => {
  const day = sample()
  day.timezone = 'Bad/Zone'
  expect(() => parseDay(day)).toThrow(/timezone/)
})

it('事件时间不可解析时错误里给出字段路径', () => {
  const day = sample()
  day.events[0].at = 'bad'
  expect(() => parseDay(day)).toThrow(/events\[0\]\.at/)
})

it('事件时间缺少时区偏移时拒绝', () => {
  const day = sample()
  day.events[0].at = '2026-07-01T08:00:00'
  expect(() => parseDay(day)).toThrow(/events\[0\]\.at/)
})

it('小节条目不是对象时给出条目路径', () => {
  const day = sample()
  day.tickets[0] = null
  expect(() => parseDay(day)).toThrow(/tickets\[0\]/)
})

it('账本分钟区间端点缺失时给出嵌套路径', () => {
  const day = sample()
  day.ledger_minutes[0].interval.end = undefined
  expect(() => parseDay(day)).toThrow(/ledger_minutes\[0\]\.interval\.end/)
})

it('context 里的跨日记录时间坏了时拒绝并给出路径，合法时通过', () => {
  const day = sample()
  day.context = { events: [{ ...day.events[0], at: '2026-06-30 23:55' }] }
  expect(() => parseDay(day)).toThrow(/context\.events\[0\]\.at/)
  day.context = { events: [day.events[0]], proposals: [day.proposals[0]] }
  expect(() => parseDay(day)).not.toThrow()
})
