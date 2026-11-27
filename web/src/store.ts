import { create } from 'zustand'
import baseSample from '../../contracts/examples/day.sample.json'
import { withShowcaseDemo } from './showcase/data/demoDay'
import type { Day as SampleDay } from './showcase/data/types'
// 控制台和展示页回放同一份内置样例：样例只有 08:05 前后一小段，其余由演示补全（标 synthetic / simulated）。
const sample = withShowcaseDemo(structuredClone(baseSample) as unknown as SampleDay) as unknown as typeof baseSample
/** 默认回放时刻：08:07:30，早高峰处置刚下发、待批准的那一条刚好出现。 */
const DEFAULT_CURSOR_OFFSET_MS = 7.5 * 60_000

type SampleProposal = typeof sample.proposals[number]
export type Day = Omit<typeof sample, 'proposals'> & {
  proposals: (Omit<SampleProposal, 'binding'> & { binding?: SampleProposal['binding'] | null })[]
}
export type UIEvent = { type: string; agent_run_id: string; seq: number; ts: string; event_id?: string | null; payload: Record<string, unknown> }
/** 处置席上的裁定：批准 / 驳回 / 抽查通过 / 发布。 */
export type Verdict = 'approved' | 'rejected' | 'sampled' | 'published'
export const time = (v: string) => new Date(v).getTime()
export function dayAtHour(day: Day, hour: number) {
  const [year, month, date] = day.local_date.split('-').map(Number)
  const wallTime = Date.UTC(year, month - 1, date, hour)
  const formatter = new Intl.DateTimeFormat('en-GB', {
    timeZone: day.timezone, hourCycle: 'h23', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
  const represented = (instant: number) => {
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(p => [p.type, p.value]))
    return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day),
      Number(parts.hour), Number(parts.minute), Number(parts.second))
  }
  // Offsets on both sides of a daylight-saving change give the instants that may represent the wall time.
  const candidates = new Set<number>()
  for (const probe of [wallTime - 86_400_000, wallTime, wallTime + 86_400_000])
    candidates.add(wallTime + (probe - represented(probe)))
  const exact = [...candidates].filter(candidate => represented(candidate) === wallTime)
  if (exact.length) return Math.min(...exact)
  // A skipped local time (e.g. midnight when clocks jump forward) starts at the first valid instant after it.
  return Math.max(...candidates)
}
export const dayStart = (day: Day) => dayAtHour(day, 0)
export const dayEnd = (day: Day) => dayAtHour(day, 24)
export function devicesAt(day: Day, at: number) {
  const frame = [...day.state_series].reverse().find(f => time(f.at) <= at)
  if (!frame) return {} as Record<string, Record<string, unknown>>
  const devices = structuredClone(frame.devices) as Record<string, Record<string, unknown>>
  // 同一帧的 changes 可能来自不同来源，先按 at 排序再应用，避免早到的读数盖掉后到的读数。
  const changes = [...frame.changes].sort((a, b) => time(a.at) - time(b.at))
  for (const change of changes) if (time(change.at) <= at && devices[change.entity_id]) devices[change.entity_id][change.field] = change.value
  return devices
}
/** 顶栏时钟左边只有在回放样例时出现，接上实时流后换成实时状态。 */
export type PlayMode = 'replay' | 'live'

type Store = {
  day: Day
  source: string
  cursor: number
  playing: boolean
  speed: number
  mode: PlayMode
  live: UIEvent[]
  liveAnswer: string
  /** 暂停自动执行：新事件只提议，预案保护动作照常执行 */
  paused: boolean
  pausedAt: number | null
  /** 已撤销的动作（按动作键） */
  retracted: string[]
  /** 处置席的裁定 */
  verdicts: Record<string, Verdict>
  /** 已取消的临时规则 */
  cancelledRules: string[]
  load: (d: Day, source: string) => void
  seek: (n: number) => void
  play: (v: boolean) => void
  setSpeed: (n: number) => void
  tick: () => void
  ingest: (event: UIEvent) => void
  setPaused: (paused: boolean) => void
  retract: (key: string) => void
  recordVerdict: (id: string, verdict: Verdict) => void
  cancelRule: (ruleId: string) => void
}
export const useDay = create<Store>((set) => ({
  day: sample, source: '标准演示场景', cursor: dayAtHour(sample, 8) + DEFAULT_CURSOR_OFFSET_MS, playing: false, speed: 60, mode: 'replay', live: [], liveAnswer: '',
  paused: false, pausedAt: null, retracted: [], verdicts: {}, cancelledRules: [],
  load: (day, source) => set({ day, source, cursor: dayAtHour(day, 8) + DEFAULT_CURSOR_OFFSET_MS, playing: false, live: [], liveAnswer: '', mode: 'replay', paused: false, pausedAt: null, retracted: [], verdicts: {}, cancelledRules: [] }),
  seek: cursor => set({ cursor }), play: playing => set({ playing }), setSpeed: speed => set({ speed }),
  tick: () => set(s => { const cursor = Math.min(dayEnd(s.day), s.cursor + s.speed * 1000); return { cursor, playing: cursor < dayEnd(s.day) } }),
  ingest: event => set(s => {
    if (s.live.some(e => e.agent_run_id === event.agent_run_id && (e.seq === event.seq || (e.event_id && e.event_id === event.event_id)))) return s
    return {
      mode: 'live',
      live: [...s.live, event],
      liveAnswer: event.type === 'run_started' ? '' : event.type === 'answer_delta' ? s.liveAnswer + String(event.payload.text ?? '') : s.liveAnswer,
    }
  }),
  setPaused: paused => set(s => ({ paused, pausedAt: paused ? s.cursor : null })),
  retract: key => set(s => ({ retracted: s.retracted.includes(key) ? s.retracted : [...s.retracted, key] })),
  recordVerdict: (id, verdict) => set(s => ({ verdicts: { ...s.verdicts, [id]: verdict } })),
  cancelRule: ruleId => set(s => ({ cancelledRules: s.cancelledRules.includes(ruleId) ? s.cancelledRules : [...s.cancelledRules, ruleId] })),
}))
export { sample }
