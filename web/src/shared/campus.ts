/**
 * 厂区模型：把数据实体归到轴测图的建筑与标注位，算颜色（正常 / 事件 / Agent 动作）和标注文字。
 * 展示页与控制台共用；只依赖普通 JSON 值，不依赖任何一侧的数据类型。
 *
 * 实体到建筑的映射按“实体所在区域”判断（Issue #181）：生产车间的照明、AHU 不能标到行政楼。
 * 楼栋归属优先用 shared/zones 的共享映射，本图只保留空压、光伏、冷却这类没有楼栋概念的落位。
 */

import { locateEntity } from './zones'
import { airEventStatus } from './labels'
import type { CampusReadings } from './readings'
import { airPressure, t1Note } from './readings'

export type CampusBuilding = 'factory' | 'admin' | 'energy' | 'air' | 'pv' | 'cooling'

/** 图上可点、可着色、可标注的位置。 */
export type CampusSpot =
  | 'factory'
  | 'equipment'
  | 'hvac'
  | 'admin'
  | 'elevator'
  | 'transformer'
  | 'battery'
  | 'compressor'
  | 'pv'
  | 'cooling'

export type CampusTone = 'normal' | 'event' | 'action'

/**
 * 事件/动作怎么着色：有独立形体的设备（变压器、储能柜、空调机组、电梯井等）整块上色；
 * 建筑本体（车间、行政楼）只描一圈外轮廓实线，内部产线、机组保持灰色。
 */
export type CampusPaint = 'fill' | 'outline'

export interface CampusEntity {
  spot: CampusSpot
  building: CampusBuilding
  tone: CampusTone
  paint: CampusPaint
  /** 标注牌标题 */
  label: string
  /** 标注牌数值；没有数据时是 — */
  value: string
  /** 悬停提示：认不出的命令把原命令名放这里 */
  title?: string
  /** 标注牌上的事件计数（实心方块里的数字） */
  alerts: number
  /** 对应的数据实体编号，点开证据抽屉时用 */
  entityId: string | null
  /** 0..1 的负载，供“负载着色” */
  load: number | null
}

export interface CampusEventRef {
  at: number
  entityId?: string | null
  summary?: string
  /** 事件类型：空压站事件按类型区分“压力异常 / 事件进行中”。 */
  eventType?: string | null
}

export interface CampusAction {
  entityId: string
  command: string
  params?: Record<string, unknown>
  start: number
  end: number
}

export interface CampusInput {
  at: number
  events: CampusEventRef[]
  actions: CampusAction[]
  devices: Record<string, Record<string, unknown>>
  /** 统一读数层算出的 T1 / 光伏 / 候梯读数；图上不再自己解析设备字段 */
  readings: CampusReadings
  /** 覆盖某个位置的标注文字（展示页的图表口径不一样时用） */
  values?: Partial<Record<CampusSpot, string>>
}

export const SPOTS: readonly CampusSpot[] = [
  'factory', 'equipment', 'hvac', 'admin', 'elevator', 'transformer', 'battery', 'compressor', 'pv', 'cooling',
]

export const SPOT_BUILDING: Record<CampusSpot, CampusBuilding> = {
  factory: 'factory',
  equipment: 'factory',
  hvac: 'factory',
  admin: 'admin',
  elevator: 'admin',
  transformer: 'energy',
  battery: 'energy',
  compressor: 'air',
  pv: 'pv',
  cooling: 'cooling',
}

export const SPOT_LABEL: Record<CampusSpot, string> = {
  factory: '生产车间',
  equipment: '车间设备',
  hvac: '暖通',
  admin: '行政楼',
  elevator: '行政楼电梯',
  transformer: 'T1 变压器',
  battery: '储能',
  compressor: '空压站',
  pv: '光伏车棚',
  cooling: '冷却塔',
}

export const SPOT_PAINT: Record<CampusSpot, CampusPaint> = {
  factory: 'outline',
  equipment: 'outline',
  hvac: 'fill',
  admin: 'outline',
  elevator: 'fill',
  transformer: 'fill',
  battery: 'fill',
  compressor: 'fill',
  pv: 'fill',
  cooling: 'fill',
}

const AREA_ADMIN = /admin|行政|office|人梯|乘客|passenger/
const AREA_FACTORY = /factory|车间|产线|production|prod\b|workshop|press|stamping|freight|货梯|工作区|plant_floor/

/**
 * 从实体编号取所在区域标记；没有区域标记返回 null。
 * 楼栋判定先走 B34/B38 共用的 shared/zones，认不出的再按本图特有的能源/空压/光伏/冷却类型兜底。
 */
export function areaOf(entityId: string): CampusBuilding | null {
  const id = entityId.toLowerCase()
  if (/air_?compressor|\/air\/|空压/.test(id)) return 'air'
  if (/\/pv\/|solar|光伏/.test(id)) return 'pv'
  // 行政楼冷水机组 CH-A1 在 site.yaml 标 12F（屋顶机房），不是园区冷却塔。
  if (/chiller|cooling|冷却/.test(id)) return locateEntity(entityId)?.building === 'admin' ? 'admin' : 'cooling'
  const location = locateEntity(entityId)
  if (location) {
    if (location.building === 'admin') return 'admin'
    if (location.building === 'factory') return 'factory'
    if (location.building === 'energy') return 'energy'
    // 特殊区域（洁净车间/危化品间/数据中心）在轴测图上没有独立形体，交给设备类型落位。
    return null
  }
  if (/energy|能源|substation|配电/.test(id)) return 'energy'
  if (AREA_ADMIN.test(id)) return 'admin'
  if (AREA_FACTORY.test(id)) return 'factory'
  return null
}

/** 数据实体 → 图上位置。认不出的实体返回 null，宁可不标也不标错。 */
export function spotOf(entityId: string | null | undefined): CampusSpot | null {
  if (!entityId) return null
  const id = entityId.toLowerCase()
  const area = areaOf(id)
  if (/transformer|\/t1\b|变压器/.test(id)) return 'transformer'
  if (/battery|bess|\/ess\b|储能/.test(id)) return area === 'pv' ? 'pv' : 'battery'
  if (/\/pv\/|solar|光伏/.test(id)) return 'pv'
  if (/compressor|air_system|空压/.test(id)) return 'compressor'
  if (/chiller|cooling_tower|cooling|冷却塔/.test(id)) return area === 'admin' ? 'admin' : 'cooling'
  if (/elevator|\/lift\b|电梯/.test(id)) return area === 'admin' ? 'elevator' : 'equipment'
  if (/lighting|照明|lamp/.test(id)) return area === 'admin' ? 'admin' : 'equipment'
  if (/hvac_zone|ahu|暖通|空调/.test(id)) return area === 'admin' ? 'admin' : 'hvac'
  if (/machine|press|robot|equipment|conveyor|设备/.test(id)) return 'equipment'
  if (area === 'admin') return 'admin'
  if (area === 'factory') return 'factory'
  return null
}

const COMMAND_LABEL: Record<string, string> = {
  discharge: '储能放电',
  charge: '储能充电',
  idle: '储能待机',
  zone_operation: '电梯分区运行',
  zone_run: '电梯分区运行',
  partition_run: '电梯分区运行',
  set_mode: '电梯切换运行模式',
  defer_precool: '暖通推迟预冷',
  precool_delay: '暖通推迟预冷',
  switch_circuit: '照明切备用回路',
  open_work_order: '开维修工单',
  create_work_order: '开维修工单',
  rescue: '电梯救援工单',
  stop: '停机',
  setpoint: '调整设定值',
  set_temperature: '临时调温',
}

const paramText = (params: Record<string, unknown> | undefined, keys: string[], unit: string) => {
  if (!params) return ''
  for (const key of keys) {
    const value = Number(params[key])
    if (Number.isFinite(value)) return ` ${value}${unit}`
  }
  return ''
}

/** 动作的人类可读文字：命令用中文名，参数带上单位和数值；认不出的命令用中文兜底。 */
export function describeAction(action: CampusAction): string {
  const base = COMMAND_LABEL[action.command] ?? '执行控制指令'
  const params = action.params ?? {}
  if (action.command === 'discharge' || action.command === 'charge') return base + paramText(params, ['kw', 'power_kw'], ' kW')
  if (action.command.startsWith('defer_precool') || action.command.startsWith('precool_delay'))
    return base + paramText(params, ['minutes', 'duration_min'], ' 分钟')
  if (action.command === 'set_temperature') return base + paramText(params, ['temperature_degC', 'target'], ' ℃')
  if (action.command === 'switch_circuit') return params.circuit ? `${base} ${String(params.circuit)}` : base
  return base
}

/** 认不出的命令把原文放进悬停提示，界面上不直接露英文枚举。 */
export function actionTitle(action: CampusAction): string | undefined {
  return COMMAND_LABEL[action.command] ? undefined : action.command
}

const pct = (value: number) => `${Math.round(value * 100)}`
const num1 = (value: number) => (Math.round(value * 10) / 10).toFixed(1)

/** 设备快照 → 标注文字与负载。T1 / 光伏 / 电梯读数来自统一读数层，其余设备字段在本图内解析。 */
function deviceFacts(devices: Record<string, Record<string, unknown>>, readings: CampusReadings) {
  const facts: Partial<Record<CampusSpot, { value: string; load: number | null; entityId: string }>> = {}
  for (const [entityId, device] of Object.entries(devices)) {
    const spot = spotOf(entityId)
    if (!spot) continue
    // 空压站同时有 AC-1..3（只有运行状态）和 AIR（压力）：每台都要看，状态取“任一台运行”，压力取带压力的那台。
    if (facts[spot] && spot !== 'compressor') continue
    if (spot === 'transformer') {
      const reading = readings.t1
      if (reading.loadPct !== null) {
        facts.transformer = {
          value: `负载 ${reading.loadPct}% · ${t1Note(reading)}`,
          load: reading.loadPct / 100,
          entityId,
        }
      }
      continue
    }
    if (spot === 'battery') {
      const soc = Number(device.soc)
      const mode = String(device.mode ?? '')
      const modeText = mode === 'discharge' ? '放电中' : mode === 'charge' ? '充电中' : mode === 'idle' ? '待机' : ''
      const parts = [modeText, Number.isFinite(soc) ? `SOC ${pct(soc)}%` : ''].filter(Boolean)
      facts.battery = { value: parts.length ? parts.join(' · ') : '—', load: null, entityId }
      continue
    }
    if (spot === 'compressor') {
      const pressure = airPressure(device)
      const running = device.running
      const earlier = facts.compressor?.value.split(' · ')[0]
      // 空压站有多台：任一台在运行就算运行中
      const state = running === true || earlier === '运行中' ? '运行中' : running === false ? '停机' : earlier === '停机' ? earlier : ''
      const earlierPressure = facts.compressor?.value.split(' · ').find(part => part.endsWith('MPa'))
      const parts = [state, pressure !== null ? `${pressure.toFixed(2)} MPa` : earlierPressure ?? ''].filter(Boolean)
      facts.compressor = { value: parts.length ? parts.join(' · ') : '—', load: null, entityId: pressure !== null || !facts.compressor ? entityId : facts.compressor.entityId }
      continue
    }
    if (spot === 'pv') {
      const power = readings.pv.kwNow
      facts.pv = { value: power !== null ? `出力 ${Math.round(power)} kW` : '—', load: null, entityId }
      continue
    }
    if (spot === 'elevator') {
      const queue = readings.elevatorQueue
      facts.elevator = { value: queue !== null ? `候梯 ${queue} 人` : '—', load: null, entityId }
      continue
    }
    if (spot === 'admin' || spot === 'hvac') {
      const supply = Number(device.supply_temp_degC)
      const temperature = Number(device.temperature_degC)
      if (Number.isFinite(supply) && !facts[spot])
        facts[spot] = { value: `送风 ${num1(supply)} ℃`, load: null, entityId }
      else if (Number.isFinite(temperature) && !facts[spot])
        facts[spot] = { value: `室温 ${num1(temperature)} ℃`, load: null, entityId }
    }
  }
  // 车间产线设备不在图上有独立标注，台数在剖面图里用。
  const running = Object.entries(devices).filter(([id, device]) => spotOf(id) === 'equipment' && device.running !== false)
  if (running.length && !facts.equipment)
    facts.equipment = { value: `${running.length} 台运行`, load: null, entityId: running[0][0] }
  return facts
}

/** 把一天的数据折成图上要的实体列表：有进行中事件→事件色，有生效中动作→动作色，否则正常。 */
export function buildCampus(input: CampusInput): CampusEntity[] {
  const entities = new Map<CampusSpot, CampusEntity>()
  for (const spot of SPOTS) {
    entities.set(spot, {
      spot,
      building: SPOT_BUILDING[spot],
      tone: 'normal',
      paint: SPOT_PAINT[spot],
      label: SPOT_LABEL[spot],
      value: '—',
      alerts: 0,
      entityId: null,
      load: null,
    })
  }

  const active = input.actions.filter(action => action.start <= input.at && action.end > input.at)
  const facts = deviceFacts(input.devices, input.readings)

  for (const [spot, fact] of Object.entries(facts) as [CampusSpot, NonNullable<(typeof facts)[CampusSpot]>][]) {
    const entity = entities.get(spot)!
    entity.value = fact.value
    entity.load = fact.load
    entity.entityId = fact.entityId
  }

  for (const event of input.events) {
    // 回放到事件之前时不能亮：这里按回放头再兜一层，未来事件不算。
    if (event.at > input.at) continue
    const spot = spotOf(event.entityId)
    if (!spot) continue
    // 车间设备位没有独立形体（货梯、产线、照明等）：事件只落到车间本体上，由外轮廓表示。
    const entity = entities.get(spot === 'equipment' ? 'factory' : spot)!
    entity.tone = 'event'
    entity.alerts += 1
    entity.entityId ??= event.entityId ?? null
  }

  // 空压站有进行中事件时按事件类型说状态：负荷突增不写成压力异常。
  const compressorEvent = input.events
    .filter(event => event.at <= input.at && event.eventType && spotOf(event.entityId) === 'compressor')
    .sort((a, b) => b.at - a.at)[0]
  if (compressorEvent?.eventType) entities.get('compressor')!.value = airEventStatus(compressorEvent.eventType)

  // 车间这类建筑本体没有量测点时，用这一区域的设备台数说话。
  const factoryDevices = Object.keys(input.devices).filter(id => {
    const spot = spotOf(id)
    return spot === 'equipment' || spot === 'hvac' || spot === 'factory'
  })
  if (factoryDevices.length && entities.get('factory')!.value === '—')
    entities.get('factory')!.value = `接入 ${factoryDevices.length} 台设备`

  for (const action of active) {
    const spot = spotOf(action.entityId)
    if (!spot) continue
    // 动作同样按建筑级处理：车间设备位只让车间外轮廓动，内部设备不上 Agent 动作色。
    const entity = entities.get(spot === 'equipment' ? 'factory' : spot)!
    if (entity.tone !== 'event') entity.tone = 'action'
    entity.entityId ??= action.entityId
    // 事件进行中时保留事件状态文案；没有事件时动作描述才覆盖它。
    if (entity.tone === 'action' && spot !== 'equipment') {
      entity.value = describeAction(action)
      entity.title = actionTitle(action)
    }
  }

  for (const [spot, value] of Object.entries(input.values ?? {}) as [CampusSpot, string][])
    entities.get(spot)!.value = value

  return SPOTS.map(spot => entities.get(spot)!)
}

/** 某个建筑上最高的负载，供“负载着色”。 */
export function buildingLoad(entities: CampusEntity[], building: CampusBuilding): number | null {
  let load: number | null = null
  for (const entity of entities) {
    if (entity.building !== building || entity.load == null) continue
    load = Math.max(load ?? 0, entity.load)
  }
  return load
}

export function entityAt(entities: CampusEntity[], spot: CampusSpot | null | undefined) {
  return spot ? entities.find(entity => entity.spot === spot) : undefined
}
