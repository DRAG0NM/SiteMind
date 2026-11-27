import { describe, expect, it } from 'vitest'
import { dayAtHour, dayEnd, dayStart, devicesAt, sample, time, useDay } from './store'

describe('样例回放', () => {
  it('重建早高峰的设备变化并在时间推进时显示后续事件', () => {
    const before = time('2026-07-01T00:04:00Z')
    const peak = time('2026-07-01T00:05:00Z')
    const battery = 'entity://demo-electronics/battery/BESS1'
    const lift = 'entity://demo-electronics/elevator_group/EG-PROD'
    expect(devicesAt(sample, before)[lift].queue).toBe(8)
    expect(devicesAt(sample, peak)[lift].queue).toBe(42)
    expect(devicesAt(sample, time('2026-07-01T00:06:00Z'))[battery].mode).toBe('discharge')
    useDay.getState().load(sample, '样例')
    useDay.getState().seek(peak)
    expect(useDay.getState().day.events.filter(e => time(e.at) <= useDay.getState().cursor)).toHaveLength(2)
    useDay.getState().seek(time('2026-07-01T02:31:00Z'))
    const appeared = useDay.getState().day.events.filter(e => e.nature === 'synthetic' && time(e.at) <= useDay.getState().cursor)
    expect(appeared.length).toBeGreaterThanOrEqual(2)
    expect(appeared.map(e => e.event_id)).toContain('event://demo-air-leak')
    expect(useDay.getState().day.decisions.some(d => d.decision_id === 'decision://dc-demo-0806')).toBe(true)
    useDay.getState().seek(time('2026-07-01T05:31:00Z'))
    expect(useDay.getState().day.work_orders.filter(w => time(w.created_at) <= useDay.getState().cursor)).toHaveLength(3)
  })

  it('使用导出时区计算日界与初始时刻，包含夏令时切换日', () => {
    expect(dayStart(sample)).toBe(time('2026-06-30T16:00:00Z'))
    const day = { ...sample, timezone: 'America/New_York', local_date: '2026-03-08' }
    expect(dayStart(day)).toBe(time('2026-03-08T05:00:00Z'))
    expect(dayEnd(day)).toBe(time('2026-03-09T04:00:00Z'))
    expect(dayAtHour(day, 8)).toBe(time('2026-03-08T12:00:00Z'))
    useDay.getState().load(day, '时区回归')
    expect(useDay.getState().cursor).toBe(dayAtHour(day, 8) + 7.5 * 60_000)
  })

  it('午夜夏令时跳变时，日界从跳变后的第一个有效时刻开始', () => {
    const santiago = { ...sample, timezone: 'America/Santiago', local_date: '2026-09-06' }
    expect(dayStart(santiago)).toBe(time('2026-09-06T04:00:00Z'))
    expect(dayEnd(santiago)).toBe(time('2026-09-07T03:00:00Z'))
    expect(dayEnd(santiago) - dayStart(santiago)).toBe(23 * 3600 * 1000)
    const havana = { ...sample, timezone: 'America/Havana', local_date: '2026-03-08' }
    expect(dayStart(havana)).toBe(time('2026-03-08T05:00:00Z'))
    expect(dayEnd(havana)).toBe(time('2026-03-09T04:00:00Z'))
    expect(dayEnd(havana) - dayStart(havana)).toBe(23 * 3600 * 1000)
  })
})
