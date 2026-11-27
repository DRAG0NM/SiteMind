import { actionTitle, describeAction } from '../../shared/campus'
import { pv as pvReading, t1 as t1Reading, t1Note } from '../../shared/readings'
import { isSelected } from '../data/evidence'
import { confidenceText, costText, deviceLabel } from '../data/labels'
import { activeActions, config, currentLedgerMinute } from '../data/rollup'
import { devicesAt } from '../data/selectors'
import { clock, dayEnd, dayStart, localHour, time } from '../data/time'
import type { Day, Proposal } from '../data/types'
import type { PageProps } from './types'
import './expert.css'

const EXPERT: Record<string, string> = { energy: '能源', elevator: '电梯', hvac: '暖通', equipment: '设备监测' }
const FLOW_LABEL: Record<string, string> = {
  process: '生产厂房',
  air_compressor: '空压站',
  elevator: '电梯',
  hvac: '暖通',
  lighting: '照明',
  base: '基础负荷',
}
const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`)
const kw = (kwh: number | null | undefined) => (kwh == null ? 0 : Math.round(Number(kwh) * 60))
const visibleAt = (proposal: Proposal, cursor: number) =>
  !proposal.binding || time(proposal.binding.trigger_at) <= cursor

function priceTiers(day: Day) {
  const prices = [...new Set(day.ledger_minutes
    .map(minute => Number(minute.price_cny_per_kwh))
    .filter(value => Number.isFinite(value)))]
    .sort((a, b) => a - b)
  if (!prices.length) return null
  return { valley: prices[0], peak: prices[prices.length - 1], count: prices.length }
}

function phaseOf(price: number, tiers: ReturnType<typeof priceTiers>) {
  if (!tiers || tiers.count < 2) return '—'
  if (price === tiers.peak) return '峰段'
  if (price === tiers.valley) return '谷段'
  return '平段'
}

function Kpi({ label, value, unit, note, accent }: { label: string; value: string; unit: string; note: string; accent?: boolean }) {
  return (
    <div className={'ex-kpi' + (accent ? ' accent' : '')}>
      <span className="ex-kpi-label">{label}</span>
      <span className="ex-kpi-line"><b>{value}</b><span className="ex-kpi-unit">{unit}</span></span>
      <span className="ex-kpi-note">{note}</span>
    </div>
  )
}

type FlowNode = { label: string; value: number; agent?: boolean }

function FlowChart({ loadRatio, sources, branches, discharging }: {
  loadRatio?: number
  sources: FlowNode[]
  branches: FlowNode[]
  discharging: boolean
}) {
  const width = 640
  const height = 320
  const max = Math.max(1, ...sources.map(node => node.value), ...branches.map(node => node.value))
  const lineWidth = (value: number) => 1.5 + (value / max) * 9
  const sourceY = [78, 160, 242]
  const ports = [102, 160, 218]
  const branchY = branches.length > 1
    ? branches.map((_, index) => 44 + (index * 232) / (branches.length - 1))
    : [160]

  return (
    <svg className="ex-flow" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet"
      role="img" aria-label="能量流：市电、光伏、储能经 T1 供给各负荷，线宽表示当前功率">
      <text x="18" y="22">来源</text>
      <text x="500" y="22">去向</text>
      {sources.map((node, index) => {
        const y = sourceY[index] ?? 160
        const port = ports[index] ?? 160
        const agent = discharging && node.agent
        return (
          <g key={node.label}>
            <path className={'ex-wire' + (agent ? ' agent' + (discharging ? ' flow' : '') : '')}
              d={`M126 ${y} C200 ${y} 220 ${port} 292 ${port}`} strokeWidth={lineWidth(node.value)} />
            <text x="118" y={y - 4} textAnchor="end">{node.label}</text>
            <text x="118" y={y + 13} textAnchor="end" className={'ex-flow-val' + (agent ? ' ex-flow-agent' : '')}>
              {node.value} kW
            </text>
          </g>
        )
      })}
      <rect className="ex-t1" x="292" y="64" width="116" height="190" rx="4" />
      <text className="ex-t1-label" x="350" y="140" textAnchor="middle">T1</text>
      <text className="ex-t1-val" x="350" y="170" textAnchor="middle">
        {loadRatio === undefined ? '—' : `${Math.round(loadRatio * 100)}%`}
      </text>
      <text x="350" y="194" textAnchor="middle">负载率</text>
      {branches.map((node, index) => {
        const y = branchY[index] ?? 160
        return (
          <g key={node.label}>
            <path className="ex-wire" d={`M408 160 C448 160 452 ${y} 500 ${y}`} strokeWidth={lineWidth(node.value)} />
            <text x="510" y={y - 4}>{node.label}</text>
            <text x="510" y={y + 13} className="ex-flow-val">{node.value} kW</text>
          </g>
        )
      })}
    </svg>
  )
}

function PriceChart({ day, cursor }: { day: Day; cursor: number }) {
  const width = 960
  const height = 180
  const padL = 30
  const padR = 12
  const padT = 12
  const padB = 24
  const start = dayStart(day)
  const end = dayEnd(day)
  const span = Math.max(1, end - start)
  const tiers = priceTiers(day)
  const bands = Array.from({ length: 24 }, (_, hour) => {
    const prices = day.ledger_minutes
      .filter(minute => localHour(day, time(minute.interval.start)) === hour && minute.price_cny_per_kwh != null)
      .map(minute => Number(minute.price_cny_per_kwh))
    return { hour, price: prices.length ? prices.reduce((sum, value) => sum + value, 0) / prices.length : null }
  })
  const bandWidth = (width - padL - padR) / 24
  const soc = (() => {
    const points: { at: number; value: number }[] = []
    for (const frame of day.state_series) {
      const values = Object.entries(frame.devices)
        .filter(([id]) => /battery|bess/i.test(id))
        .map(([, device]) => Number(device.soc))
        .filter(value => Number.isFinite(value) && value >= 0 && value <= 1)
      if (!values.length) continue
      points.push({ at: time(frame.at), value: values.reduce((sum, value) => sum + value, 0) / values.length })
    }
    return points.sort((a, b) => a.at - b.at)
  })()
  const floor = config.socFloor(day)
  const X = (at: number) => padL + ((at - start) / span) * (width - padL - padR)
  const Y = (value: number) => height - padB - value * (height - padT - padB)
  const colorOf = (price: number | null) => {
    if (price === null || !tiers || tiers.count < 2) return 'ex-band-flat'
    if (price >= tiers.peak) return 'ex-band-peak'
    if (price <= tiers.valley) return 'ex-band-off'
    return 'ex-band-flat'
  }

  return (
    <svg className="ex-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet"
      role="img" aria-label="全天分时电价色带与储能 SOC 曲线">
      {bands.map(band => (
        <rect key={band.hour} className={colorOf(band.price)}
          x={padL + band.hour * bandWidth} y={padT} width={bandWidth + 0.5} height={height - padT - padB} />
      ))}
      {floor !== undefined && (
        <>
          <line className="ex-limit" x1={padL} y1={Y(floor / 100)} x2={width - padR} y2={Y(floor / 100)} />
          <text x={padL + 4} y={Y(floor / 100) - 4} className="ex-warn">SOC 下限 {floor}%</text>
        </>
      )}
      {soc.length > 1 && (
        <path className="ex-series" d={soc.map((point, index) =>
          `${index ? 'L' : 'M'}${X(point.at).toFixed(1)} ${Y(point.value).toFixed(1)}`).join('')} />
      )}
      <line className="ex-cursor" x1={X(cursor)} y1={padT} x2={X(cursor)} y2={height - padB} />
      <text x={padL} y={height - 6}>00:00</text>
      <text x={padL + (width - padL - padR) / 2} y={height - 6} textAnchor="middle">12:00</text>
      <text x={width - padR} y={height - 6} textAnchor="end">24:00</text>
    </svg>
  )
}

function Judgement({ day, cursor, proposal, openEvidence }: {
  day: Day
  cursor: number
  proposal?: Proposal
  openEvidence: PageProps['openEvidence']
}) {
  if (!proposal) {
    return (
      <section className="ex-panel ex-judge">
        <header className="ex-head"><h2>能源专家这次的判断</h2></header>
        <div className="ex-empty">此刻之前还没有能源专家的提案。</div>
      </section>
    )
  }
  const actions = proposal.actions ?? []
  const accepted = isSelected(proposal, day, cursor)
  return (
    <section className="ex-panel ex-judge">
      <header className="ex-head">
        <h2>能源专家这次的判断</h2>
        <span className="ex-meta n">{seconds(proposal.timing?.latency_ms)} · {confidenceText(proposal.confidence)}</span>
      </header>
      <div className="ex-body ex-judge-body">
        <p className="ex-text">{proposal.diagnosis ?? '这条提案没有填写判断说明。'}</p>
        <div className="ex-facts">
          <div className="ex-fact"><span>动作</span><b title={actions.map(action => actionTitle({ entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0 })).filter(Boolean).join('、') || undefined}>{actions.length ? actions.map(action => describeAction({
            entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0,
          })).join('、') : '—'}</b></div>
          <div className="ex-fact"><span>折算</span><b className="n">{costText(proposal.cost.cny)}</b></div>
          <div className="ex-fact"><span>裁决结果</span><b className={accepted ? 'or' : ''}>{accepted ? '被采纳' : '未采纳'}</b></div>
        </div>
        <div className="ex-actions">
          <button type="button" className="ex-btn on"
            onClick={() => openEvidence(actions[0]?.entity_id ?? null, '能源专家判断与依据')}>依据与运行记录</button>
        </div>
      </div>
    </section>
  )
}

export function EnergyPage({ day, cursor, openEvidence }: PageProps) {
  const devices = devicesAt(day, cursor)
  const transformer = Object.entries(devices).find(([id]) => /transformer|t1/i.test(id))
  const battery = Object.entries(devices).find(([id]) => /battery|bess/i.test(id))
  const t1Read = t1Reading(day, cursor)
  const pvRead = pvReading(day, cursor)
  const loadRatio = t1Read.loadPct === null ? undefined : t1Read.loadPct / 100
  const soc = Number(battery?.[1].soc)
  const batteryMode = String(battery?.[1].mode ?? '')
  const minute = currentLedgerMinute(day, cursor)
  const price = minute?.price_cny_per_kwh == null ? undefined : Number(minute.price_cny_per_kwh)
  const tiers = priceTiers(day)
  const floor = config.socFloor(day)
  const proposals = day.proposals.filter(proposal => proposal.expert === 'energy' && visibleAt(proposal, cursor))
  const latest = proposals[proposals.length - 1]
  const discharging = activeActions(day, cursor).some(action => action.command === 'discharge')
  const ledgerDischarge = minute ? minute.discharge_kwh > 0 : false
  const ledgerCharge = minute ? minute.charge_kwh > 0 : false
  const effectiveMode = ledgerDischarge ? 'discharge' : ledgerCharge ? 'charge' : batteryMode
  const modeText = effectiveMode === 'discharge' ? '放电中' : effectiveMode === 'charge' ? '充电中' : effectiveMode === 'idle' ? '待机' : ''

  const sources: FlowNode[] = minute ? [
    { label: '市电', value: kw(minute.grid_import_kwh) },
    { label: '光伏', value: pvRead.kwNow ?? 0 },
    {
      label: minute.discharge_kwh > 0 ? '储能放电' : minute.charge_kwh > 0 ? '储能充电' : '储能待机',
      value: Math.max(kw(minute.discharge_kwh), kw(minute.charge_kwh)),
      agent: minute.discharge_kwh > 0,
    },
  ] : []
  const branches: FlowNode[] = (() => {
    if (!minute) return []
    const loadPower = kw(minute.load_kwh)
    const rows = Object.entries(minute.load_breakdown ?? {})
      .map(([key, value]) => ({ label: FLOW_LABEL[key] ?? key, value: Number(value) }))
      .filter(row => row.value > 0)
    if (!rows.length) return [{ label: '全厂负荷', value: loadPower }]
    // load_breakdown 是同一分钟的构成占比；按当前负荷折成千瓦，避免用不自洽的原始拼接值。
    const total = rows.reduce((sum, row) => sum + row.value, 0)
    const scaled = rows
      .map(row => ({ label: row.label, value: Math.round((row.value / total) * loadPower) }))
      .sort((a, b) => b.value - a.value)
    if (scaled.length > 5) {
      const rest = scaled.slice(4)
      scaled.splice(4, scaled.length - 4, { label: '其他', value: rest.reduce((sum, row) => sum + row.value, 0) })
    }
    return scaled
  })()

  const switches = proposals.flatMap(proposal => (proposal.actions ?? [])
    .filter(action => action.command === 'switch_circuit')
    .map(action => ({
      key: `${action.entity_id}-${action.interval.start}`,
      entityId: action.entity_id,
      action: describeAction({ entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0 }),
      title: actionTitle({ entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0 }),
      window: `${clock(time(action.interval.start), day.timezone)}–${clock(time(action.interval.end), day.timezone)}`,
      accepted: isSelected(proposal, day, cursor),
    })))
  const lightingDevices = Object.entries(devices).filter(([id]) => /lighting|照明|lamp/i.test(id))

  return (
    <div className="ex-page">
      <div className="ex-kpis ex-kpis-5">
        <Kpi label="T1 负载率" value={t1Read.loadPct === null ? '—' : String(t1Read.loadPct)} unit="%"
          note={`${t1Note(t1Read)}${t1Read.limitPct === null ? '' : ` · 上限 ${t1Read.limitPct}%`}`}
          accent={t1Read.state === 'over'} />
        <Kpi label="储能 SOC" value={Number.isFinite(soc) ? String(Math.round(soc * 100)) : '—'} unit="%"
          note={`${modeText || '模式未记录'}${floor === undefined ? '' : ` · 下限 ${floor}%`}`}
          accent={effectiveMode === 'discharge' || (Number.isFinite(soc) && floor !== undefined && soc * 100 <= floor)} />
        {/* 能源页要看当日累计发电；此刻出力是另一个字段，旁注里写明。 */}
        <Kpi label="光伏今日发电" value={pvRead.kwhToday === null ? '—' : pvRead.kwhToday.toFixed(1)} unit="kWh"
          note={pvRead.kwNow === null ? '今日累计 · 此刻出力 —' : `今日累计 · 此刻出力 ${pvRead.kwNow} kW`} />
        <Kpi label="当前电价" value={price === undefined ? '—' : price.toFixed(2)} unit="元/kWh"
          note={price === undefined ? '没有当前电价记录' : `${phaseOf(price, tiers)}${tiers ? ` · 峰 ${tiers.peak.toFixed(2)} / 谷 ${tiers.valley.toFixed(2)}` : ''}`} />
        <Kpi label="今日专家提案" value={String(proposals.length)} unit="条"
          note={latest ? `最新${confidenceText(latest.confidence)}` : '还没有能源专家提案'} />
      </div>

      <div className="ex-main">
        <section className="ex-panel">
          <header className="ex-head">
            <h2>能量流</h2>
            <span className="ex-sub">线宽代表当前功率，蓝色虚线是 Agent 调度的放电路径</span>
            <button type="button" className="ex-btn ex-push" disabled={!transformer}
              onClick={() => openEvidence(transformer?.[0] ?? null, 'T1 负载与储能动作')}>T1 依据</button>
          </header>
          <div className="ex-body">
            {minute ? (
              <>
                <div className="ex-flow-wrap">
                  <FlowChart loadRatio={Number.isFinite(loadRatio) ? loadRatio : undefined} sources={sources}
                    branches={branches} discharging={discharging} />
                </div>
                <div className="ex-note">各去向功率按用电构成占比估算。</div>
                <div className="ex-divider" />
                <div className="ex-subhead">
                  <b>电价与储能 · 全天</b>
                  <span>24 段色带是分时电价，曲线是 SOC</span>
                  <span className="ex-metaclock n">{clock(cursor, day.timezone)}</span>
                </div>
                <div className="ex-price-chart"><PriceChart day={day} cursor={cursor} /></div>
                <div className="ex-legend">
                  <span><i className="peak" />峰段</span>
                  <span><i className="flat" />平段</span>
                  <span><i className="off" />谷段</span>
                  <span><i className="line" />SOC</span>
                  {floor !== undefined && <span><i className="line limit" />SOC 下限 {floor}%</span>}
                </div>
              </>
            ) : <div className="ex-empty">回放头之前还没有用电记录，能量流等第一条功率记录。</div>}
          </div>
        </section>

        <div className="ex-col">
          <Judgement day={day} cursor={cursor} proposal={latest} openEvidence={openEvidence} />
          <section className="ex-panel">
            <header className="ex-head">
              <h2>照明回路</h2>
              <span className="ex-sub">回路切换动作来自能源专家提案</span>
            </header>
            <div className="ex-body flush">
              {switches.length ? (
                <div className="ex-scroll">
                  <table className="ex-table">
                    <thead><tr><th>回路</th><th>动作</th><th>时段</th><th>裁决</th></tr></thead>
                    <tbody>
                      {switches.map(row => (
                        <tr key={row.key} className={row.accepted ? 'on' : ''}>
                          <td><span className="ex-strong">{deviceLabel(row.entityId)}</span></td>
                          <td title={row.title}>{row.action}</td>
                          <td className="n">{row.window}</td>
                          <td><span className={'ex-tag' + (row.accepted ? ' on' : '')}>{row.accepted ? '已采纳' : '未采纳'}</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : lightingDevices.length ? (
                <div className="ex-scroll">
                  <table className="ex-table">
                    <thead><tr><th>回路</th><th>回路 A</th><th>回路 B</th><th>功率</th></tr></thead>
                    <tbody>
                      {lightingDevices.map(([id, device]) => {
                        const circuit = (value: unknown) =>
                          value === 'running' ? '运行' : value === 'tripped' ? '跳闸' : value === 'standby' ? '备用' : '—'
                        return (
                          <tr key={id}>
                            <td><span className="ex-strong">{deviceLabel(id)}</span></td>
                            <td>{circuit(device.circuit_a)}</td>
                            <td>{circuit(device.circuit_b)}</td>
                            <td className="n">{Number.isFinite(Number(device.power_kw)) ? `${Number(device.power_kw).toFixed(1)} kW` : '—'}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              ) : <div className="ex-empty">此刻之前还没有照明回路的切换动作或设备读数。</div>}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

