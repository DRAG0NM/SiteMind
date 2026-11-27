/**
 * 统一读数层：同一时刻、同一读数，总览、能源页、舞台、控制台与园区标注都从这里取。
 * 只依赖 day.json 的普通 JSON 值，不依赖展示页或控制台的类型；限值统一读 metrics 的 config 组。
 *
 * 口径（#239 定案）：
 * - T1 到上限不算超：load > limit 才是 over，正好等于时是 at_limit（文案“已到上限”）。
 * - 余量按上限算：limit - load，正数是余量，负数是超限。
 * - 光伏功率与今日发电是两个字段，页面标签必须写明是哪一个。
 * - 空压站读数兼容 pressure_bar 与 pressure_mpa，统一输出 MPa。
 */

export interface ReadingInterval {
  start: string
  end: string
}

export interface ReadingStateChange {
  at: string
  entity_id: string
  field: string
  value: unknown
}

export interface ReadingStateFrame {
  at: string
  devices: Record<string, Record<string, unknown>>
  changes?: ReadingStateChange[]
}

export interface ReadingLedgerMinute {
  interval: ReadingInterval
  pv_used_kwh: number
  pv_available_kwh?: number
  discharge_kwh?: number
  charge_kwh?: number
}

export interface ReadingMetric {
  metric: string
  group: string
  value: number | null
}

/** 读数层需要的最小 day.json 形状；展示页与控制台的 Day 都满足。 */
export interface ReadingsDay {
  state_series: ReadingStateFrame[]
  ledger_minutes: ReadingLedgerMinute[]
  metrics: ReadingMetric[]
}

const time = (value: string) => new Date(value).getTime()

/** 回放头之前最后一帧设备快照；帧内 change 按时间顺序应用，后到的读数覆盖先到的。 */
export function devicesAt(day: Pick<ReadingsDay, 'state_series'>, at: number) {
  let frame: ReadingStateFrame | undefined
  for (const candidate of day.state_series) if (time(candidate.at) <= at) frame = candidate
  if (!frame) return {} as Record<string, Record<string, unknown>>
  const devices = structuredClone(frame.devices)
  const changes = [...(frame.changes ?? [])].sort((a, b) => time(a.at) - time(b.at))
  for (const change of changes) {
    if (time(change.at) <= at && devices[change.entity_id]) devices[change.entity_id][change.field] = change.value
  }
  return devices
}

/** 回放头前最后一次账本分钟，用来算瞬时功率（kWh/分钟 × 60）。保留元素的完整类型，页面还能读其他账本字段。 */
export function currentLedgerMinute<T extends { interval: ReadingInterval }>(
  day: { ledger_minutes: T[] }, cursor: number,
): T | undefined {
  let latest: T | undefined
  for (const minute of day.ledger_minutes) {
    if (time(minute.interval.end) > cursor) continue
    if (!latest || time(minute.interval.end) > time(latest.interval.end)) latest = minute
  }
  return latest
}

/** 配置项：缺失时返回 null，页面显示“缺少限值配置”，不写死数字。 */
export function configValue(day: Pick<ReadingsDay, 'metrics'>, metric: string): number | null {
  const found = day.metrics.find(item => item.metric === metric && item.group === 'config')
  return typeof found?.value === 'number' && Number.isFinite(found.value) ? found.value : null
}

export function transformerLimit(day: Pick<ReadingsDay, 'metrics'>): number | null {
  return configValue(day, 'transformer_limit_pct')
}

const TRANSFORMER = /transformer|\/t1\b|变压器/i
const COMPRESSOR = /compressor|air_system|空压/i
const ELEVATOR = /elevator|\/lift\b|电梯/i
const PV = /\/pv\/|solar|光伏/i

function firstDevice(day: Pick<ReadingsDay, 'state_series'>, cursor: number, match: RegExp) {
  return Object.entries(devicesAt(day, cursor)).find(([id]) => match.test(id))?.[1]
}

export type T1State = 'ok' | 'at_limit' | 'over'

export interface T1Reading {
  /** 负载百分比（四舍五入到整数，与页面显示一致） */
  loadPct: number | null
  /** 上限百分比；没有配置是 null */
  limitPct: number | null
  /** ok / at_limit / over；没有读数或没有限值时是 null */
  state: T1State | null
  /** 余量 = 上限 − 负载；负数表示超出。没有限值或读数时是 null */
  marginPct: number | null
}

/** T1 变压器负载读数。上限来自 metrics 的 transformer_limit_pct（config 组）。 */
export function t1(day: Pick<ReadingsDay, 'state_series' | 'metrics'>, cursor: number): T1Reading {
  const ratio = Number(firstDevice(day, cursor, TRANSFORMER)?.load_ratio)
  const rawPct = Number.isFinite(ratio) ? ratio * 100 : null
  const loadPct = rawPct === null ? null : Math.round(rawPct)
  const limitPct = transformerLimit(day)
  if (rawPct === null || loadPct === null || limitPct === null) {
    return { loadPct, limitPct, state: null, marginPct: null }
  }
  // 状态按原始比值判（0.854 不会因为取整成 85 被当成“已到上限”），取整只用于显示；浮点误差按 1e-9 容忍。
  const state: T1State = rawPct - limitPct > 1e-9 ? 'over' : Math.abs(rawPct - limitPct) <= 1e-9 ? 'at_limit' : 'ok'
  return { loadPct, limitPct, state, marginPct: Math.round(limitPct - rawPct) }
}

/** T1 状态的一句话：正常写余量，正好到上限写“已到上限”，超过写“超上限”。 */
export function t1Note(reading: T1Reading): string {
  if (reading.limitPct === null) return reading.loadPct === null ? '缺少读数' : '缺少限值配置'
  if (reading.loadPct === null) return '缺少读数'
  if (reading.state === 'over') return reading.marginPct === 0 ? '略超上限' : `超上限 ${-(reading.marginPct ?? 0)} 个百分点`
  if (reading.state === 'at_limit') return '已到上限'
  return reading.marginPct === 0 ? '距上限不足 1 个百分点' : `距上限 ${reading.marginPct} 个百分点`
}

export interface PvReading {
  /** 此刻出力（kW） */
  kwNow: number | null
  /** 今日累计发电（kWh） */
  kwhToday: number | null
}

/** 光伏读数：此刻出力来自当前账本分钟，今日发电是已回放分钟的累计。 */
export function pv(day: Pick<ReadingsDay, 'ledger_minutes'>, cursor: number): PvReading {
  const minute = currentLedgerMinute(day, cursor)
  let kwhToday = 0
  let seen = false
  for (const item of day.ledger_minutes) {
    if (time(item.interval.end) > cursor) continue
    const used = Number(item.pv_used_kwh)
    if (!Number.isFinite(used)) continue
    kwhToday += used
    seen = true
  }
  return {
    kwNow: minute ? Math.round(minute.pv_used_kwh * 60) : null,
    kwhToday: seen ? Math.round(kwhToday * 10) / 10 : null,
  }
}

/** 空压站母管压力：兼容 pressure_bar 与 pressure_mpa，统一输出 MPa（bar × 0.1）。 */
export function airPressure(device: Record<string, unknown> | null | undefined): number | null {
  if (!device) return null
  const mpa = Number(device.pressure_mpa)
  if (device.pressure_mpa != null && Number.isFinite(mpa)) return mpa
  const bar = Number(device.pressure_bar)
  if (device.pressure_bar != null && Number.isFinite(bar)) return Math.round(bar * 0.1 * 1000) / 1000
  return null
}

/**
 * 空压站设备：真实状态帧里 air_compressor/AC-1..3（只有 running/fault）排在带压力的 air_system/AIR 前面，
 * 同属空压站，所以取读数时优先选取得到压力的那一台，取不到才退回第一台。
 */
export function airDevice(
  devices: Record<string, Record<string, unknown>>,
  isAir: (entityId: string) => boolean,
): [string, Record<string, unknown>] | undefined {
  const air = Object.entries(devices).filter(([id]) => isAir(id))
  return air.find(([, device]) => airPressure(device) !== null) ?? air[0]
}

export type ElevatorBuilding = 'admin' | 'factory'

const ELEVATOR_BUILDING: { building: ElevatorBuilding; match: RegExp }[] = [
  { building: 'admin', match: /admin|行政|passenger|人梯|office/i },
  { building: 'factory', match: /factory|prod|车间|freight|货梯/i },
]

/**
 * 候梯人数：按楼栋汇总所有带 queue 字段的电梯设备（含 elevator_group/admin_low|admin_high），
 * 取不到任何队列读数时返回 null，页面显示“—”。
 */
export function elevatorQueue(
  day: Pick<ReadingsDay, 'state_series'>, cursor: number, building: ElevatorBuilding = 'admin',
): number | null {
  const matcher = ELEVATOR_BUILDING.find(item => item.building === building)?.match
  let total = 0
  let found = false
  for (const [id, device] of Object.entries(devicesAt(day, cursor))) {
    if (!ELEVATOR.test(id) || !matcher?.test(id)) continue
    const queue = Number(device.queue)
    if (!Number.isFinite(queue)) continue
    total += queue
    found = true
  }
  return found ? total : null
}

/** 园区标注要的三组读数，由页面统一从这里取，组件内不再解析设备字段。 */
export interface CampusReadings {
  t1: T1Reading
  pv: PvReading
  /** 行政楼候梯人数；取不到是 null */
  elevatorQueue: number | null
}

export function campusReadings(day: ReadingsDay, cursor: number): CampusReadings {
  return { t1: t1(day, cursor), pv: pv(day, cursor), elevatorQueue: elevatorQueue(day, cursor, 'admin') }
}

/** 按单位换算成百分比数值：ratio → ×100，% → 原值；认不出的单位返回 null。 */
export function percentValue(value: number | null | undefined, unit?: string | null): number | null {
  if (value == null || !Number.isFinite(value)) return null
  if (unit === '%' || unit === 'pct' || unit === 'percent') return value
  if (unit === 'ratio' || unit === '' || unit == null) return value * 100
  return null
}

/**
 * 两个带单位的指标之差（百分点）：先按各自单位换算到同一比例再相减。
 * 86%（unit=%）与 71%（unit=%）差 15 个百分点；0.86 与 0.71（ratio）同样差 15。
 */
export function pctDiff(
  a: { value: number | null | undefined; unit?: string | null },
  b: { value: number | null | undefined; unit?: string | null },
): number | null {
  const left = percentValue(a.value, a.unit)
  const right = percentValue(b.value, b.unit)
  if (left === null || right === null) return null
  return Math.round((left - right) * 10) / 10
}
