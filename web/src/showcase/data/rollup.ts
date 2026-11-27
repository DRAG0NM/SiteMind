import type { CampusAction, CampusInput, CampusSpot } from '../../shared/campus'
import { actionTitle, describeAction, spotOf } from '../../shared/campus'
import {
  airDevice, airPressure, campusReadings, configValue, currentLedgerMinute, pv as pvReading, t1 as t1Reading, t1Note,
  transformerLimit as configTransformerLimit,
} from '../../shared/readings'
import { activeEvents, decisionVisible, devicesAt } from './selectors'
import { clock, resultVisibleAt, time } from './time'
import type { Day, Decision, Proposal } from './types'

export { currentLedgerMinute }

/** 回放头前最后一次账本分钟，用来算瞬时功率（kWh/分钟 × 60）。 */
export function powerOf(day: Day, cursor: number) {
  const minute = currentLedgerMinute(day, cursor)
  return minute ? minute.load_kwh * 60 : undefined
}

export function pvPowerOf(day: Day, cursor: number) {
  return pvReading(day, cursor).kwNow ?? undefined
}

/** 已被裁决选中、且此刻仍在执行窗口里的动作。 */
export function activeActions(day: Day, cursor: number): CampusAction[] {
  const selected = new Set<string>()
  for (const decision of day.decisions) {
    if (!decisionVisible(day, decision, cursor)) continue
    for (const id of decision.result.selected_proposal_ids) selected.add(id)
  }
  const actions: CampusAction[] = []
  for (const proposal of day.proposals) {
    if (proposal.proposal_id && !selected.has(proposal.proposal_id)) continue
    for (const action of proposal.actions ?? []) {
      const start = time(action.interval.start)
      const end = time(action.interval.end)
      if (start > cursor || end <= cursor) continue
      actions.push({ entityId: action.entity_id, command: action.command, params: action.params, start, end })
    }
  }
  return actions
}

/** 某个提案里此刻正在执行的动作。 */
export function proposalActions(proposal: Proposal, cursor: number): CampusAction[] {
  return (proposal.actions ?? [])
    .filter(action => time(action.interval.start) <= cursor && time(action.interval.end) > cursor)
    .map(action => ({
      entityId: action.entity_id,
      command: action.command,
      params: action.params,
      start: time(action.interval.start),
      end: time(action.interval.end),
    }))
}

/** 折成厂区图要的输入：近 20 分钟内进行中的事件、此刻生效的动作、设备快照与统一读数。 */
export function campusInput(day: Day, cursor: number, values?: Partial<Record<CampusSpot, string>>): CampusInput {
  return {
    at: cursor,
    events: activeEvents(day, cursor, 20 * 60_000)
      .filter(event => Boolean(event.entity_id))
      .map(event => ({ at: time(event.at), entityId: event.entity_id, summary: event.summary, eventType: event.event_type })),
    actions: activeActions(day, cursor),
    devices: devicesAt(day, cursor),
    readings: campusReadings(day, cursor),
    values,
  }
}

/** 右栏的三条 Agent 动作：动作名 + 时段。 */
export function actionRows(day: Day, cursor: number) {
  return activeActions(day, cursor).map(action => {
    const entityId = action.entityId
    const spot = spotOf(entityId) ?? 'factory'
    return {
      key: `${entityId}-${action.command}-${action.start}`,
      spot,
      entityId,
      label: describeAction(action),
      title: actionTitle(action),
      detail: `${clock(action.start, day.timezone)} 起 · 至 ${clock(action.end, day.timezone)}`,
    }
  })
}

/** T1 负载全天曲线：状态帧里的负载率与帧内逐分钟改动都画，缺帧补空，不插值。 */
export function transformerLoadPoints(day: Day) {
  return day.state_series
    .flatMap(frame => {
      const transformer = Object.entries(frame.devices).find(([id]) => spotOf(id) === 'transformer')
      const ratio = Number(transformer?.[1].load_ratio)
      // 帧内的逐分钟变化也画进曲线（比如裁决下发后的回落）
      const changes = (frame.changes ?? [])
        .filter(change => change.entity_id === transformer?.[0] && change.field === 'load_ratio')
        .map(change => ({ at: time(change.at), value: Number(change.value) * 100 }))
      return [...(Number.isFinite(ratio) ? [{ at: time(frame.at), value: ratio * 100 }] : []), ...changes]
    })
    .filter(point => Number.isFinite(point.value))
    .sort((a, b) => a.at - b.at)
}

/** T1 负载百分比（整数），没有读数返回 null。 */
function transformerPct(day: Day, cursor: number) {
  return t1Reading(day, cursor).loadPct
}

/**
 * 一次裁决的效果：下发前与动作生效期间（最早结束的动作之前一分钟）的 T1 负载。
 * 只对自动执行且有生效动作的裁决出卡；卡片是否出现、写什么都只看回放头之前的读数，不用未来读数决定。
 * 契约里没有试算结果字段：读数还没变化之前（done=false）只给 before，界面写“已裁决，效果待观察”；
 * 读数发生变化后（或动作窗口已结束）done=true，after 取回放头此刻的实际读数（与左栏一致），没有改善也如实给出。
 */
export type DecisionEffect =
  | { before: number; after: number; limit: number | null; done: true }
  | { before: number; after: null; limit: number | null; done: false }

/** 效果的一句话：降 N / 升 N / 无变化个百分点。 */
export const effectDelta = (effect: { before: number; after: number }) =>
  effect.after < effect.before ? `降 ${effect.before - effect.after} 个百分点`
    : effect.after > effect.before ? `升 ${effect.after - effect.before} 个百分点` : '无变化'

export function decisionEffect(day: Day, decision: Decision | null | undefined, cursor: number): DecisionEffect | null {
  if (!decision || decision.result.outcome !== 'executed') return null
  const at = time(decision.at)
  if (at > cursor) return null // 裁决还没发生，③不出卡
  const actions = activeActions(day, at + 60_000)
  if (!actions.length) return null
  const windowEnd = Math.min(...actions.map(action => action.end))
  const before = transformerPct(day, at)
  if (before === null) return null
  // 读数按整分钟采样：起点向上取整到分钟，终点向下取整；只扫到回放头为止。
  const firstMinute = Math.ceil((at + 60_000) / 60_000) * 60_000
  // 动作窗口不足两分钟时窗口内没有整分钟采样点，用窗口后的第一个采样点兜底，避免永远停在“待观察”。
  const lastMinute = Math.max(firstMinute, Math.floor((windowEnd - 60_000) / 60_000) * 60_000)
  const readAt = Math.min(cursor, lastMinute)
  let changed = false
  for (let minute = firstMinute; minute <= readAt; minute += 60_000) {
    const value = transformerPct(day, minute)
    if (value !== null && value !== before) { changed = true; break }
  }
  const limit = configTransformerLimit(day)
  const after = readAt >= firstMinute ? transformerPct(day, readAt) : null
  return after !== null && (changed || cursor > lastMinute)
    ? { before, after, limit, done: true }
    : { before, after: null, limit, done: false }
}

/** 配置项：样例里带的限值与容量，缺失时页面写 —。 */
export const config = {
  transformerLimit: (day: Day) => configTransformerLimit(day) ?? undefined,
  socFloor: (day: Day) => configValue(day, 'battery_soc_floor_pct') ?? undefined,
  contractCapacity: (day: Day) => configValue(day, 'contract_capacity_kw') ?? undefined,
  airPressureFloor: (day: Day) => configValue(day, 'compressed_air_floor_mpa') ?? undefined,
  airLeakThreshold: (day: Day) => configValue(day, 'compressed_air_leak_threshold') ?? undefined,
  supplySetpoint: (day: Day) => configValue(day, 'hvac_supply_setpoint_c') ?? undefined,
}

export interface Reading {
  key: string
  label: string
  value: string
  unit: string
  /** 竖条填充位置 0..100 */
  pct: number | null
  /** 限值刻度位置 0..100 */
  tick: number | null
  note: string
  over: boolean
}

const ratioPct = (value: number | undefined) => (value === undefined ? null : Math.round(value * 100))

/** 总览左栏的六个读数：T1 / 光伏来自统一读数层，储能、负荷、空压、送风按同一份设备快照取。 */
export function readings(day: Day, cursor: number): Reading[] {
  const t1Read = t1Reading(day, cursor)
  const pvRead = pvReading(day, cursor)
  const devices = devicesAt(day, cursor)
  const find = (match: (id: string) => boolean) => Object.entries(devices).find(([id]) => match(id))?.[1]
  const battery = find(id => spotOf(id) === 'battery')
  const compressor = airDevice(devices, id => spotOf(id) === 'compressor')?.[1]
  const hvac = find(id => spotOf(id) === 'hvac')
  const soc = Number(battery?.soc)
  const power = powerOf(day, cursor)
  const pressure = airPressure(compressor)
  const supply = Number(hvac?.supply_temp_degC)
  const socFloor = config.socFloor(day)
  const capacity = config.contractCapacity(day)
  const pressureFloor = config.airPressureFloor(day)
  const setpoint = config.supplySetpoint(day)
  const pctOf = (value: number | undefined, max: number) => (Number.isFinite(value) ? Math.round(((value as number) / max) * 100) : null)
  const t1Limit = t1Read.limitPct

  return [
    {
      key: 'transformer',
      label: 'T1 负载',
      value: t1Read.loadPct === null ? '—' : String(t1Read.loadPct),
      unit: '%',
      pct: t1Read.loadPct,
      tick: t1Limit,
      note: t1Read.loadPct === null
        ? '缺少读数'
        : `${t1Note(t1Read)}${t1Limit === null ? '' : ` · 上限 ${t1Limit}%`}`,
      over: t1Read.state === 'over',
    },
    {
      key: 'soc',
      label: '储能 SOC',
      value: Number.isFinite(soc) ? String(Math.round(soc * 100)) : '—',
      unit: '%',
      pct: ratioPct(Number.isFinite(soc) ? soc : undefined),
      tick: socFloor ?? null,
      note: socFloor != null ? `放电下限 ${socFloor}%` : '缺少限值配置',
      over: false,
    },
    {
      key: 'load',
      label: '全厂负荷',
      value: power === undefined ? '—' : String(Math.round(power)),
      unit: 'kW',
      pct: capacity != null ? pctOf(power, capacity) : null,
      tick: capacity != null ? 100 : null,
      note: capacity != null ? `合同容量 ${capacity} kW` : '缺少容量配置',
      over: capacity != null && power !== undefined && power > capacity,
    },
    {
      key: 'pv',
      label: '光伏出力',
      value: pvRead.kwNow === null ? '—' : String(pvRead.kwNow),
      unit: 'kW',
      // 当日累计发电只作旁注，不混进“此刻出力”的读数。
      pct: null,
      tick: null,
      note: pvRead.kwhToday === null ? '此刻出力 · 今日累计 —' : `此刻出力 · 今日累计 ${pvRead.kwhToday.toFixed(1)} kWh`,
      over: false,
    },
    {
      key: 'air',
      label: '空压母管压力',
      value: pressure === null ? '—' : pressure.toFixed(2),
      unit: 'MPa',
      pct: pctOf(pressure ?? undefined, 1),
      tick: pressureFloor != null ? Math.round(pressureFloor * 100) : null,
      note: pressureFloor != null ? `下限 ${pressureFloor.toFixed(2)} MPa` : '缺少限值配置',
      over: pressure !== null && pressureFloor != null && pressure < pressureFloor,
    },
    {
      key: 'supply',
      label: '车间送风温度',
      value: Number.isFinite(supply) ? supply.toFixed(1) : '—',
      unit: '℃',
      pct: pctOf(Number.isFinite(supply) ? supply : undefined, 40),
      tick: setpoint != null ? Math.round((setpoint / 40) * 100) : null,
      note: setpoint != null ? `设定 ${setpoint} ℃` : '缺少设定值',
      over: false,
    },
  ]
}

/** “今天到现在”的四个计数：全部来自 day.json。 */
export function todayStats(day: Day, cursor: number) {
  const decided = day.decisions.filter(decision => decisionVisible(day, decision, cursor))
  const layer12Executed = decided.filter(decision => decision.layer <= 2 && decision.result.outcome === 'executed').length
  const layer3 = decided.filter(decision => decision.layer >= 3).length
  const rehearsed = decided.filter(decision => (decision.conversion.notes ?? []).some(note => note.includes('预演')))
  const rehearsalOk = rehearsed.filter(decision => decision.result.outcome === 'executed').length
  const events = day.events.filter(event => time(event.at) <= cursor).length
  const tickets = day.tickets.filter(ticket => time(ticket.opened_at) <= cursor).length
  const evidence = new Set(day.proposals
    .filter(proposal => resultVisibleAt(proposal) <= cursor)
    .flatMap(proposal => proposal.evidence_refs ?? [])).size
  return { layer12Executed, layer3, rehearsalOk, rehearsalTotal: rehearsed.length, events, tickets, evidence }
}

/** 场景章节：把一天切成四段，播放条上的章节带用。 */
export function chapters() {
  return [
    { key: 'night', index: '序', name: '夜间谷段', from: 0, to: 6 },
    { key: 'act1', index: '第一幕', name: '早高峰争容量', from: 6, to: 10 },
    { key: 'act2', index: '第二幕', name: '午后设备异常', from: 10, to: 17 },
    { key: 'act3', index: '第三幕', name: '晚峰复盘与知识更新', from: 17, to: 24 },
  ]
}

export function chapterAt(day: Day, cursor: number) {
  const minutes = new Intl.DateTimeFormat('en-GB', { timeZone: day.timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
    .format(cursor)
  const [hour, minute] = minutes.split(':').map(Number)
  const position = hour + minute / 60
  const list = chapters()
  return list.find(chapter => position >= chapter.from && position < chapter.to) ?? list[list.length - 1]
}

