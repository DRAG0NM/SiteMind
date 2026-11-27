/**
 * 设备/区域 → 楼栋与楼层的共享映射（#181 方案 1）。
 * 2D 厂区态势（B34 App）与 3D 简版（B38 Twin3D）共用；映射表先写死本厂编号，
 * 后续接入 B39 的 site/zones descriptor 时只替换本文件。
 */
export type ZoneBuilding = 'factory' | 'admin' | 'energy' | 'special'

export interface ZoneLocation {
  building: ZoneBuilding
  floor?: number
  label: string
  /** 特殊区域的区域名（洁净车间 / 危化品间 / 数据中心），供界面提示。 */
  zone?: string
}

const SPECIAL_ZONES: Record<string, string> = {
  CLEANROOM: '洁净车间',
  HAZMAT: '危化品间',
  DATACENTER: '数据中心',
}

const admin = (floor: number): ZoneLocation => ({ building: 'admin', floor, label: `行政楼 ${floor}F` })
const adminBuilding = (): ZoneLocation => ({ building: 'admin', label: '行政楼' })
const factory = (): ZoneLocation => ({ building: 'factory', floor: 1, label: '厂房' })
const energy = (): ZoneLocation => ({ building: 'energy', label: '能源站' })
const special = (zone: string): ZoneLocation => ({ building: 'special', label: `特殊区域 · ${zone}`, zone })

/** zone 事件的区域名：独立 `zone:` 前缀，或路径段正好是 `zone`（`hvac_zone` 这类带前缀的路径段不算）。 */
function zoneName(id: string): string | undefined {
  return id.match(/^ZONE:([A-Z0-9_-]+)$/)?.[1] ?? id.match(/(?:^|\/)ZONE\/([A-Z0-9_-]+)(?:\/|$)/)?.[1]
}

/** 设备编号/区域事件 → 楼栋；无法映射返回 null（界面显示“未定位”）。 */
export function locateEntity(entityId: string | null | undefined): ZoneLocation | null {
  if (!entityId) return null
  const id = entityId.trim().toUpperCase()
  if (!id) return null

  // zone 事件：zone:cleanroom 或 entity://…/zone/cleanroom；只有已知区域才映射，其余按未定位
  const zone = zoneName(id)
  if (zone) {
    const specialZone = SPECIAL_ZONES[zone]
    if (specialZone) return special(specialZone)
    if (zone === 'ADMIN') return adminBuilding()
    if (zone === 'PARK' || zone === 'PRODUCTION') return factory()
    return null
  }
  if (/CR-AHU/.test(id)) return special('洁净车间')
  if (/HZ-/.test(id)) return special('危化品间')
  if (/DC-CRAC/.test(id)) return special('数据中心')

  // 行政楼编号：A03→3F、A12→12F
  const lighting = id.match(/LT-A(\d+)/)
  if (lighting) return admin(Number(lighting[1]))
  if (/LT-P/.test(id)) return factory()
  const ahuA = id.match(/AHU-A(\d*)/)
  if (ahuA) return admin(ahuA[1] ? Number(ahuA[1]) : 3)
  if (/AHU-P/.test(id)) return factory()
  // 行政楼冷水机组 CH-A1 在 site.yaml 标 floor: 12（屋顶机房），编号尾号不是楼层，不按尾号猜。
  if (/CH-A1\b/.test(id)) return admin(12)
  // 人梯按 site.yaml 的 label 分区：PE-1/2/3 低区（1～6 层）映射 1F，PE-4/5/6 高区（7～12 层）映射高区起始层 7F；floor 字段仍是 1，楼层依据在 label。
  if (/\bPE-[4-6]\b/.test(id) || /ADMIN_HIGH/.test(id)) return admin(7)
  if (/PASSENGER_ELEVATOR|ADMIN_LOW|\bPE-[1-3]\b/.test(id)) return admin(1)
  if (/FREIGHT_ELEVATOR|EG-PROD|ELEVATOR_GROUP\/FREIGHT|\bFE-\d/.test(id)) return factory()
  const adminFloor = id.match(/ADMIN[-_]?(\d+)F/)
  if (adminFloor) return admin(Number(adminFloor[1]))
  if (/\bADMIN\b/.test(id)) return admin(3)
  if (/COMPRESSOR|AIR_SYSTEM|\bAC-\d|LINE-|PUMP_STATION|\bUTIL-P\d/.test(id)) return factory()
  if (/TRANSFORMER|BATTERY|\/PV(\/|$)|BESS/.test(id)) return energy()
  return null
}

/** 楼栋+楼层在状态集合里的键；无楼层的楼栋（能源站/特殊区域）取 :0。 */
export function locationKey(location: ZoneLocation): string {
  return `${location.building}:${location.floor ?? 0}`
}
