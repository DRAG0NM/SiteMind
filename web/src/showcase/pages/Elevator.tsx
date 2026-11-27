import { describeAction } from '../../shared/campus'
import { isSelected } from '../data/evidence'
import { confidenceText, costText, deviceLabel } from '../data/labels'
import { devicesAt, metricValue } from '../data/selectors'
import { clock, dayEnd, dayStart, time } from '../data/time'
import type { Day, Proposal } from '../data/types'
import type { PageProps } from './types'
import './expert.css'

const EVENT_LABEL: Record<string, string> = { elevator_peak: '电梯高峰', elevator_fault: '电梯故障' }
const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`)

const visibleAt = (proposal: Proposal, cursor: number) =>
  !proposal.binding || time(proposal.binding.trigger_at) <= cursor

const numberField = (device: Record<string, unknown> | undefined, key: string) => {
  const value = Number(device?.[key])
  return Number.isFinite(value) ? value : undefined
}

/** 真实导出的 fault 是布尔值：只有 true 是故障，false 表示正常；旧样例用非空文本描述故障。 */
function faultState(device: Record<string, unknown>) {
  if (device.fault === true) return { active: true, note: '' }
  if (typeof device.fault === 'string' && device.fault.trim() !== '') {
    return { active: true, note: device.fault }
  }
  return { active: false, note: '' }
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

function Judgement({ day, cursor, proposal, openEvidence }: {
  day: Day
  cursor: number
  proposal?: Proposal
  openEvidence: PageProps['openEvidence']
}) {
  if (!proposal) {
    return (
      <section className="ex-panel ex-judge">
        <header className="ex-head"><h2>电梯专家这次的判断</h2></header>
        <div className="ex-empty">此刻之前还没有电梯专家的提案。</div>
      </section>
    )
  }
  const actions = proposal.actions ?? []
  const accepted = isSelected(proposal, day, cursor)
  const extra = [
    proposal.cost.wait_min ? `候梯 +${proposal.cost.wait_min} 分钟` : '',
    proposal.cost.comfort_min ? `舒适度 -${proposal.cost.comfort_min} 分钟` : '',
  ].filter(Boolean).join(' · ')
  return (
    <section className="ex-panel ex-judge">
      <header className="ex-head">
        <h2>电梯专家这次的判断</h2>
        <span className="ex-meta n">{seconds(proposal.timing?.latency_ms)} · {confidenceText(proposal.confidence)}</span>
      </header>
      <div className="ex-body ex-judge-body">
        <p className="ex-text">{proposal.diagnosis ?? '这条提案没有填写判断说明。'}</p>
        <div className="ex-facts">
          <div className="ex-fact"><span>动作</span><b>{actions.length ? actions.map(action => describeAction({
            entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0,
          })).join('、') : '—'}</b></div>
          <div className="ex-fact"><span>折算</span><b className="n">{costText(proposal.cost.cny)}</b>{extra && <span className="ex-note">{extra}</span>}</div>
          <div className="ex-fact"><span>裁决结果</span><b className={accepted ? 'or' : ''}>{accepted ? '被采纳' : '未采纳'}</b></div>
        </div>
        <div className="ex-actions">
          <button type="button" className="ex-btn on"
            onClick={() => openEvidence(actions[0]?.entity_id ?? null, '电梯专家判断与依据')}>依据与运行记录</button>
        </div>
      </div>
    </section>
  )
}

function QueueChart({ day, cursor, series }: {
  day: Day
  cursor: number
  series: { id: string; points: { at: number; value: number }[] }[]
}) {
  const width = 960
  const height = 210
  const padL = 32
  const padR = 12
  const padT = 14
  const padB = 26
  const start = dayStart(day)
  const end = dayEnd(day)
  const span = Math.max(1, end - start)
  const maxValue = Math.max(10, ...series.flatMap(group => group.points.map(point => point.value)))
  const max = Math.ceil(maxValue / 10) * 10
  const X = (at: number) => padL + ((at - start) / span) * (width - padL - padR)
  const Y = (value: number) => height - padB - (value / max) * (height - padT - padB)
  const style = ['', ' dash', ' soft']

  return (
    <svg className="ex-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet"
      role="img" aria-label="各梯群全天候梯人数">
      {[0, 0.5, 1].map(ratio => (
        <g key={ratio}>
          <line className="ex-grid" x1={padL} y1={Y(max * ratio)} x2={width - padR} y2={Y(max * ratio)} />
          <text x={padL - 6} y={Y(max * ratio) + 4} textAnchor="end">{Math.round(max * ratio)}</text>
        </g>
      ))}
      <line className="ex-axis" x1={padL} y1={height - padB} x2={width - padR} y2={height - padB} />
      {series.map((group, index) => (
        <path key={group.id} className={'ex-series' + (style[index % style.length] ?? '')}
          d={group.points.map((point, order) => `${order ? 'L' : 'M'}${X(point.at).toFixed(1)} ${Y(point.value).toFixed(1)}`).join('')} />
      ))}
      <line className="ex-cursor" x1={X(cursor)} y1={padT} x2={X(cursor)} y2={height - padB} />
      <text x={padL} y={height - 6}>00:00</text>
      <text x={X(start + span / 2)} y={height - 6} textAnchor="middle">12:00</text>
      <text x={width - padR} y={height - 6} textAnchor="end">24:00</text>
    </svg>
  )
}

export function ElevatorPage({ day, cursor, openEvidence }: PageProps) {
  const devices = devicesAt(day, cursor)
  const groups = Object.entries(devices).filter(([id]) => /elevator/i.test(id))
  const adminGroup = groups.find(([id]) => /ADMIN/i.test(id))
  const queue = numberField(adminGroup?.[1], 'queue') ?? groups.map(([, device]) => numberField(device, 'queue')).find(value => value !== undefined)
  const running = groups.reduce((sum, [, device]) => sum + (numberField(device, 'running') ?? 0), 0)
  const runningKnown = groups.some(([, device]) => numberField(device, 'running') !== undefined)
  const averageWait = metricValue(day, 'avg_wait_min', 'coordinated')
  const proposals = day.proposals.filter(proposal => proposal.expert === 'elevator' && visibleAt(proposal, cursor))
  const latest = proposals[proposals.length - 1]
  const maxQueue = Math.max(1, ...groups.map(([, device]) => numberField(device, 'queue') ?? 0))

  const events = day.events
    .filter(event => time(event.at) <= cursor && /elevator/i.test(event.event_type))
    .sort((a, b) => time(b.at) - time(a.at))

  const series = (() => {
    const grouped = new Map<string, { at: number; value: number }[]>()
    for (const frame of day.state_series) {
      for (const [id, device] of Object.entries(frame.devices)) {
        if (!/elevator/i.test(id)) continue
        const value = numberField(device, 'queue')
        if (value === undefined) continue
        const points = grouped.get(id) ?? []
        points.push({ at: time(frame.at), value })
        grouped.set(id, points)
      }
    }
    return [...grouped.entries()].map(([id, points]) => ({
      id,
      points: points.sort((a, b) => a.at - b.at),
    }))
  })()

  return (
    <div className="ex-page">
      <div className="ex-kpis">
        <Kpi label="1F 候梯人数" value={queue === undefined ? '—' : String(queue)} unit="人"
          note={adminGroup ? `${deviceLabel(adminGroup[0])} · ${clock(cursor, day.timezone)}` : groups.length ? '生产楼货梯快照' : '设备快照里没有梯群'}
          accent={queue !== undefined && queue >= 20} />
        <Kpi label="平均候梯" value={averageWait === undefined ? '—' : averageWait.toFixed(1)} unit="分钟"
          note={averageWait === undefined ? '没有平均候梯指标' : '效果对比 · 协调组'} />
        <Kpi label="运行台数" value={runningKnown ? String(running) : '—'} unit="台"
          note={groups.length ? `${groups.length} 个梯群有快照` : '缺少运行台数字段'} />
        <Kpi label="今日专家提案" value={String(proposals.length)} unit="条"
          note={latest ? `最新${confidenceText(latest.confidence)}` : '还没有电梯专家提案'} />
      </div>

      <div className="ex-main">
        <section className="ex-panel">
          <header className="ex-head">
            <h2>梯群实况</h2>
            <span className="ex-sub">按设备快照汇总；数据里没有轿厢位置，不画轿厢</span>
            <button type="button" className="ex-btn ex-push" disabled={!groups.length}
              onClick={() => openEvidence(groups[0]?.[0] ?? null, '梯群设备快照')}>设备明细</button>
          </header>
          <div className="ex-body flush">
            {groups.length ? (
              <>
                <table className="ex-table">
                  <thead>
                    <tr><th>梯群</th><th>运行台数</th><th>候梯人数</th><th>分区运行</th><th>故障</th></tr>
                  </thead>
                  <tbody>
                    {groups.map(([id, device]) => {
                      const zone = device.zone_operation
                      const fault = faultState(device)
                      const count = numberField(device, 'queue')
                      const busy = (count ?? 0) >= 20 || fault.active
                      return (
                        <tr key={id} className={busy ? 'on' : ''}>
                          <td><span className="ex-strong">{deviceLabel(id)}</span></td>
                          <td className="n">{numberField(device, 'running') ?? '—'}</td>
                          <td className="n">
                            {count ?? '—'}
                            {count !== undefined && <div className="ex-mini"><i style={{ width: `${Math.round((count / maxQueue) * 100)}%` }} /></div>}
                          </td>
                          <td>{zone === true ? '分区运行' : zone === false ? '全区运行' : '—'}</td>
                          <td>{fault.active ? <span className="ex-tag on">故障{fault.note ? ` · ${fault.note}` : ''}</span> : '—'}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
                <div className="ex-foot">
                  台数和候梯人数来自设备快照。
                </div>
                <div className="ex-divider" />
                <div className="ex-chart-block">
                  <div className="ex-subhead">
                    <b>候梯人数 · 全天</b>
                    <span>按每次设备快照取点</span>
                    <span className="ex-metaclock n">{clock(cursor, day.timezone)}</span>
                  </div>
                  {series.length ? (
                    <>
                      <div className="ex-chart-wrap">
                        <QueueChart day={day} cursor={cursor} series={series} />
                      </div>
                      <div className="ex-legend">
                        {series.map((group, index) => (
                          <span key={group.id}><i className={index === 0 ? 'line' : index === 1 ? 'line dash' : 'line soft'} />{deviceLabel(group.id)}</span>
                        ))}
                      </div>
                    </>
                  ) : <div className="ex-empty">这一天的设备快照里没有候梯人数。</div>}
                </div>
              </>
            ) : <div className="ex-empty">此刻还没有梯群设备快照。</div>}
          </div>
        </section>

        <div className="ex-col scroll">
          <Judgement day={day} cursor={cursor} proposal={latest} openEvidence={openEvidence} />
          <section className="ex-panel">
            <header className="ex-head">
              <h2>电梯相关事件</h2>
              <span className="ex-meta">{events.length} 条</span>
            </header>
            <div className="ex-body flush">
              {events.length ? (
                <div className="ex-scroll">
                  {events.slice(0, 6).map(event => (
                    <div className="ex-event" key={event.event_id}>
                      <span className="n">{clock(time(event.at), day.timezone)}</span>
                      <span className={'ex-tag' + ((event.severity ?? 1) >= 4 ? ' on' : '')}>
                        {EVENT_LABEL[event.event_type] ?? event.event_type}
                      </span>
                      <p className="ex-text">{event.summary}</p>
                    </div>
                  ))}
                </div>
              ) : <div className="ex-empty">此刻之前没有电梯相关事件。</div>}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}
