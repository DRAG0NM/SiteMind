import { describeAction } from '../../shared/campus'
import { isSelected } from '../data/evidence'
import { confidenceText, costText, deviceLabel } from '../data/labels'
import { devicesAt } from '../data/selectors'
import { clock, dayEnd, dayStart, time } from '../data/time'
import type { Day, Proposal, TempRule } from '../data/types'
import type { PageProps } from './types'
import './expert.css'

const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`)
const visibleAt = (proposal: Proposal, cursor: number) =>
  !proposal.binding || time(proposal.binding.trigger_at) <= cursor

const isHvac = (id: string) => /hvac|ahu|\/zone\//i.test(id)
const temperatureOf = (device: Record<string, unknown>) => Number(device.temperature_degC ?? device.temperature)
const isHvacRule = (rule: TempRule) => (rule.entity_ids ?? []).some(isHvac)

function Kpi({ label, value, unit, note, accent }: { label: string; value: string; unit: string; note: string; accent?: boolean }) {
  return (
    <div className={'ex-kpi' + (accent ? ' accent' : '')}>
      <span className="ex-kpi-label">{label}</span>
      <span className="ex-kpi-line"><b>{value}</b><span className="ex-kpi-unit">{unit}</span></span>
      <span className="ex-kpi-note">{note}</span>
    </div>
  )
}

function TempChart({ day, cursor, points, rules }: {
  day: Day
  cursor: number
  points: { at: number; value: number }[]
  rules: TempRule[]
}) {
  const width = 960
  const height = 210
  const padL = 34
  const padR = 12
  const padT = 16
  const padB = 24
  const start = dayStart(day)
  const end = dayEnd(day)
  const span = Math.max(1, end - start)
  const values = points.map(point => point.value)
  const min = Math.floor((Math.min(...values) - 0.4) * 10) / 10
  const max = Math.ceil((Math.max(...values) + 0.4) * 10) / 10
  const range = Math.max(0.5, max - min)
  const X = (at: number) => padL + ((at - start) / span) * (width - padL - padR)
  const Y = (value: number) => height - padB - ((value - min) / range) * (height - padT - padB)

  return (
    <svg className="ex-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet"
      role="img" aria-label="行政楼平均室温全天曲线，色带是临时规则生效时段">
      {[0, 0.5, 1].map(ratio => (
        <g key={ratio}>
          <line className="ex-grid" x1={padL} y1={Y(min + range * ratio)} x2={width - padR} y2={Y(min + range * ratio)} />
          <text x={padL - 6} y={Y(min + range * ratio) + 4} textAnchor="end">{(min + range * ratio).toFixed(1)}</text>
        </g>
      ))}
      {rules.map((rule, index) => {
        const x1 = Math.max(padL, X(time(rule.effective.start)))
        const x2 = Math.min(width - padR, X(time(rule.effective.end)))
        if (x2 - x1 < 2) return null
        return (
          <g key={rule.rule_id}>
            <rect className="ex-rule-band" x={x1} y={padT} width={x2 - x1} height={height - padT - padB} />
            {index === rules.length - 1 && <text className="ex-warn" x={x1 + 4} y={padT + 12}>临时规则</text>}
          </g>
        )
      })}
      <line className="ex-axis" x1={padL} y1={height - padB} x2={width - padR} y2={height - padB} />
      <path className="ex-series" d={points.map((point, index) =>
        `${index ? 'L' : 'M'}${X(point.at).toFixed(1)} ${Y(point.value).toFixed(1)}`).join('')} />
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
        <header className="ex-head"><h2>暖通专家这次的判断</h2></header>
        <div className="ex-empty">此刻之前还没有暖通专家的提案。</div>
      </section>
    )
  }
  const actions = proposal.actions ?? []
  const accepted = isSelected(proposal, day, cursor)
  return (
    <section className="ex-panel ex-judge">
      <header className="ex-head">
        <h2>暖通专家这次的判断</h2>
        <span className="ex-meta n">{seconds(proposal.timing?.latency_ms)} · {confidenceText(proposal.confidence)}</span>
      </header>
      <div className="ex-body ex-judge-body">
        <p className="ex-text">{proposal.diagnosis ?? '这条提案没有填写判断说明。'}</p>
        <div className="ex-facts">
          <div className="ex-fact"><span>动作</span><b>{actions.length ? actions.map(action => describeAction({
            entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0,
          })).join('、') : '—'}</b></div>
          <div className="ex-fact"><span>折算</span><b className="n">{costText(proposal.cost.cny)}</b>
            {proposal.cost.comfort_min ? <span className="ex-note">舒适度 -{proposal.cost.comfort_min} 分钟</span> : null}
          </div>
          <div className="ex-fact"><span>裁决结果</span><b className={accepted ? 'or' : ''}>{accepted ? '被采纳' : '未采纳'}</b></div>
        </div>
        <div className="ex-actions">
          <button type="button" className="ex-btn on"
            onClick={() => openEvidence(actions[0]?.entity_id ?? null, '暖通专家判断与依据')}>依据与运行记录</button>
        </div>
      </div>
    </section>
  )
}

export function HvacPage({ day, cursor, openEvidence }: PageProps) {
  const devices = devicesAt(day, cursor)
  const zones = Object.entries(devices).filter(([id]) => isHvac(id))
  const temperatures = zones
    .map(([id, device]) => ({ id, value: temperatureOf(device) }))
    .filter(zone => Number.isFinite(zone.value))
  const adminTemps = temperatures.filter(zone => /ADMIN/i.test(zone.id))
  const average = (list: number[]) => (list.length ? list.reduce((sum, value) => sum + value, 0) / list.length : undefined)
  const averageTemp = average((adminTemps.length ? adminTemps : temperatures).map(zone => zone.value))
  const runningFields = zones.filter(([, device]) => typeof device.running === 'boolean')
  const running = runningFields.filter(([, device]) => device.running === true).length

  const rules = day.temp_rules.filter(rule => isHvacRule(rule) && time(rule.effective.start) <= time(rule.effective.end))
  const activeRules = rules
    .filter(rule => time(rule.effective.start) <= cursor && time(rule.effective.end) > cursor)
    .sort((a, b) => time(a.created_at) - time(b.created_at))
  const rule = activeRules[activeRules.length - 1]
  const covered = new Set(activeRules.flatMap(item => item.entity_ids ?? []))
  const proposals = day.proposals.filter(proposal => proposal.expert === 'hvac' && visibleAt(proposal, cursor))
  const latest = proposals[proposals.length - 1]

  const points = (() => {
    let useAdmin = false
    for (const frame of day.state_series) {
      for (const [id, device] of Object.entries(frame.devices)) {
        if (isHvac(id) && /ADMIN/i.test(id) && Number.isFinite(temperatureOf(device))) useAdmin = true
      }
    }
    const result: { at: number; value: number }[] = []
    for (const frame of day.state_series) {
      const values = Object.entries(frame.devices)
        .filter(([id]) => isHvac(id) && (!useAdmin || /ADMIN/i.test(id)))
        .map(([, device]) => temperatureOf(device))
        .filter(value => Number.isFinite(value))
      if (!values.length) continue
      result.push({ at: time(frame.at), value: values.reduce((sum, value) => sum + value, 0) / values.length })
    }
    return result.sort((a, b) => a.at - b.at)
  })()
  const ruleEvent = rule?.source_event_id ? day.events.find(event => event.event_id === rule.source_event_id) : undefined

  return (
    <div className="ex-page">
      <div className="ex-kpis">
        <Kpi label="行政楼平均室温" value={averageTemp === undefined ? '—' : averageTemp.toFixed(1)} unit="℃"
          note={adminTemps.length ? `行政楼 ${adminTemps.length} 个分区` : zones.length ? `${zones.length} 个暖通分区` : '设备快照里没有温度'}
          accent={Boolean(rule)} />
        <Kpi label="生效临时规则" value={String(activeRules.length)} unit="条"
          note={rule ? `${clock(time(rule.effective.start), day.timezone)}–${clock(time(rule.effective.end), day.timezone)} 生效` : '此刻没有生效规则'}
          accent={activeRules.length > 0} />
        <Kpi label="空调机组运行台数" value={runningFields.length ? String(running) : '—'} unit="台"
          note={runningFields.length ? `${runningFields.length} 台有运行状态记录` : '设备快照里没有运行状态'} />
        <Kpi label="今日专家提案" value={String(proposals.length)} unit="条"
          note={latest ? `最新${confidenceText(latest.confidence)}` : '还没有暖通专家提案'} />
      </div>

      <div className="ex-main">
        <section className="ex-panel">
          <header className="ex-head">
            <h2>分区温度</h2>
            <span className="ex-sub">单位 ℃；橙色边框是临时规则覆盖的区域</span>
            <span className="ex-meta n">快照 {clock(cursor, day.timezone)}</span>
          </header>
          <div className="ex-body">
            {zones.length ? (
              <div className="ex-temp-grid">
                {zones.map(([id, device]) => {
                  const temperature = temperatureOf(device)
                  const supply = Number(device.supply_temp_degC)
                  const humidity = Number(device.humidity)
                  const on = covered.has(id)
                  return (
                    <div className={'ex-temp' + (on ? ' on' : '')} key={id}>
                      <span className="ex-temp-name">{deviceLabel(id)}{on && <span className="ex-rule-tag"> · 临时规则</span>}</span>
                      <span className="ex-temp-value">
                        <b>{Number.isFinite(temperature) ? temperature.toFixed(1) : '—'}</b><span>℃</span>
                      </span>
                      <span className="ex-temp-note">
                        {Number.isFinite(supply) ? `送风 ${supply.toFixed(1)} ℃`
                          : device.running === false ? '停机'
                            : device.running === true ? '运行'
                              : Number.isFinite(humidity) ? (humidity <= 1 ? `湿度 ${humidity}（单位未标注）` : `湿度 ${Math.round(humidity)}%`) : '—'}
                      </span>
                    </div>
                  )
                })}
              </div>
            ) : <div className="ex-empty">此刻还没有暖通分区的温度快照。</div>}
            <div className="ex-divider" />
            <div className="ex-subhead">
              <b>行政楼平均室温 · 全天</b>
              <span>按每次设备快照取点</span>
            </div>
            {points.length ? (
              <>
                <div className="ex-chart-wrap">
                  <TempChart day={day} cursor={cursor} points={points} rules={rules} />
                </div>
                <div className="ex-legend">
                  <span><i className="line" />室温</span>
                  <span><i className="peak" />临时规则生效时段</span>
                </div>
              </>
            ) : <div className="ex-empty">这一天没有可用的空调温度记录。</div>}
          </div>
        </section>

        <div className="ex-col scroll">
          <section className="ex-panel">
            <header className="ex-head">
              <h2>一句话变成临时规则</h2>
              <span className="ex-meta">生效 {activeRules.length} 条</span>
            </header>
            <div className="ex-body ex-judge-body">
              {rule ? (
                <>
                  <div className="ex-bubble-q">{rule.text}</div>
                  <div className="ex-facts two">
                    <div className="ex-fact"><span>生效时段</span>
                      <b className="n">{clock(time(rule.effective.start), day.timezone)}–{clock(time(rule.effective.end), day.timezone)}</b></div>
                    <div className="ex-fact"><span>范围</span>
                      <b>{(rule.entity_ids ?? []).map(deviceLabel).join('、') || '—'}</b></div>
                    <div className="ex-fact"><span>估计多花电费</span>
                      <b className={rule.estimated_cost_cny != null && rule.estimated_cost_cny > 0 ? 'or' : ''}>
                        {rule.estimated_cost_cny != null ? costText(rule.estimated_cost_cny) : '—'}
                      </b></div>
                    <div className="ex-fact"><span>来源</span>
                      <b>{ruleEvent?.summary ?? '—'}</b></div>
                  </div>
                  <div className="ex-actions">
                    <button type="button" className="ex-btn on"
                      onClick={() => openEvidence((rule.entity_ids ?? [])[0] ?? null, '临时规则依据')}>
                      规则依据
                    </button>
                  </div>
                  {activeRules.length > 1 && (
                    <div className="ex-note">
                      另有 {activeRules.length - 1} 条同时生效
                    </div>
                  )}
                </>
              ) : <div className="ex-empty">此刻没有生效的暖通临时规则。把播放头拖到 13:00 之后看这条规则。</div>}
            </div>
          </section>
          <Judgement day={day} cursor={cursor} proposal={latest} openEvidence={openEvidence} />
        </div>
      </div>
    </div>
  )
}
