import type { Day } from './types'

export const time = (iso: string) => new Date(iso).getTime()

/** 提案 / 运行记录上用于回放裁剪的时间字段；墙钟字段 requested_at / returned_at 不在其中。 */
export interface SimulationTimed {
  binding?: { trigger_at?: string | null } | null
  timing?: {
    origin_event_at?: string | null
    trigger_at?: string | null
    effective_at?: string | null
  } | null
}

/**
 * 结果在回放里的可见时刻：优先结果生效的仿真时刻 effective_at，再看触发时刻。
 * requested_at / returned_at 是墙钟，不能拿来和回放头比较；完全没有仿真时间时按“一直可见”处理。
 */
export function resultVisibleAt(item: SimulationTimed): number {
  const at = item.timing?.effective_at ?? item.binding?.trigger_at ?? item.timing?.trigger_at ?? item.timing?.origin_event_at
  return at ? time(at) : Number.NEGATIVE_INFINITY
}

const formatter = (timeZone: string) => new Intl.DateTimeFormat('en-GB', {
  timeZone, hourCycle: 'h23', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
})

export const clock = (at: number, timeZone: string) => new Intl.DateTimeFormat('zh-CN', {
  timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
}).format(at)

export const clockWithSeconds = (at: number, timeZone: string) => new Intl.DateTimeFormat('zh-CN', {
  timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
}).format(at)

export function localHour(day: Day, at: number) {
  return Number(new Intl.DateTimeFormat('en-GB', { timeZone: day.timezone, hourCycle: 'h23', hour: '2-digit' }).format(at))
}

/** 本地时区的墙上时间换算回时刻；夏令时跳过或重复的整点也有确定结果。 */
export function dayAtHour(day: Day, hour: number) {
  const [year, month, date] = day.local_date.split('-').map(Number)
  const wallTime = Date.UTC(year, month - 1, date, hour)
  const format = formatter(day.timezone)
  const represented = (instant: number) => {
    const parts = Object.fromEntries(format.formatToParts(instant).map(p => [p.type, p.value]))
    return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day),
      Number(parts.hour), Number(parts.minute), Number(parts.second))
  }
  const candidates = new Set<number>()
  for (const probe of [wallTime - 86_400_000, wallTime, wallTime + 86_400_000])
    candidates.add(wallTime + (probe - represented(probe)))
  const exact = [...candidates].filter(candidate => represented(candidate) === wallTime)
  if (exact.length) return Math.min(...exact)
  // 午夜被夏令时跳过时，当天从跳变后的第一个有效时刻开始。
  return Math.max(...candidates)
}

export const dayStart = (day: Day) => dayAtHour(day, 0)
export const dayEnd = (day: Day) => dayAtHour(day, 24)
