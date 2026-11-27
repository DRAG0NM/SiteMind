import sample from '../../../../contracts/examples/day.sample.json'
import { DayDataError, validateDay } from '../../shared/day/validate'
import { withShowcaseDemo } from './demoDay'
import { failureReason, fetchBackendDay, OFFLINE_NOTE, onlineRunId, requestedRunId, serverStatus, type PageLocation } from './online'
import type { Day } from './types'

export { DayDataError }

export const SAMPLE_LABEL = '标准演示场景 · 含合成补全'

export interface DaySource {
  kind: 'exported' | 'imported' | 'sample' | 'backend'
  label: string
  /** 在线模式没从后端拿到数据时的白话原因，顶栏显示在来源旁。 */
  note?: string
}

export interface LoadedDay {
  day: Day
  source: DaySource
}

export const sampleDay = (): Day => withShowcaseDemo(structuredClone(sample as unknown as Day))

/**
 * 按 contracts/day.schema.json 校验必填字段与时间格式：runs 至少一条，其余小节可省略并默认为空数组。
 * 与工作台共用 shared/day/validate；失败抛 DayDataError，消息带字段路径。
 */
export function parseDay(raw: unknown): Day {
  return validateDay(raw) as unknown as Day
}

const sampleLoaded = (note?: string): LoadedDay =>
  ({ day: sampleDay(), source: { kind: 'sample', label: SAMPLE_LABEL, ...(note ? { note } : {}) } })

/** 5xx 是后端自己出错，与“没有这次运行”不同：不拿内置样例掩盖，直接报错让页面显示错误条。 */
const serverError = (status: number) => new Error(`后端出错（HTTP ${status}）`)

/**
 * URL 带 ?run_id 时先读后端一天；失败退回同目录 day.json，再退回内置样例，
 * 并在 source.note 写明白话原因。没有 ?run_id 的离线单文件行为保持不变。
 * 后端或 day.json 返回 5xx 时不回退样例，抛错交给页面显示错误条。
 */
export async function loadDay(fetcher: typeof fetch = fetch, env: PageLocation = {}): Promise<LoadedDay> {
  const runId = onlineRunId(env)
  let note: string | undefined
  if (!runId && requestedRunId(env)) note = OFFLINE_NOTE
  if (runId) {
    try {
      const day = await fetchBackendDay(runId, parseDay, fetcher)
      return { day, source: { kind: 'backend', label: `后端 · 运行 ${runId}` } }
    } catch (reason) {
      const status = serverStatus(reason)
      if (status !== null) throw serverError(status)
      note = failureReason(reason)
    }
  }
  // file:// 下浏览器不允许读同目录 day.json，直接走内置样例，避免控制台留下资源加载失败的红字。
  if (typeof location !== 'undefined' && location.protocol === 'file:') return sampleLoaded(note)
  let response: Response
  try {
    response = await fetcher('day.json', { cache: 'no-store' })
  } catch {
    // 没有 day.json（静态托管缺文件、或 file:// 打开被浏览器拦截）时走内置样例。
    // 离线打开时用页面上的“导入 day.json”选择导出文件。
    return sampleLoaded(note)
  }
  if (response.status >= 500) throw serverError(response.status)
  if (!response.ok) return sampleLoaded(note)
  // 静态托管开了 SPA 回退时，缺 day.json 会把 index.html 以 200 返回；按“没有 day.json”回退样例，别当坏 JSON 报错。
  if ((response.headers.get('content-type') ?? '').includes('text/html')) return sampleLoaded(note)
  let raw: unknown
  try {
    raw = await response.json()
  } catch {
    throw new DayDataError('day.json 不是合法的 JSON')
  }
  const day = parseDay(raw)
  return { day, source: { kind: 'exported', label: `day.json · ${day.local_date}`, ...(note ? { note } : {}) } }
}
