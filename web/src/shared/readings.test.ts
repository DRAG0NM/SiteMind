import { describe, expect, it } from 'vitest'
import {
  airPressure, campusReadings, elevatorQueue, pctDiff, pv, t1, t1Note, type ReadingsDay,
} from './readings'

/** 造一个只含读数层所需字段的最小 day：一帧设备快照 + 若干账本分钟 + config 指标。 */
function makeDay(loadRatio: number, limit: number | null = 85): ReadingsDay {
  return {
    state_series: [{
      at: '2026-07-01T00:00:00Z',
      devices: {
        'entity://demo-electronics/transformer/T1': { load_ratio: loadRatio },
        'entity://demo-electronics/compressor/AIR-1': { pressure_mpa: 0.58, queue: undefined },
        'entity://demo-electronics/elevator_group/admin_low': { queue: 12 },
        'entity://demo-electronics/elevator_group/admin_high': { queue: 8 },
        'entity://demo-electronics/elevator_group/EG-PROD': { queue: 42 },
        'entity://demo-electronics/pv/PV-1': { output_kw: 90 },
      },
    }],
    ledger_minutes: [
      { interval: { start: '2026-07-01T00:00:00Z', end: '2026-07-01T00:01:00Z' }, pv_used_kwh: 0.1 },
      { interval: { start: '2026-07-01T00:01:00Z', end: '2026-07-01T00:02:00Z' }, pv_used_kwh: 0.2 },
      { interval: { start: '2026-07-01T00:02:00Z', end: '2026-07-01T00:03:00Z' }, pv_used_kwh: 0.5 },
    ],
    metrics: [
      ...(limit === null ? [] : [{ metric: 'transformer_limit_pct', group: 'config', value: limit }]),
    ],
  }
}

const at = (minute: number) => Date.parse('2026-07-01T00:00:00Z') + minute * 60_000

describe('统一读数层', () => {
  it('T1 正好到上限算已到上限，不算超限，余量为 0', () => {
    const reading = t1(makeDay(0.85), at(0))
    expect(reading).toEqual({ loadPct: 85, limitPct: 85, state: 'at_limit', marginPct: 0 })
    expect(t1Note(reading)).toBe('已到上限')
  })

  it('T1 超限按严格大于上限判断，余量按上限算', () => {
    expect(t1(makeDay(0.86), at(0))).toEqual({ loadPct: 86, limitPct: 85, state: 'over', marginPct: -1 })
    expect(t1Note(t1(makeDay(0.86), at(0)))).toBe('超上限 1 个百分点')
    expect(t1(makeDay(0.84), at(0))).toEqual({ loadPct: 84, limitPct: 85, state: 'ok', marginPct: 1 })
    expect(t1Note(t1(makeDay(0.84), at(0)))).toBe('距上限 1 个百分点')
  })

  it('没有限值配置时状态为空，文案提示缺少限值', () => {
    const reading = t1(makeDay(0.9, null), at(0))
    expect(reading.state).toBeNull()
    expect(reading.loadPct).toBe(90)
    expect(t1Note(reading)).toBe('缺少限值配置')
  })

  it('空压站压力兼容 pressure_bar 与 pressure_mpa，统一输出 MPa', () => {
    expect(airPressure({ pressure_mpa: 0.58 })).toBeCloseTo(0.58)
    expect(airPressure({ pressure_bar: 5.8 })).toBeCloseTo(0.58)
    expect(airPressure({ pressure_mpa: 0.58, pressure_bar: 99 })).toBeCloseTo(0.58)
    expect(airPressure({})).toBeNull()
    expect(airPressure(undefined)).toBeNull()
  })

  it('行政楼候梯人数汇总多台电梯，取不到写 —', () => {
    const day = makeDay(0.72)
    expect(elevatorQueue(day, at(0), 'admin')).toBe(20)
    expect(elevatorQueue(day, at(0), 'factory')).toBe(42)
    delete day.state_series[0].devices['entity://demo-electronics/elevator_group/admin_low'].queue
    delete day.state_series[0].devices['entity://demo-electronics/elevator_group/admin_high'].queue
    expect(elevatorQueue(day, at(0), 'admin')).toBeNull()
  })

  it('光伏把此刻出力与今日发电分开，未来分钟不计入', () => {
    const reading = pv(makeDay(0.72), at(2))
    expect(reading.kwNow).toBe(12)
    expect(reading.kwhToday).toBe(0.3)
    expect(pv(makeDay(0.72), at(99)).kwhToday).toBe(0.8)
  })

  it('跨文档问答差值先按单位换算再相减', () => {
    expect(pctDiff({ value: 86, unit: '%' }, { value: 71, unit: '%' })).toBe(15)
    expect(pctDiff({ value: 0.86, unit: 'ratio' }, { value: 0.71, unit: 'ratio' })).toBe(15)
    expect(pctDiff({ value: null, unit: '%' }, { value: 71, unit: '%' })).toBeNull()
  })

  it('园区读数一次算好：T1、光伏与候梯都来自同一层', () => {
    const reading = campusReadings(makeDay(0.85), at(2))
    expect(reading.t1.state).toBe('at_limit')
    expect(reading.pv).toEqual({ kwNow: 12, kwhToday: 0.3 })
    expect(reading.elevatorQueue).toBe(20)
  })
})
