import { describeAction, spotOf } from '../../shared/campus'
import { airDevice, airPressure } from '../../shared/readings'
import { isSelected } from '../data/evidence'
import { confidenceText, costText, deviceLabel, ticketLabel } from '../data/labels'
import { config } from '../data/rollup'
import { devicesAt, latestKnowledge } from '../data/selectors'
import { clock, dayEnd, dayStart, resultVisibleAt, time } from '../data/time'
import type { Day, DayEvent, Proposal, WorkOrder } from '../data/types'
import type { PageProps } from './types'
import './expert.css'

const ORDER_STATUS: Record<string, string> = {
  proposed: '待确认',
  confirmed: '已确认',
  rejected: '已拒绝',
  expired: '已过期',
  done: '已完成',
  cancelled: '已取消',
}
/** 工具调用业务状态的中文名，用于传感器试探过程。 */
const PROBE_STATUS: Record<string, string> = { done: '完成', degraded: '可疑', running: '进行中', failed: '失败', conflict: '冲突' }
/** 真实导出的读数质量是英文文本，展示时换成中文；未收录的取值原样显示，不吞信息。 */
const QUALITY_TEXT: Record<string, string> = {
  good: '可信', ok: '正常', normal: '正常', suspect: '可疑', low: '偏低', poor: '偏差', bad: '异常', degraded: '降级',
}
const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} 秒`)
const visibleAt = (proposal: Proposal, cursor: number) =>
  resultVisibleAt(proposal) <= cursor

const numberField = (device: Record<string, unknown>, keys: string[], digits: number) => {
  for (const key of keys) {
    const value = Number(device[key])
    if (Number.isFinite(value)) return value.toFixed(digits)
  }
  return '—'
}

/** 候选项在真实导出里是 {id,label,…} 对象，内置样例里是字符串；统一取可读文本。 */
function itemText(item: unknown): string {
  if (typeof item === 'string') return item.trim()
  if (typeof item === 'number' && Number.isFinite(item)) return String(item)
  if (item && typeof item === 'object') {
    const record = item as Record<string, unknown>
    for (const key of ['label', 'name', 'title', 'id']) {
      const value = record[key]
      if (typeof value === 'string' && value.trim() !== '') return value.trim()
    }
  }
  return ''
}

/** 候选故障假设：丢弃空项、按 id（无 id 时按名称+序号）生成稳定且唯一的 key。 */
function candidateChips(value: unknown): { key: string; label: string; ids: string[] }[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  const chips: { key: string; label: string; ids: string[] }[] = []
  value.forEach((item, index) => {
    const label = itemText(item)
    if (!label) return
    const record = item && typeof item === 'object' ? item as Record<string, unknown> : null
    const id = record && typeof record['id'] === 'string' ? record['id'].trim() : ''
    const key = id || `${label}#${index}`
    if (seen.has(key)) return
    seen.add(key)
    chips.push({ key, label, ids: id ? [id, label] : [label] })
  })
  return chips
}

/** 字符串或对象混排的文本列表（排除依据、已验证结论），去重后逐条展示。 */
function textList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  const list: string[] = []
  for (const item of value) {
    const label = itemText(item)
    if (!label || seen.has(label)) continue
    seen.add(label)
    list.push(label)
  }
  return list
}

function statusOf(device: Record<string, unknown>) {
  // 真实导出的 fault 是布尔值：只有 true 才是故障，false 表示正常，不能当文本渲染。
  if (device.fault === true) return { text: '故障', note: '', bad: true }
  if (typeof device.fault === 'string' && device.fault.trim() !== '') {
    return { text: '故障', note: device.fault, bad: true }
  }
  if (device.running === false) return { text: '停机', note: '', bad: true }
  if (device.running === true) return { text: '运行', note: '', bad: false }
  const mode = String(device.mode ?? '')
  if (mode === 'discharge') return { text: '放电', note: '', bad: false }
  if (mode === 'charge') return { text: '充电', note: '', bad: false }
  if (mode === 'idle') return { text: '待机', note: '', bad: false }
  return { text: '—', note: '', bad: false }
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

function DualChart({ day, cursor, pressure, current, floor, leakAt }: {
  day: Day
  cursor: number
  pressure: { at: number; value: number }[]
  current: { at: number; value: number }[]
  floor?: number
  leakAt?: number
}) {
  const width = 960
  const height = 240
  const padL = 40
  const padR = 42
  const padT = 16
  const padB = 26
  const start = dayStart(day)
  const end = dayEnd(day)
  const span = Math.max(1, end - start)
  const pressureValues = pressure.map(point => point.value)
  const currentValues = current.map(point => point.value)
  // 真实导出里带压力的 air_system/AIR 没有 current_a，压力与电流可能只有一边有数据；
  // 空序列不能参与 Math.min/max，否则会得到 ±Infinity 并把坐标算成 NaN。
  const hasPressure = pressureValues.length > 0
  const hasCurrent = currentValues.length > 0
  const basePressure = floor ?? 0.8
  const pHigh = (hasPressure ? Math.max(Math.max(...pressureValues), floor ?? -Infinity) : basePressure) + 0.02
  const pLow = (hasPressure ? Math.min(Math.min(...pressureValues), floor ?? Infinity) : basePressure) - 0.02
  const aTop = hasCurrent ? Math.max(...currentValues) : 1
  const aBottom = hasCurrent ? Math.min(...currentValues) : 0
  const aHigh = Math.max(aTop, 1) * 1.08
  const aLow = Math.max(0, Math.min(aBottom, aTop) * 0.85)
  const X = (at: number) => padL + ((at - start) / span) * (width - padL - padR)
  const Yp = (value: number) => height - padB - ((value - pLow) / Math.max(0.01, pHigh - pLow)) * (height - padT - padB)
  const Ya = (value: number) => height - padB - ((value - aLow) / Math.max(0.1, aHigh - aLow)) * (height - padT - padB)
  const path = (points: { at: number; value: number }[], map: (value: number) => number) =>
    points.map((point, index) => `${index ? 'L' : 'M'}${X(point.at).toFixed(1)} ${map(point.value).toFixed(1)}`).join('')

  return (
    <svg className="ex-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet"
      role="img" aria-label="空压机管网压力与主机电流双曲线">
      {[0, 0.5, 1].map(ratio => (
        <g key={ratio}>
          <line className="ex-grid" x1={padL} y1={padT + (height - padT - padB) * ratio}
            x2={width - padR} y2={padT + (height - padT - padB) * ratio} />
          <text x={padL - 6} y={padT + (height - padT - padB) * ratio + 4} textAnchor="end">
            {(pHigh - (pHigh - pLow) * ratio).toFixed(2)}
          </text>
          <text x={width - padR + 6} y={padT + (height - padT - padB) * ratio + 4}>
            {Math.round(aHigh - (aHigh - aLow) * ratio)}
          </text>
        </g>
      ))}
      <line className="ex-axis" x1={padL} y1={height - padB} x2={width - padR} y2={height - padB} />
      {floor !== undefined && (
        <>
          <line className="ex-limit" x1={padL} y1={Yp(floor)} x2={width - padR} y2={Yp(floor)} />
          <text className="ex-warn" x={width - padR} y={Yp(floor) - 4} textAnchor="end">压力下限 {floor.toFixed(2)}</text>
        </>
      )}
      {leakAt !== undefined && (
        <>
          <line className="ex-limit" x1={X(leakAt)} y1={padT} x2={X(leakAt)} y2={height - padB} />
          <text className="ex-warn" x={X(leakAt) + 4} y={padT + 10}>泄漏告警</text>
        </>
      )}
      {hasCurrent && <path className="ex-series dash" d={path(current, Ya)} />}
      {hasPressure && <path className="ex-series" d={path(pressure, Yp)} />}
      <line className="ex-cursor" x1={X(cursor)} y1={padT} x2={X(cursor)} y2={height - padB} />
      <text x={padL} y={height - 6}>00:00</text>
      <text x={padL + (width - padL - padR) / 2} y={height - 6} textAnchor="middle">12:00</text>
      <text x={width - padR} y={height - 6} textAnchor="end">24:00</text>
    </svg>
  )
}

/** 带读数质量 / 残差 / 判读结论的传感器事件，按时间排好；没有就返回空。 */
function sensorEpisodes(day: Day, cursor: number) {
  return day.events
    .filter(event => time(event.at) <= cursor && event.payload != null
      && (event.payload['quality'] != null || event.payload['residual_c'] != null || event.payload['conclusion'] != null))
    .sort((a, b) => time(a.at) - time(b.at))
}

/** 试探过程：用传感器事件所属事件单的提案，找到运行记录里的工具调用。 */
function probeSteps(day: Day, event: DayEvent, cursor: number) {
  const ticket = day.tickets.find(item => (item.event_ids ?? []).includes(event.event_id))
  const proposalIds = new Set(day.proposals
    .filter(proposal => ticket && proposal.ticket_id === ticket.ticket_id && proposal.proposal_id)
    .map(proposal => proposal.proposal_id as string))
  // 工具明细的 started_at 可能是墙钟，不能逐条和回放头比较；整次运行按仿真生效时刻裁剪。
  const run = ticket ? day.agent_runs.find(item => (item.proposal_ids ?? []).some(id => proposalIds.has(id))
    && resultVisibleAt(item) <= cursor) : undefined
  // 触发时刻取场景时刻（binding.trigger_at），墙钟 started_at 不上屏，与证据抽屉一致。
  const trigger = run?.binding?.trigger_at ? clock(time(run.binding.trigger_at), day.timezone) : '—'
  return { trigger, calls: run?.tool_calls ?? [] }
}

function SensorQuality({ day, cursor, openEvidence }: {
  day: Day
  cursor: number
  openEvidence: PageProps['openEvidence']
}) {
  const episodes = sensorEpisodes(day, cursor)
  const latest = episodes[episodes.length - 1]
  // 读数与判读可能分两条事件：读数超限那条带质量分与候选假设，判读那条带结论。
  const alarm = [...episodes].reverse().find(item => item.payload?.['quality'] != null || item.payload?.['residual_c'] != null)
  if (!latest) {
    return (
      <section className="ex-panel ex-sensor">
        <header className="ex-head">
          <h2>传感器读数质量</h2>
          <span className="ex-sub">读数质量分 · 残差报警 · 候选故障假设</span>
        </header>
        <div className="ex-empty">
          当前时刻暂无传感器读数异常事件；演示场景中的探头漂移排查事件位于 15:10。
        </div>
      </section>
    )
  }
  const source = alarm ?? latest
  const payload = source.payload ?? {}
  const sourceTickets = day.tickets.filter(ticket => (ticket.event_ids ?? []).includes(source.event_id))
  const resolved = episodes
    .filter(item => item.payload?.['conclusion'] != null && time(item.at) >= time(source.at)
      && (item.event_id === source.event_id || sourceTickets.some(ticket => (ticket.event_ids ?? []).includes(item.event_id)))
      && (!source.entity_id || !item.entity_id || source.entity_id === item.entity_id)
      && (!payload['sensor'] || !item.payload?.['sensor'] || payload['sensor'] === item.payload['sensor']))
    .map(item => item.payload ?? {})
    .pop() ?? {}
  const candidates = candidateChips(payload['candidates'])
  const excluded = new Set(textList(resolved['excluded']))
  const verified = textList(resolved['verified_by'])
  const { trigger: probeTrigger, calls: steps } = probeSteps(day, source, cursor)
  const raw = (key: string) => payload[key]
  const text = (key: string) => (raw(key) == null ? '—' : String(raw(key)))
  // quality 在真实导出里是文本（suspect），在内置样例里是数值（0.42）；两种都要能显示，也都要能判低。
  const qualityRaw = raw('quality')
  const qualityNumber = typeof qualityRaw === 'number' ? qualityRaw
    : typeof qualityRaw === 'string' && qualityRaw.trim() !== '' ? Number(qualityRaw) : Number.NaN
  const qualitySuspicious = typeof qualityRaw === 'string' && /^(suspect|low|bad|poor|degraded)$/i.test(qualityRaw.trim())
  const qualityLow = (Number.isFinite(qualityNumber) && qualityNumber < 0.6) || qualitySuspicious
  // 提示只在真的偏低时出现：0.9 这类正常数值不能被标成「低于 0.6」。
  const qualityNote = Number.isFinite(qualityNumber) && qualityNumber < 0.6 ? ' · 低于 0.6' : ''
  const qualityText = qualityRaw == null || qualityRaw === '' ? '—'
    : typeof qualityRaw === 'string' ? (QUALITY_TEXT[qualityRaw.trim().toLowerCase()] ?? String(qualityRaw))
      : String(qualityRaw)
  const conclusion = resolved['conclusion'] === 'sensor_lie' ? '判定为传感器读数异常（探头漂移）' : resolved['conclusion'] != null ? String(resolved['conclusion']) : null

  return (
    <section className="ex-panel ex-sensor">
      <header className="ex-head">
        <h2>传感器读数质量</h2>
        <span className="ex-sub">先校验读数可靠性，再决定调度动作</span>
        <span className="ex-meta n">{clock(time(source.at), day.timezone)}</span>
      </header>
      <div className="ex-body">
        <div className="ex-facts two">
          <div className="ex-fact"><span>测点</span><b>{text('sensor')}</b></div>
          <div className="ex-fact"><span>读数 / 基线</span><b className="n">{text('reading')} / {text('baseline')}</b></div>
          <div className="ex-fact"><span>残差 / 限值</span><b className="n">{text('residual_c')} ℃ / {text('residual_limit_c')} ℃</b></div>
          <div className="ex-fact">
            <span>读数质量</span>
            <b className={qualityLow ? 'or' : ''}>{qualityText}{qualityNote}</b>
          </div>
        </div>

        {candidates.length > 0 && (
          <div className="ex-candidates">
            <span className="ex-label">候选故障假设</span>
            <div className="ex-chips">
              {candidates.map(candidate => {
                const off = candidate.ids.some(id => excluded.has(id))
                return (
                  <span className={'ex-chip' + (off ? ' off' : '')} key={candidate.key}>
                    {off ? '已排除 · ' : ''}{candidate.label}
                  </span>
                )
              })}
            </div>
          </div>
        )}

        {verified.length > 0 && (
          <div className="ex-candidates">
            <span className="ex-label">排除依据</span>
            <ul className="ex-verify">
              {verified.map(item => <li key={item}>{item}</li>)}
            </ul>
          </div>
        )}

        {steps.length > 0 && (
          <div className="ex-probe">
            <span className="ex-label">Agent 试探过程</span>
            {steps.map(step => (
              <div className="ex-probe-row" key={step.call_id}>
                <div className="ex-probe-head">
                  <b>{step.tool}</b>
                  <span className="ex-meta n">
                    触发 {probeTrigger} · 耗时 {seconds(step.duration_ms)}
                  </span>
                  <span className={'ex-tag' + (step.business_status === 'degraded' ? ' on' : '')}>
                    {PROBE_STATUS[step.business_status ?? ''] ?? step.business_status ?? '—'}
                  </span>
                </div>
                <p className="ex-text">{step.summary ?? '这一步没有记录摘要。'}</p>
              </div>
            ))}
          </div>
        )}

        {conclusion && <p className="ex-note">判读结论：{conclusion}。{resolved['resolution'] != null ? String(resolved['resolution']) : ''}</p>}
        <div className="ex-actions">
          <button type="button" className="ex-btn on"
            onClick={() => openEvidence(source.entity_id ?? null, '传感器读数质量与判读')}>依据与运行记录</button>
        </div>
      </div>
    </section>
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
        <header className="ex-head"><h2>设备监测专家这次的判断</h2></header>
        <div className="ex-empty">此刻之前还没有设备监测专家的提案。</div>
      </section>
    )
  }
  const actions = proposal.actions ?? []
  const accepted = isSelected(proposal, day, cursor)
  return (
    <section className="ex-panel ex-judge">
      <header className="ex-head">
        <h2>设备监测专家这次的判断</h2>
        <span className="ex-meta n">{seconds(proposal.timing?.latency_ms)} · {confidenceText(proposal.confidence)}</span>
      </header>
      <div className="ex-body ex-judge-body">
        <p className="ex-text">{proposal.diagnosis ?? '这条提案没有填写判断说明。'}</p>
        <div className="ex-facts">
          <div className="ex-fact"><span>动作</span><b>{actions.length ? actions.map(action => describeAction({
            entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0,
          })).join('、') : '—'}</b></div>
          <div className="ex-fact"><span>折算</span><b className="n">{costText(proposal.cost.cny)}</b></div>
          <div className="ex-fact"><span>裁决结果</span><b className={accepted ? 'or' : ''}>{accepted ? '被采纳' : '未采纳'}</b></div>
        </div>
        <div className="ex-actions">
          <button type="button" className="ex-btn on"
            onClick={() => openEvidence(actions[0]?.entity_id ?? null, '设备监测专家判断与依据')}>依据与运行记录</button>
        </div>
      </div>
    </section>
  )
}

export function EquipmentPage({ day, cursor, openEvidence }: PageProps) {
  const devices = devicesAt(day, cursor)
  const compressor = airDevice(devices, id => spotOf(id) === 'compressor')
  const pressure: { at: number; value: number }[] = []
  const current: { at: number; value: number }[] = []
  for (const frame of day.state_series) {
    const machine = airDevice(frame.devices, id => spotOf(id) === 'compressor')?.[1]
    if (!machine) continue
    const p = airPressure(machine) ?? Number.NaN
    const a = Number(machine.current_a)
    if (Number.isFinite(p)) pressure.push({ at: time(frame.at), value: p })
    if (Number.isFinite(a)) current.push({ at: time(frame.at), value: a })
  }
  pressure.sort((a, b) => a.at - b.at)
  current.sort((a, b) => a.at - b.at)
  const leakEvent = day.events
    .filter(event => time(event.at) <= cursor && event.payload?.['pressure_drop_mpa_per_min'] != null)
    .reverse()[0]
  const eventRate = leakEvent ? Number(leakEvent.payload?.['pressure_drop_mpa_per_min']) : undefined
  const pastPressure = pressure.filter(point => point.at <= cursor)
  const frameRate = (() => {
    const latest = pastPressure[pastPressure.length - 1]
    const previous = pastPressure[pastPressure.length - 2]
    if (!latest || !previous) return undefined
    const minutes = (latest.at - previous.at) / 60_000
    if (!(minutes > 0)) return undefined
    return (previous.value - latest.value) / minutes
  })()
  const dropRate = Number.isFinite(eventRate) && eventRate !== undefined
    ? { value: eventRate, note: `${clock(time(leakEvent.at), day.timezone)} 告警里的压降速率` }
    : frameRate !== undefined ? { value: frameRate, note: '按最近两帧压力换算' } : undefined
  const floor = config.airPressureFloor(day)
  const leakThreshold = config.airLeakThreshold(day)
  const knowledge = latestKnowledge(day, cursor)
  const proposals = day.proposals.filter(proposal => proposal.expert === 'equipment' && visibleAt(proposal, cursor))
  const latest = proposals[proposals.length - 1]

  // 只列有测点的设备：照明、光伏这类没有压力/电流/振动/温度读数的设备不进来，避免整表都是 —。
  const deviceRows = Object.entries(devices)
    .map(([id, device]) => ({
      id,
      pressure: airPressure(device) === null ? '—' : (airPressure(device) as number).toFixed(2),
      current: numberField(device, ['current_a'], 1),
      vibration: numberField(device, ['vibration_mm_s', 'vibration'], 2),
      temperature: numberField(device, ['temperature_degC'], 1),
      state: statusOf(device),
    }))
    .filter(row => row.pressure !== '—' || row.current !== '—' || row.vibration !== '—' || row.temperature !== '—')

  const equipmentTickets = new Set(day.tickets
    .filter(ticket => (ticket.experts ?? []).includes('equipment')
      || (ticket.event_ids ?? []).some(eventId => {
        const event = day.events.find(item => item.event_id === eventId)
        return event ? /equipment|air_pressure|vibration|compound/i.test(event.event_type) : false
      }))
    .map(ticket => ticket.ticket_id))
  const orders: WorkOrder[] = day.work_orders
    .filter(order => time(order.created_at) <= cursor && equipmentTickets.has(order.ticket_id))
    .sort((a, b) => time(a.created_at) - time(b.created_at))
  const orderProposal = proposals
    .filter(proposal => (proposal.actions ?? []).some(action => action.command === 'open_work_order'))
    .reverse()[0]
  const orderProposalAt = orderProposal ? resultVisibleAt(orderProposal) : NaN
  const leakAt = leakEvent ? time(leakEvent.at) : undefined

  return (
    <div className="ex-page">
      <div className="ex-kpis">
        <Kpi label="压降速率" value={dropRate ? String(Number(dropRate.value.toFixed(3))) : '—'} unit="MPa/min"
          note={dropRate?.note ?? '没有压力记录或告警数据'} accent={Boolean(dropRate && dropRate.value > 0)} />
        <Kpi label="判别阈值" value={leakThreshold === undefined ? '—' : String(leakThreshold)} unit="MPa/min"
          note={leakThreshold === undefined ? '没有配置判别阈值' : `配置项 · 与知识版本一起生效`} />
        <Kpi label="在线设备" value={String(deviceRows.length)} unit="台" note="当前接入的设备" />
        <Kpi label="今日维修工单" value={String(orders.length)} unit="张"
          note={orders.length ? `${ORDER_STATUS[orders[0].status] ?? orders[0].status}` : '还没有与设备监测相关的工单'} />
      </div>

      <div className="ex-main">
        <div className="ex-equipment-left">
          <section className="ex-panel">
            <header className="ex-head">
              <h2>{compressor ? deviceLabel(compressor[0]) : '空压机'} · 压力与电流</h2>
              <span className="ex-sub">压力与电流一起看，按每次设备快照取点</span>
              <span className="ex-meta n">{clock(cursor, day.timezone)}</span>
            </header>
            <div className="ex-body">
              {pressure.length || current.length ? (
                <>
                  <div className="ex-chart-wrap">
                    <DualChart day={day} cursor={cursor} pressure={pressure} current={current} floor={floor} leakAt={leakAt} />
                  </div>
                  <div className="ex-legend">
                    <span><i className="line" />管网压力</span>
                    <span><i className="line dash" />主机电流</span>
                    {floor !== undefined && <span><i className="line limit" />压力下限 {floor.toFixed(2)} MPa</span>}
                  </div>
                </>
              ) : <div className="ex-empty">这一天没有空压机的压力和电流记录。</div>}
            </div>
          </section>

          <section className="ex-panel">
            <header className="ex-head">
              <h2>全厂设备健康</h2>
              <span className="ex-sub">缺数据显示 —</span>
            </header>
            <div className="ex-body flush">
              {deviceRows.length ? (
                <div className="ex-scroll">
                  <table className="ex-table">
                    <thead><tr><th>设备</th><th>压力</th><th>电流</th><th>振动</th><th>温度</th><th>状态</th></tr></thead>
                    <tbody>
                      {deviceRows.map(row => (
                        <tr key={row.id}>
                          <td><span className="ex-strong">{deviceLabel(row.id)}</span></td>
                          <td className="n">{row.pressure}</td>
                          <td className="n">{row.current}</td>
                          <td className="n">{row.vibration}</td>
                          <td className="n">{row.temperature}</td>
                          <td>
                            <span className={'ex-tag' + (row.state.bad ? ' on' : '')} title={row.state.note || undefined}>
                              {row.state.text}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <div className="ex-empty">回放头之前还没有设备快照。</div>}
            </div>
          </section>
        </div>

        <div className="ex-col scroll">
          <SensorQuality day={day} cursor={cursor} openEvidence={openEvidence} />
          <Judgement day={day} cursor={cursor} proposal={latest} openEvidence={openEvidence} />

          <section className="ex-panel">
            <header className="ex-head">
              <h2>用上了今天学到的规则</h2>
              <span className="ex-sub">最新生效的知识版本</span>
            </header>
            <div className="ex-body">
              {knowledge ? (
                <>
                  <div className="ex-know-head">
                    <span className="ex-tag on">知识 v{knowledge.version}</span>
                    <span className="ex-note n">{clock(time(knowledge.activated_at), day.timezone)} 生效</span>
                  </div>
                  <p className="ex-text">{knowledge.summary ?? '这个版本没有记录摘要。'}</p>
                  <div className="ex-actions">
                    <button type="button" className="ex-btn"
                      onClick={() => openEvidence(null, `知识 v${knowledge.version}`)}>查看依据</button>
                  </div>
                </>
              ) : <div className="ex-empty">此刻之前还没有生效的知识版本。</div>}
            </div>
          </section>

          <section className="ex-panel">
            <header className="ex-head">
              <h2>维修工单</h2>
              <span className="ex-meta">{orders.length} 张</span>
            </header>
            <div className="ex-body">
              {orders.length ? orders.map(order => (
                <div className="ex-order" key={order.work_order_id} title={`工单号 ${order.work_order_id}`}>
                  <div className="ex-order-head">
                    <b>维修工单</b>
                    <span className={'ex-tag' + (order.status === 'proposed' ? ' on' : '')}>
                      {ORDER_STATUS[order.status] ?? order.status}
                    </span>
                  </div>
                  <p className="ex-text">{order.reason}</p>
                  <div className="ex-order-meta">
                    <span>创建 {clock(time(order.created_at), day.timezone)}</span>
                    <span>{ticketLabel(order.ticket_id)}</span>
                  </div>
                  <div className="ex-actions">
                    <button type="button" className="ex-btn"
                      onClick={() => openEvidence((order.actions ?? [])[0]?.entity_id ?? null, '维修工单详情')}>
                      工单详情
                    </button>
                  </div>
                </div>
              )) : (
                <div className="ex-empty">
                  {orderProposal
                    ? `设备监测专家在 ${Number.isFinite(orderProposalAt) ? clock(orderProposalAt, day.timezone) : '—'} 的提案请求开维修工单，但没有对应的工单记录。`
                    : '此刻还没有与设备监测相关的维修工单。'}
                </div>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

