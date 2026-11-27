import { useState } from 'react'
import { describeAction } from '../../shared/campus'
import { outcomeLabel, workOrderStatusLabel } from '../../shared/labels'
import { decisionChain, isSelected } from '../data/evidence'
import { eventsOfTicket, findProposal, findTicket } from '../data/lookup'
import { activeEvents, decisionVisible } from '../data/selectors'
import { confidenceText, costText, expertShort, plainText, refLabel, ticketLabel, toolLabel, toolSummaryText } from '../data/labels'
import { clock, clockWithSeconds, resultVisibleAt, time } from '../data/time'
import type { Day, Decision, ProposedAction } from '../data/types'
import type { PageProps } from './types'
import './expert.css'

// Decision.layer is the arbitration layer, not the B47 autonomy level or routing channel.
const LAYER: Record<number, string> = { 1: '第一层', 2: '第二层', 3: '第三层' }
const decisionOutcome = (decision: Decision) => decision.result.outcome === 'executed'
  && decision.result.selected_proposal_ids.length === 0 ? '无选中动作' : outcomeLabel(decision.result.outcome)
const EVENT_TYPE: Record<string, string> = {
  elevator_peak: '电梯高峰',
  elevator_fault: '电梯故障',
  transformer_overload: '变压器越限',
  air_pressure_abnormal: '空压压力异常',
  equipment_vibration: '设备振动',
  lighting_fault: '照明回路故障',
  compound_anomaly: '复合异常',
  operator_instruction: '人工指令',
  operator_confirm: '值班确认',
  document_ingest: '资料入库',
  valley_charge: '谷段充电',
}
const seconds = (ms: number | null | undefined) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`)
const shortId = (id: string) => id.split('/').pop() ?? id
const eventLabel = (type: string) => EVENT_TYPE[type] ?? type
const actionText = (action: ProposedAction) =>
  describeAction({ entityId: action.entity_id, command: action.command, params: action.params, start: 0, end: 0 })

/** 把一个裁决拆成六步；耗时只取提案计时或时间戳差，缺的留 —。 */
function chainSteps(day: Day, decision: Decision, cursor: number) {
  const ticket = findTicket(day, decision.ticket_id)
  const events = eventsOfTicket(day, decision.ticket_id).filter(event => time(event.at) <= cursor)
  const proposals = (decision.conflict.proposal_ids ?? [])
    .flatMap(proposalId => findProposal(day, proposalId) ?? [])
  const selected = new Set(decision.result.selected_proposal_ids)
  const picked = proposals.filter(item => item.proposal_id && selected.has(item.proposal_id))
  const actions = picked.flatMap(item => item.actions ?? [])
  const latencies = proposals.map(item => item.timing?.latency_ms).filter((value): value is number => value != null)
  const scores = Object.keys(decision.conversion.scores ?? {})
  const notes = (decision.conversion.notes ?? []).map(plainText)
  const runs = day.agent_runs.filter(run => run.binding?.ticket_id === decision.ticket_id && resultVisibleAt(run) <= cursor)
  const orders = day.work_orders.filter(order => order.ticket_id === decision.ticket_id
    && time(order.created_at) <= time(decision.at))
  const firstEvent = events[0]
  const elapsed = ticket ? time(decision.at) - time(ticket.opened_at) : null

  return [
    {
      title: '事件触发',
      detail: events.length
        ? `${events.slice(0, 2).map(event => event.summary).join('；')}${events.length > 2 ? ` 等 ${events.length} 条` : ''}`
        : '事件单没有关联事件明细',
      meta: firstEvent ? `${clock(time(firstEvent.at), day.timezone)} · ${events.length} 条` : '—',
    },
    {
      title: '专家提案',
      detail: proposals.length ? proposals.map(item => expertShort(item.expert)).join('、') : '没有专家提交方案',
      meta: latencies.length ? `${seconds(Math.max(...latencies))} 最慢` : '—',
    },
    {
      title: '统一量纲换算',
      detail: notes.length ? notes[0] : '—',
      meta: scores.length ? `${scores.length} 项折算` : '—',
    },
    {
      title: '裁决',
      detail: `${LAYER[decision.layer] ?? '未记录层级'}裁决 · ${decisionOutcome(decision)}`,
      meta: elapsed == null ? '—' : `${Math.round(elapsed / 1000)} s`,
    },
    {
      title: '下发',
      detail: actions.length ? actions.map(actionText).join('、') : '这次没有下发动作',
      meta: actions.length ? `${actions.length} 条动作` : '—',
    },
    {
      title: '回执',
      detail: orders.length
        ? `${orders.length} 张操作工单 · ${workOrderStatusLabel(orders[0].status)}`
        : runs.length ? `${runs.length} 条运行记录` : '没有回执记录',
      meta: orders.length ? clock(time(orders[0].created_at), day.timezone) : '—',
    },
  ]
}

/** 每类事件的裁决层级；不能把裁决层级解释为自主等级。 */
function autonomyRows(day: Day, cursor: number) {
  const ticketOf = new Map<string, string>()
  for (const ticket of day.tickets) for (const eventId of ticket.event_ids ?? []) ticketOf.set(eventId, ticket.ticket_id)
  const latestInTicket = new Map<string, Decision>()
  for (const decision of day.decisions) {
    if (!decisionVisible(day, decision, cursor)) continue
    if (decision.result.outcome !== 'executed' && decision.result.outcome !== 'work_order') continue
    const previous = latestInTicket.get(decision.ticket_id)
    if (!previous || time(decision.at) >= time(previous.at)) latestInTicket.set(decision.ticket_id, decision)
  }
  const rows = new Map<string, { count: number; layer: number | null }>()
  for (const event of day.events) {
    if (time(event.at) > cursor) continue
    const ticketId = ticketOf.get(event.event_id)
    const layer = ticketId ? latestInTicket.get(ticketId)?.layer ?? null : null
    const row = rows.get(event.event_type) ?? { count: 0, layer }
    row.count += 1
    if (layer !== null) row.layer = layer
    rows.set(event.event_type, row)
  }
  return [...rows.entries()]
    .sort((a, b) => b[1].count - a[1].count)
    .map(([type, row]) => ({ type, ...row }))
}

function QaPanel({ day, cursor, openEvidence, source }: PageProps) {
  const [picked, setPicked] = useState<string | null>(null)
  const entries = day.qa.filter(entry => time(entry.at) <= cursor).sort((a, b) => time(a.at) - time(b.at))
  const latest = entries[entries.length - 1]
  // 往回拖动播放头后，先前点选的问题可能已经落在“未来”，按未选处理，绝不显示未来的回答。
  const activePicked = picked && entries.some(entry => entry.question === picked) ? picked : null
  const chosen = activePicked ? entries.find(entry => entry.question === activePicked) : undefined
  const shown = activePicked ? chosen : latest
  const run = shown?.agent_run_id ? day.agent_runs.find(item => item.agent_run_id === shown.agent_run_id) : undefined
  const tools = run?.tool_calls ?? []
  const toolTime = tools.length
    ? `${(tools.reduce((sum, call) => sum + (call.duration_ms ?? 0), 0) / 1000).toFixed(1)} s`
    : seconds(run?.timing?.latency_ms)
  // 预设问题只用此刻之前 day.json 里真实记录过的问答（最近 3~4 条），不掺示例问题。
  const presets = [...new Set(entries.map(entry => entry.question))].slice(-4)
  const activeQuestion = shown?.question ?? null
  const synthetic = source === 'sample'

  return (
    <section className="ex-panel ex-qa">
      <header className="ex-head">
        <h2>运维问答</h2>
        <span className="ex-sub">
          {synthetic
            ? '标准演示场景中的运维问答记录，随播放进度同步展示'
            : '按当前回放时刻展示对应运维问答记录'}
        </span>
      </header>
      <div className="ex-thread">
        {shown ? (
          <>
            <div className="ex-bubble-q">{shown.question}</div>
            <div className="ex-bubble-a">
              <div className="ex-bubble-meta">
                <span className="n">{clock(time(shown.at), day.timezone)}</span>
                <span>调用 {run ? tools.length : '—'} 个工具 · {toolTime}</span>
                {synthetic && <span className="ex-tag on">演示场景</span>}
                {!shown.agent_run_id && <span className="ex-tag">无运行记录关联</span>}
                {shown.agent_run_id && !run && <span className="ex-tag">运行记录未导出</span>}
              </div>
              {tools.length > 0 && (
                <div className="ex-tools">
                  {tools.map(call => (
                    <div className="ex-tool" key={call.call_id}>
                      <i aria-hidden="true" />
                      <span>{toolLabel(call.tool)}{call.summary ? ` · ${toolSummaryText(call.summary)}` : ''}</span>
                      <small>{seconds(call.duration_ms)}</small>
                    </div>
                  ))}
                </div>
              )}
              {shown.answer?.trim() ? plainText(shown.answer) : '这条问答只有问题，没有记录回答。'}
              {shown.evidence_refs?.length ? (
                <div className="ex-refs">
                  {shown.evidence_refs.map(ref => (
                    <button type="button" className="ex-btn" key={ref}
                      onClick={() => openEvidence(ref, `问答依据 · ${shown.question}`)}>依据 {refLabel(ref)}</button>
                  ))}
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <div className="ex-empty">当前时刻暂无问答记录，请推进播放条查看后续交互。</div>
        )}
      </div>
      <div className="ex-presets">
        {presets.map(question => (
          <button type="button" key={question} aria-pressed={question === activeQuestion}
            className={'ex-btn ex-preset' + (question === activeQuestion ? ' on' : '')}
            onClick={() => setPicked(question)}>{question}</button>
        ))}
      </div>
    </section>
  )
}

export function MainAgentPage(props: PageProps) {
  const { day, cursor, openEvidence } = props
  const chain = decisionChain(day, cursor)
  const decision = chain?.decision
  const candidates = decision
    ? (decision.conflict.proposal_ids ?? [])
      .flatMap(id => findProposal(day, id) ?? [])
    : []
  const selectedNow = new Set(decision?.result.selected_proposal_ids ?? [])
  const steps = decision ? chainSteps(day, decision, cursor) : []
  const currentEvents = activeEvents(day, cursor)
  const levels = autonomyRows(day, cursor)

  return (
    <div className="ex-page ex-agent">
      <div className="ex-agent-main">
        <div className="ex-agent-left">
          <section className="ex-panel ex-decision">
            {currentEvents.length > 0 && <p className="ex-sub">此刻事件：{currentEvents.map(event => event.summary).join('；')}</p>}
            <header className="ex-head">
              <h2>本次裁决链路</h2>
              {decision ? (
                <span className="ex-sub">{ticketLabel(decision.ticket_id)} · {clockWithSeconds(time(decision.at), day.timezone)}</span>
              ) : <span className="ex-sub">此刻之前还没有裁决</span>}
              <button type="button" className="ex-btn ex-push" disabled={!decision}
                onClick={() => decision && openEvidence(null, `Agent 运行记录 · ${ticketLabel(decision.ticket_id)}`, 'runs', decision.ticket_id)}>
                Agent 运行记录
              </button>
            </header>
            {decision ? (
              <div className="ex-decision-body">
                <div className="ex-verdict">
                  <b>{decisionOutcome(decision)}</b>
                  <span>
                    {LAYER[decision.layer] ?? '未记录层级'}裁决 · 裁决记录未含自主等级
                    {' '}· 采纳 {decision.result.selected_proposal_ids.length} / {decision.conflict.proposal_ids.length} 个方案
                  </span>
                </div>
                {chain?.pending && (
                  <div className="ex-pending">
                    <span className="ex-tag hot">{outcomeLabel(chain.pending.result.outcome)}</span>
                    <span className="ex-pending-text">{chain.pending.result.summary}</span>
                    <span className="ex-dim n">{clock(time(chain.pending.at), day.timezone)}</span>
                  </div>
                )}
                <div className="ex-split">
                  <div className="ex-chain">
                    <span className="ex-label">决策链</span>
                    <ol className="ex-chain-list">
                      {steps.map((step, index) => (
                        <li key={step.title}>
                          <span className="ex-step-n n">{index + 1}</span>
                          <span className="ex-step-main"><b>{step.title}</b><small>{step.detail}</small></span>
                          <span className="ex-step-meta n">{step.meta}</span>
                        </li>
                      ))}
                    </ol>
                  </div>
                  <div className="ex-candidates">
                    <span className="ex-label">候选方案对比</span>
                    <div className="ex-scroll">
                      <table className="ex-table">
                        <thead>
                          <tr><th>方案</th><th>动作</th><th>折算</th><th>把握</th><th>裁决</th></tr>
                        </thead>
                        <tbody>
                          {candidates.map(proposal => {
                            const id = proposal.proposal_id ?? ''
                            const picked = selectedNow.has(id)
                            const tag = picked ? '选中' : isSelected(proposal, day, cursor) ? '已采纳' : '驳回'
                            const actions = (proposal.actions ?? []).map(actionText).join('、')
                            return (
                              <tr key={id || `${proposal.expert}-${proposal.ticket_id}`} className={picked ? 'on' : ''}>
                                <td>
                                  <span className="ex-strong">{expertShort(proposal.expert)}</span>
                                </td>
                                <td>{actions || '—'}</td>
                                <td className="n">{costText(proposal.cost.cny)}</td>
                                <td className="n">{confidenceText(proposal.confidence)}</td>
                                <td><span className={'ex-tag' + (picked ? ' on' : '')}>{tag}</span></td>
                              </tr>
                            )
                          })}
                          {!candidates.length && (
                            <tr><td colSpan={5} className="ex-empty-cell">
                              {decision.result.outcome === 'executed'
                                ? '这次裁决没有候选方案，请查看运行记录和回执。'
                                : '这次裁决没有候选方案，直接转人工。'}
                            </td></tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              </div>
            ) : <div className="ex-empty">此刻之前还没有裁决。把播放头往后拖，等第一条事件单进入研判。</div>}
          </section>

          <section className="ex-panel ex-levels">
            <header className="ex-head">
              <h2>每类事件的裁决层级</h2>
              <span className="ex-sub">裁决层级不代表自主等级；此表仅展示裁决层级</span>
            </header>
            <div className="ex-levels-body">
              {levels.length ? (
                <>
                  <div className="ex-levels-board">
                    {[1, 2, 3].map(layer => {
                      const items = levels.filter(row => row.layer === layer)
                      return (
                        <div className="ex-level-col" key={layer}>
                          <div className="ex-level-head">
                            <span>{LAYER[layer]}裁决</span>
                            <b className="n">{items.reduce((sum, row) => sum + row.count, 0)} 起</b>
                          </div>
                          {items.length ? items.map(row => (
                            <div className="ex-level-item" key={row.type}>
                              <span>{eventLabel(row.type)}</span>
                              <small>{row.count} 起</small>
                            </div>
                          )) : <span className="ex-level-none">—</span>}
                        </div>
                      )
                    })}
                  </div>
                  {levels.some(row => row.layer === null) && (
                    <div className="ex-note">
                      未裁决：{levels.filter(row => row.layer === null).map(row => eventLabel(row.type)).join('、')}
                    </div>
                  )}
                </>
              ) : <div className="ex-empty-inline">此刻之前还没有事件，矩阵等第一条告警。</div>}
            </div>
          </section>
        </div>
        <QaPanel {...props} />
      </div>
    </div>
  )
}
