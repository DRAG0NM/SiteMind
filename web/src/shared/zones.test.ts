import { expect, it } from 'vitest'
import { OUTCOME_LABELS, outcomeLabel, ticketStatusLabel, wallOutcomeLabel, workOrderStatusLabel } from './labels'
import { locateEntity } from './zones'

const entity = (path: string) => `entity://demo-electronics/${path}`

it('设备编号按共享映射表定位到楼栋与楼层', () => {
  expect(locateEntity(entity('lighting/LT-P'))).toMatchObject({ building: 'factory' })
  expect(locateEntity(entity('hvac/AHU-P1'))).toMatchObject({ building: 'factory' })
  expect(locateEntity(entity('air_compressor/AC-2'))).toMatchObject({ building: 'factory' })
  expect(locateEntity(entity('production/LINE-3'))).toMatchObject({ building: 'factory' })
  expect(locateEntity(entity('lighting/LT-A03'))).toMatchObject({ building: 'admin', floor: 3 })
  expect(locateEntity(entity('lighting/LT-A12'))).toMatchObject({ building: 'admin', floor: 12 })
  expect(locateEntity(entity('hvac/AHU-A1'))).toMatchObject({ building: 'admin', floor: 1 })
  expect(locateEntity(entity('elevator/passenger_elevator/PE-1'))).toMatchObject({ building: 'admin', floor: 1 })
  expect(locateEntity(entity('elevator/passenger_elevator/PE-4'))).toMatchObject({ building: 'admin', floor: 7 })
  expect(locateEntity(entity('elevator_group/admin_low'))).toMatchObject({ building: 'admin', floor: 1 })
  expect(locateEntity(entity('elevator_group/admin_high'))).toMatchObject({ building: 'admin', floor: 7 })
  expect(locateEntity(entity('chiller/CH-A1'))).toMatchObject({ building: 'admin', floor: 12 })
  expect(locateEntity(entity('pump_station/UTIL-P1'))).toMatchObject({ building: 'factory' })
  expect(locateEntity(entity('elevator/freight_elevator/FE-2'))).toMatchObject({ building: 'factory' })
  expect(locateEntity(entity('hvac/CR-AHU-1'))).toMatchObject({ building: 'special' })
  expect(locateEntity(entity('hazmat/HZ-1'))).toMatchObject({ building: 'special' })
  expect(locateEntity(entity('datacenter/DC-CRAC1'))).toMatchObject({ building: 'special' })
  expect(locateEntity(entity('transformer/T1'))).toMatchObject({ building: 'energy' })
})

it('zone 事件定位到特殊区域或对应楼栋', () => {
  expect(locateEntity('zone:cleanroom')).toMatchObject({ building: 'special', zone: '洁净车间' })
  expect(locateEntity(entity('zone/cleanroom'))).toMatchObject({ building: 'special', zone: '洁净车间' })
  expect(locateEntity('zone:admin')).toMatchObject({ building: 'admin' })
  expect(locateEntity('zone:park')).toMatchObject({ building: 'factory' })
})

it('hvac_zone 路径段不是 zone 事件，ADMIN-3F 按行政楼楼层映射', () => {
  expect(locateEntity(entity('hvac_zone/ADMIN-3F'))).toMatchObject({ building: 'admin', floor: 3 })
})

it('无法映射的实体返回 null，由界面显示未定位', () => {
  expect(locateEntity(entity('unknown/FD-9'))).toBeNull()
  expect(locateEntity('zone:unknown')).toBeNull()
  expect(locateEntity(null)).toBeNull()
})

it('裁决 outcome、事件单与操作工单状态映射与契约一致', () => {
  expect(Object.keys(OUTCOME_LABELS).sort()).toEqual(['blocked', 'executed', 'sampling', 'to_human', 'work_order'])
  expect(outcomeLabel('work_order')).toBe('转人工')
  expect(outcomeLabel('to_human')).toBe('转人工')
  expect(outcomeLabel('sampling')).toBe('抽查待确认')
  expect(wallOutcomeLabel('executed')).toBe('已自动处置')
  expect(wallOutcomeLabel('blocked')).toBe('已知会值班')
  expect(wallOutcomeLabel('unknown')).toBe('已处理')
  expect(ticketStatusLabel('open')).toBe('进行中')
  expect(ticketStatusLabel('waiting')).toBe('待确认')
  expect(ticketStatusLabel('blocked')).toBe('受阻')
  expect(ticketStatusLabel('resolved')).toBe('已解决')
  expect(ticketStatusLabel('closed')).toBe('已关闭')
  expect(workOrderStatusLabel('proposed')).toBe('待确认')
  expect(workOrderStatusLabel('expired')).toBe('已过期')
  expect(workOrderStatusLabel('done')).toBe('已完成')
  // 事件单与操作工单是两套状态，各查各的，不互相兜底。
  expect(ticketStatusLabel('proposed')).toBe('proposed')
  expect(workOrderStatusLabel('waiting')).toBe('waiting')
})
