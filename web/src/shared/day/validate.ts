/**
 * day.json 手写校验（#180 方案 A）：与 contracts/day.schema.json 的必填字段、时间格式对齐。
 * 展示页（B38）与指挥工作台（B34）共用同一模块；失败抛 DayDataError，消息里带字段路径。
 * 只校验展示与回放用得到的结构，不做完整 JSON Schema 校验。
 */

export class DayDataError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'DayDataError'
  }
}

export interface DayRecordLike {
  plant_id: string
  local_date: string
  timezone: string
  run_id: string
  runs: unknown[]
  [key: string]: unknown
}

const ARRAY_FIELDS = [
  'events', 'tickets', 'proposals', 'decisions', 'work_orders', 'temp_rules',
  'knowledge_versions', 'qa', 'agent_runs', 'metrics', 'state_series', 'ledger_minutes',
  // B49 追加：资料资产与证据事件，day.json 里可选；缺省归一为 []，旧导出照旧可加载。
  'assets', 'evidence',
] as const

/** schema 里带时间的条目：每个条目必须校验的顶层时间字段。 */
const ENTRY_TIME_FIELDS: Record<string, readonly string[]> = {
  events: ['at'],
  tickets: ['opened_at'],
  decisions: ['at'],
  work_orders: ['created_at'],
  knowledge_versions: ['activated_at'],
  qa: ['at'],
  state_series: ['at'],
  assets: ['available_at'],
  evidence: ['occurred_at'],
}

const TIMING_FIELDS = ['origin_event_at', 'trigger_at', 'effective_at', 'requested_at'] as const
const OFFSET_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/
const LOCAL_DATE = /^\d{4}-\d{2}-\d{2}$/

function object(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new DayDataError(`${path} 必须是对象`)
  return value as Record<string, unknown>
}

/** 时间必须是可解析、带 Z 或 ±hh:mm 的 ISO 8601；渲染层的 Intl 才不会抛 RangeError。 */
export function checkTimestamp(value: unknown, path: string): void {
  if (typeof value !== 'string') throw new DayDataError(`${path} 必须是带时区的 ISO 8601 时间字符串`)
  if (!OFFSET_TIMESTAMP.test(value) || Number.isNaN(Date.parse(value)))
    throw new DayDataError(`${path} 不是带时区（Z 或 ±hh:mm）的 ISO 8601 时间：${value}`)
}

export function checkTimezone(value: unknown, path = 'timezone'): void {
  if (typeof value !== 'string' || !value) throw new DayDataError(`${path} 必须是非空的 IANA 时区`)
  try {
    new Intl.DateTimeFormat('en-GB', { timeZone: value })
  } catch {
    throw new DayDataError(`${path} 不是浏览器可用的 IANA 时区：${value}`)
  }
}

function checkOptionalTimestamp(value: unknown, path: string): void {
  if (value !== undefined && value !== null) checkTimestamp(value, path)
}

function checkTiming(value: unknown, path: string, required = false): void {
  if (value === undefined || value === null) {
    if (required) throw new DayDataError(`${path} 缺少 timing`)
    return
  }
  const timing = object(value, path)
  for (const field of TIMING_FIELDS) checkTimestamp(timing[field], `${path}.${field}`)
  checkOptionalTimestamp(timing.returned_at, `${path}.returned_at`)
}

function checkBinding(value: unknown, path: string): void {
  if (value === undefined || value === null) return
  checkOptionalTimestamp(object(value, path).trigger_at, `${path}.trigger_at`)
}

function checkEntry(field: string, entry: unknown, path: string): void {
  const item = object(entry, path)
  for (const name of ENTRY_TIME_FIELDS[field] ?? []) checkTimestamp(item[name], `${path}.${name}`)
  checkOptionalTimestamp(item.closed_at, `${path}.closed_at`)
  if (field === 'proposals') {
    checkBinding(item.binding, `${path}.binding`)
    checkTiming(item.timing, `${path}.timing`)
  }
  if (field === 'agent_runs') {
    checkBinding(item.binding, `${path}.binding`)
    checkTiming(item.timing, `${path}.timing`, true)
    for (const node of (item.nodes as unknown[] | undefined) ?? []) {
      if (node && typeof node === 'object') {
        checkOptionalTimestamp((node as Record<string, unknown>).started_at, `${path}.nodes[].started_at`)
        checkOptionalTimestamp((node as Record<string, unknown>).finished_at, `${path}.nodes[].finished_at`)
      }
    }
    for (const call of (item.tool_calls as unknown[] | undefined) ?? []) {
      if (call && typeof call === 'object') checkOptionalTimestamp((call as Record<string, unknown>).started_at, `${path}.tool_calls[].started_at`)
    }
  }
  if (field === 'temp_rules') {
    const effective = object(item.effective, `${path}.effective`)
    checkTimestamp(effective.start, `${path}.effective.start`)
    checkTimestamp(effective.end, `${path}.effective.end`)
  }
  if (field === 'ledger_minutes') {
    const interval = object(item.interval, `${path}.interval`)
    checkTimestamp(interval.start, `${path}.interval.start`)
    checkTimestamp(interval.end, `${path}.interval.end`)
  }
  if (field === 'state_series') {
    for (const change of (item.changes as unknown[] | undefined) ?? []) {
      if (change && typeof change === 'object') checkTimestamp((change as Record<string, unknown>).at, `${path}.changes[].at`)
    }
  }
}

/** 校验并返回 day 记录；可选小节缺省为空数组（schema 允许省略）。 */
export function validateDay(raw: unknown): DayRecordLike {
  const record = object(raw, 'day.json')
  for (const field of ['plant_id', 'local_date', 'timezone', 'run_id'] as const) {
    if (typeof record[field] !== 'string' || !record[field]) throw new DayDataError(`day.json 缺少 ${field}`)
  }
  if (!LOCAL_DATE.test(record.local_date as string))
    throw new DayDataError(`local_date 必须是 YYYY-MM-DD：${String(record.local_date)}`)
  checkTimezone(record.timezone)
  if (!Array.isArray(record.runs) || record.runs.length === 0)
    throw new DayDataError('day.json 的 runs 至少需要一条运行记录（schema minItems: 1）')
  record.runs.forEach((run, index) => {
    const item = object(run, `runs[${index}]`)
    if (typeof item.run_id !== 'string' || !item.run_id)
      throw new DayDataError(`runs[${index}].run_id 必须是非空字符串`)
    checkOptionalTimestamp(item.forked_at, `runs[${index}].forked_at`)
  })
  const day = { ...record }
  for (const field of ARRAY_FIELDS) {
    const value = record[field]
    if (value === undefined || value === null) {
      day[field] = []
      continue
    }
    if (!Array.isArray(value)) throw new DayDataError(`day.json 的 ${field} 必须是数组`)
    value.forEach((entry, index) => checkEntry(field, entry, `${field}[${index}]`))
  }
  // 跨日引用小节 context：原样携带的前一日记录，时间字段同样要过校验，坏时间不能让后面的 Intl 抛异常。
  if (record.context !== undefined && record.context !== null) {
    const context = object(record.context, 'context')
    for (const field of ['proposals', 'events', 'tickets'] as const) {
      const value = context[field]
      if (value === undefined || value === null) continue
      if (!Array.isArray(value)) throw new DayDataError(`day.json 的 context.${field} 必须是数组`)
      value.forEach((entry, index) => checkEntry(field, entry, `context.${field}[${index}]`))
    }
  }
  return day as unknown as DayRecordLike
}
