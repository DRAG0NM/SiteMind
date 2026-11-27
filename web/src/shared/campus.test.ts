import { expect, it } from 'vitest'
import { actionTitle, buildCampus, describeAction, entityAt, spotOf, SPOT_BUILDING } from './campus'
import type { CampusReadings } from './readings'

/** 只测着色与文案的用例不关心读数，给一组空读数。 */
export const noReadings: CampusReadings = {
  t1: { loadPct: null, limitPct: null, state: null, marginPct: null },
  pv: { kwNow: null, kwhToday: null },
  elevatorQueue: null,
}

const base = { at: 0, events: [], actions: [], devices: {}, readings: noReadings }
const at = (minute: number) => minute * 60_000

it('B17 导出的实体名按区域落位：生产车间的照明、AHU 不标到行政楼（Issue #181）', () => {
  expect(spotOf('entity://demo-electronics/air_compressor/AC-2')).toBe('compressor')
  expect(spotOf('entity://demo-electronics/lighting/ADMIN-3F')).toBe('admin')
  expect(spotOf('entity://demo-electronics/lighting/FACTORY-1F')).toBe('equipment')
  expect(spotOf('entity://demo-electronics/lighting/LT-A03')).toBe('admin')
  expect(spotOf('entity://demo-electronics/hvac_zone/ADMIN-3F')).toBe('admin')
  expect(spotOf('entity://demo-electronics/hvac_zone/FACTORY-AHU')).toBe('hvac')
  expect(spotOf('entity://demo-electronics/ahu/AHU-2')).toBe('hvac')
})

it('人梯归行政楼、货梯归车间设备位，储能与变压器归能源站', () => {
  for (const id of ['elevator_group/admin_low', 'elevator_group/admin_high', 'passenger_elevator/PE-1']) {
    expect(spotOf(`entity://demo-electronics/${id}`)).toBe('elevator')
    expect(SPOT_BUILDING.elevator).toBe('admin')
  }
  for (const id of ['elevator_group/freight', 'elevator_group/EG-PROD', 'freight_elevator/FE-2']) {
    expect(spotOf(`entity://demo-electronics/${id}`)).toBe('equipment')
    expect(SPOT_BUILDING.equipment).toBe('factory')
  }
  expect(spotOf('entity://demo-electronics/transformer/T1')).toBe('transformer')
  expect(spotOf('entity://demo-electronics/battery/BESS1')).toBe('battery')
  expect(spotOf('entity://demo-electronics/pv/PV-1')).toBe('pv')
  expect(spotOf('entity://demo-electronics/chiller/CH-1')).toBe('cooling')
  expect(spotOf('entity://unknown/topic')).toBeNull()
})

it('进行中的事件用事件色并带计数，生效中的动作只给动作色', () => {
  const entities = buildCampus({
    ...base,
    at: at(10),
    events: [{ at: at(9), entityId: 'entity://demo-electronics/transformer/T1', summary: 'T1 越限' }],
    actions: [{ entityId: 'entity://demo-electronics/battery/BESS1', command: 'discharge', params: { kw: 390 }, start: at(6), end: at(30) }],
    devices: { 'entity://demo-electronics/transformer/T1': { load_ratio: 0.96 } },
    readings: {
      t1: { loadPct: 96, limitPct: 85, state: 'over', marginPct: -11 },
      pv: { kwNow: null, kwhToday: null },
      elevatorQueue: null,
    },
  })
  const transformer = entities.find(entity => entity.spot === 'transformer')!
  expect(transformer.tone).toBe('event')
  expect(transformer.alerts).toBe(1)
  expect(transformer.value).toBe('负载 96% · 超上限 11 个百分点')
  expect(transformer.load).toBeCloseTo(0.96)
  const battery = entities.find(entity => entity.spot === 'battery')!
  expect(battery.tone).toBe('action')
  expect(battery.value).toBe('储能放电 390 kW')
  expect(entities.find(entity => entity.spot === 'pv')!.value).toBe('—')
})

it('事件只能绑到建筑时，建筑外轮廓吃事件色、内部设备保持灰', () => {
  const entities = buildCampus({
    ...base,
    at: at(10),
    // 货梯 EG-PROD 没有独立形体，只能落到车间设备位 → 由车间外轮廓表示。
    events: [{ at: at(9), entityId: 'entity://demo-electronics/elevator_group/EG-PROD', summary: '货梯困人' }],
  })
  const factory = entityAt(entities, 'factory')!
  expect(factory.tone).toBe('event')
  expect(factory.paint).toBe('outline')
  expect(factory.alerts).toBe(1)
  // 内部产线、机组、空调箱不吃事件色。
  expect(entityAt(entities, 'equipment')!.tone).toBe('normal')
  expect(entityAt(entities, 'hvac')!.tone).toBe('normal')
  // 有独立形体的设备仍整块上色。
  const withTransformer = buildCampus({
    ...base,
    at: at(10),
    events: [{ at: at(9), entityId: 'entity://demo-electronics/transformer/T1', summary: 'T1 越限' }],
  })
  expect(entityAt(withTransformer, 'transformer')!.tone).toBe('event')
  expect(entityAt(withTransformer, 'transformer')!.paint).toBe('fill')
})

it('回放到事件之前就回到正常色，动作过了执行窗口也不再是动作色', () => {
  const input = {
    ...base,
    events: [{ at: at(30), entityId: 'entity://demo-electronics/compressor/AIR-1', summary: '空压压力下降' }],
    actions: [{ entityId: 'entity://demo-electronics/battery/BESS1', command: 'discharge', params: { kw: 390 }, start: at(6), end: at(10) }],
  }
  const before = buildCampus({ ...input, at: at(29) })
  expect(before.find(entity => entity.spot === 'compressor')!.tone).toBe('normal')
  const after = buildCampus({ ...input, at: at(30) })
  expect(after.find(entity => entity.spot === 'compressor')!.tone).toBe('event')
  expect(after.find(entity => entity.spot === 'battery')!.tone).toBe('normal')
})

it('动作文字带参数与单位，未知命令用中文兜底', () => {
  expect(describeAction({ entityId: 'e', command: 'discharge', params: { kw: 390 }, start: 0, end: 1 })).toBe('储能放电 390 kW')
  expect(describeAction({ entityId: 'e', command: 'defer_precool', params: { minutes: 15 }, start: 0, end: 1 })).toBe('暖通推迟预冷 15 分钟')
  expect(describeAction({ entityId: 'e', command: 'zone_operation', start: 0, end: 1 })).toBe('电梯分区运行')
  expect(describeAction({ entityId: 'e', command: 'custom_cmd', start: 0, end: 1 })).toBe('执行控制指令')
  expect(actionTitle({ entityId: 'e', command: 'custom_cmd', start: 0, end: 1 })).toBe('custom_cmd')
})

it('zone:admin 是楼栋级事件：只出一条“行政楼”，不按 12 层拆成多条', () => {
  expect(spotOf('zone:admin')).toBe('admin')
  const entities = buildCampus({
    ...base,
    at: at(10),
    events: [{ at: at(9), entityId: 'zone:admin', summary: '气象预警：午后极端高温' }],
  })
  const admin = entityAt(entities, 'admin')!
  expect(admin.tone).toBe('event')
  expect(admin.alerts).toBe(1)
  expect(admin.label).toBe('行政楼')
  expect(entities.filter(entity => entity.building === 'admin' && entity.tone === 'event')).toHaveLength(1)
})

it('行政楼冷水机组 CH-A1 归行政楼，不落到园区冷却塔', () => {
  expect(spotOf('entity://demo-electronics/chiller/CH-A1')).toBe('admin')
  expect(spotOf('entity://demo-electronics/chiller/CH-1')).toBe('cooling')
})

it('空压站事件按事件类型说状态：压力类才写压力异常，负荷突增写事件进行中', () => {
  const withEvent = (eventType: string) => buildCampus({
    ...base,
    at: at(10),
    events: [{ at: at(9), entityId: 'entity://demo-electronics/air_compressor/AC-2', eventType, summary: '空压站压力下降' }],
  })
  expect(entityAt(withEvent('air_pressure_abnormal'), 'compressor')!.value).toBe('压力异常')
  expect(entityAt(withEvent('load_spike'), 'compressor')!.value).toBe('事件进行中')
  expect(entityAt(withEvent('compressor_pressure_drop'), 'compressor')!.value).toBe('压力异常')
})

it('air_system 聚合实体也落到空压站，并按事件类型说状态', () => {
  expect(spotOf('entity://demo-electronics/air_system/AIR')).toBe('compressor')
  const entities = buildCampus({
    ...base,
    at: at(10),
    events: [{ at: at(9), entityId: 'entity://demo-electronics/air_system/AIR', eventType: 'load_spike', summary: '总用电突增' }],
  })
  const compressor = entityAt(entities, 'compressor')!
  expect(compressor.tone).toBe('event')
  expect(compressor.value).toBe('事件进行中')
})

it('园区标注遇到未知命令：文字用中文兜底，原命令名放 title', () => {
  const entities = buildCampus({
    ...base,
    actions: [{ entityId: 'entity://demo-electronics/transformer/T1', command: 'custom_cmd', start: 0, end: 60_000 }],
  })
  const lit = entities.find(entity => entity.tone === 'action')!
  expect(lit.value).toBe('执行控制指令')
  expect(lit.title).toBe('custom_cmd')
})

it('空压站任一台 running 为真就显示运行中，与设备顺序无关', () => {
  const site = 'entity://demo-electronics/'
  const devices = {
    [`${site}air_compressor/AC-1`]: { running: false },
    [`${site}air_compressor/AC-2`]: { running: true },
    [`${site}air_system/AIR`]: { pressure_bar: 7.2 },
  }
  const air = buildCampus({ ...base, devices }).find(entity => entity.spot === 'compressor')!
  expect(air.value).toBe('运行中 · 0.72 MPa')
})
