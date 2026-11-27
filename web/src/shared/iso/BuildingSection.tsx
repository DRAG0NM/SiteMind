import { useEffect } from 'react'
import type { CampusBuilding, CampusEntity } from '../campus'
import { CloseIcon } from '../icons'

/**
 * 建筑剖面：对着正面的 2D 剖面图，线条与配色与轴测图一致。
 * 行政楼画三层和电梯井里的轿厢位置，车间画两层与各层设备；其余建筑画单层设备剖面。
 * 楼层数是建筑属性；轿厢楼层与设备名从回放数据来，缺数据就写 —。
 */
export function BuildingSection({ building, entities, title, carFloor, onClose }: {
  building: CampusBuilding
  entities: CampusEntity[]
  title: string
  carFloor?: number | null
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const value = (spot: CampusEntity['spot']) => entities.find(entity => entity.spot === spot)?.value ?? '—'
  const line = 'var(--line-strong)'
  const thin = 'var(--line)'
  const accent = 'var(--accent)'

  return (
    <div className="iso-section-mask" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}>
      <div className="iso-section" role="dialog" aria-modal="true" aria-label={`${title} 建筑剖面`}>
        <div className="iso-section-head">
          <b>{title} · 建筑剖面</b>
          <span className="iso-section-scale">固定视角 · 每层一台设备示意</span>
          <button type="button" className="btn" style={{ marginLeft: 'auto', height: 30 }} onClick={onClose} aria-label="关闭建筑剖面">
            <CloseIcon size={15} />关闭
          </button>
        </div>
        <div className="iso-section-body">
          {building === 'admin' && <AdminSection line={line} thin={thin} accent={accent} carFloor={carFloor} elevator={value('elevator')} />}
          {building === 'factory' && <FactorySection line={line} thin={thin} accent={accent} hvac={value('hvac')} equipment={value('equipment')} />}
          {building !== 'admin' && building !== 'factory' && (
            <SingleSection line={line} thin={thin} accent={accent} title={title} value={value(building === 'energy' ? 'transformer' : building === 'air' ? 'compressor' : building === 'pv' ? 'pv' : 'cooling')} />
          )}
        </div>
      </div>
    </div>
  )
}

const label = { fill: 'var(--text-2)', fontSize: 12, fontFamily: 'var(--sans)' } as const
const num = { fill: 'var(--text)', fontSize: 13, fontFamily: 'var(--num)', fontWeight: 600 } as const

function AdminSection({ line, thin, accent, carFloor, elevator }: {
  line: string; thin: string; accent: string; carFloor?: number | null; elevator: string
}) {
  const floorHeight = 74
  const baseY = 280
  const car = Math.min(3, Math.max(1, carFloor ?? 1))
  const halls = [
    { name: '办公区', x: 40, w: 250 },
    { name: '会议区', x: 300, w: 90 },
  ]
  const floors = [1, 2, 3]
  return (
    <svg viewBox="0 0 660 320" width="100%" role="img" aria-label="行政楼正面剖面：三层办公与会议区，右侧电梯井内有轿厢">
      <line x1="24" y1={baseY} x2="636" y2={baseY} stroke={line} strokeWidth="2" />
      {floors.map((floor, index) => {
        const y = baseY - (index + 1) * floorHeight
        return (
          <g key={floor}>
            <rect x="40" y={y} width="350" height={floorHeight} fill="none" stroke={thin} />
            {halls.map(hall => (
              <g key={hall.name}>
                <rect x={hall.x} y={y + 8} width={hall.w} height={floorHeight - 16} fill="var(--panel-2)" stroke={thin} />
                <text x={hall.x + 10} y={y + 26} {...label}>{hall.name}</text>
              </g>
            ))}
            {[0, 1, 2, 3].map(column => (
              <line key={column} x1={52 + column * 84} y1={y + 38} x2={52 + column * 84} y2={y + 58} stroke={thin} />
            ))}
            <text x="30" y={y + 42} {...num} textAnchor="end">{floor}F</text>
          </g>
        )
      })}
      {/* 电梯井与轿厢 */}
      <rect x="430" y={baseY - 3 * floorHeight} width="70" height={3 * floorHeight} fill="var(--panel-2)" stroke={line} />
      <text x="465" y={baseY - 3 * floorHeight + 18} {...label} textAnchor="middle">电梯井</text>
      {[1, 2, 3].map(floor => {
        const floorTop = baseY - floor * floorHeight
        const active = car === floor
        return (
          <g key={`car-${floor}`}>
            <line x1="430" y1={floorTop} x2="500" y2={floorTop} stroke={thin} strokeDasharray="3 4" />
            <rect x="440" y={floorTop + 26} width="50" height={floorHeight - 26} fill={active ? 'var(--accent-wash)' : 'none'}
              stroke={active ? accent : thin} strokeDasharray={active ? undefined : '4 4'} />
            {active && <text x="465" y={floorTop + 26 + (floorHeight - 26) / 2 + 4} {...num} textAnchor="middle" fill={accent}>轿厢</text>}
          </g>
        )
      })}
      <text x="500" y={baseY - 3 * floorHeight - 8} {...label} textAnchor="end">{elevator}</text>
      <text x="40" y="306" {...label}>楼层数为建筑属性；轿厢楼层取自回放时刻的设备状态，缺数据停在一层</text>
    </svg>
  )
}

function FactorySection({ line, thin, accent, hvac, equipment }: {
  line: string; thin: string; accent: string; hvac: string; equipment: string
}) {
  const floor1 = 224
  const floor2 = 96
  return (
    <svg viewBox="0 0 700 330" width="100%" role="img" aria-label="生产车间正面剖面：一层产线设备与二层夹层空调机组">
      <line x1="20" y1="290" x2="680" y2="290" stroke={line} strokeWidth="2" />
      <rect x="40" y={floor2} width="560" height={floor1 - floor2} fill="none" stroke={thin} />
      <text x="30" y={floor2 + 18} {...num} textAnchor="end">2F</text>
      <text x="30" y={floor1 - 10} {...num} textAnchor="end">1F</text>
      <line x1="40" y1={floor1} x2="600" y2={floor1} stroke={line} />
      {/* 2F 夹层空调机组 */}
      {[0, 1, 2].map(index => (
        <g key={`ahu-${index}`}>
          <rect x={70 + index * 150} y={floor2 + 20} width={104} height={46} fill="var(--panel-3)" stroke={index === 0 ? accent : thin}
            strokeDasharray={index === 0 ? '4 3' : undefined} />
          <text x={122 + index * 150} y={floor2 + 48} {...label} textAnchor="middle">AHU-{index + 1}</text>
        </g>
      ))}
      <text x="70" y={floor2 - 10} {...label}>夹层：{hvac}</text>
      {/* 冷冻水管 */}
      <line x1="70" y1={floor2 + 6} x2="570" y2={floor2 + 6} stroke="var(--iso-water, #5c7088)" strokeWidth="2" />
      {/* 1F 设备 */}
      {[0, 1, 2, 3].map(index => (
        <g key={`eq-${index}`}>
          <rect x={70 + index * 128} y={floor1 + 22} width={96} height={60} fill="var(--panel-3)" stroke={thin} />
          <line x1={70 + index * 128 + 12} y1={floor1 + 40} x2={70 + index * 128 + 84} y2={floor1 + 40} stroke={thin} />
          <line x1={70 + index * 128 + 12} y1={floor1 + 56} x2={70 + index * 128 + 84} y2={floor1 + 56} stroke={thin} />
          <text x={118 + index * 128} y={floor1 + 98} {...label} textAnchor="middle">产线 {index + 1}</text>
        </g>
      ))}
      <text x="600" y="306" {...label} textAnchor="end">一层设备：{equipment}</text>
    </svg>
  )
}

function SingleSection({ line, thin, accent, title, value }: {
  line: string; thin: string; accent: string; title: string; value: string
}) {
  return (
    <svg viewBox="0 0 620 260" width="100%" role="img" aria-label={`${title}单层设备剖面`}>
      <line x1="20" y1="220" x2="600" y2="220" stroke={line} strokeWidth="2" />
      <rect x="60" y="80" width="220" height="140" fill="var(--panel-2)" stroke={accent} strokeDasharray="4 3" />
      <text x="170" y="120" {...label} textAnchor="middle">{title}</text>
      <text x="170" y="150" {...num} textAnchor="middle">{value}</text>
      {[0, 1, 2].map(index => (
        <rect key={index} x="320" y={120 + index * 34} width="220" height="24" fill="var(--panel-3)" stroke={thin} />
      ))}
      <text x="430" y="112" {...label} textAnchor="middle">柜体 / 储能单元</text>
    </svg>
  )
}

export const BUILDING_TITLE: Record<CampusBuilding, string> = {
  factory: '生产车间',
  admin: '行政楼',
  energy: '能源站',
  air: '空压站',
  pv: '光伏车棚',
  cooling: '冷却塔',
}
