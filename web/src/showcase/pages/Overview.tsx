import { useEffect, useMemo, type RefObject } from 'react'
import type { CampusEntity, CampusSpot } from '../../shared/campus'
import { buildCampus, entityAt } from '../../shared/campus'
import { IsoCampus, type IsoCampusHandle } from '../../shared/iso/IsoCampus'
import { LoadCurve } from '../../shared/LoadCurve'
import { TweenNumber } from '../../shared/TweenNumber'
import { ArrowRightIcon } from '../../shared/icons'
import { outcomeLabel } from '../../shared/labels'
import { latestDecision, openWorkOrders, humanHandoffs } from '../data/selectors'
import { latestExecutedDecision } from '../data/evidence'
import { plainText, plantLabel, refLabel, replayDateText } from '../data/labels'
import {
  actionRows, campusInput, config, decisionEffect, effectDelta, readings, todayStats, transformerLoadPoints,
} from '../data/rollup'
import { clock, dayAtHour, dayEnd, time } from '../data/time'
import type { Day } from '../data/types'
import type { EvidenceTab } from '../components/EvidenceDrawer'

export interface OverviewProps {
  day: Day
  cursor: number
  selected: CampusSpot | null
  onSelect: (spot: CampusSpot | null) => void
  campusRef: RefObject<IsoCampusHandle | null>
  openEvidence: (entityId: string | null, fallbackTitle: string, tab?: EvidenceTab) => void
  /** 自动巡演中这一幕的镜头位置；有值时厂区图压暗无关区域，镜头把事件和动作同框 */
  tourSpot?: CampusSpot | null
}

/**
 * 总览：左栏“此刻”六读数与“今天到现在”，中间厂区数字孪生，右栏 Agent 结论、
 * 三条动作、三个证据入口与 T1 全天曲线。数值全部来自统一读数层与 day.json，缺数据写 —。
 */
export function Overview({ day, cursor, selected, onSelect, campusRef, openEvidence, tourSpot = null }: OverviewProps) {
  const entities: CampusEntity[] = useMemo(() => buildCampus(campusInput(day, cursor)), [day, cursor])
  const nowReadings = useMemo(() => readings(day, cursor), [day, cursor])
  const stats = useMemo(() => todayStats(day, cursor), [day, cursor])
  const rows = useMemo(() => actionRows(day, cursor).slice(0, 3), [day, cursor])
  // 结论跟正在执行的动作对齐：取真正下发过动作的那次裁决；转人工那条留在待人工块。
  const decision = latestExecutedDecision(day, cursor)
  const latestAny = latestDecision(day, cursor)
  const loadPoints = useMemo(() => transformerLoadPoints(day), [day])
  const limit = config.transformerLimit(day)
  const touring = tourSpot !== null

  // 巡演镜头：有事件或动作时把它们一起框进来，没有就推到这一幕的位置；新动作出现时重新取景。
  const lit = entities.filter(entity => entity.tone !== 'normal').map(entity => entity.spot).join(',')
  useEffect(() => {
    if (!tourSpot) return
    const id = window.setTimeout(() => {
      if (lit) campusRef.current?.focusSpots(lit.split(',') as CampusSpot[], [0.06, 0.94])
      else campusRef.current?.focusSpot(tourSpot)
    }, 80)
    return () => window.clearTimeout(id)
  }, [campusRef, lit, tourSpot])

  const selectedProposals = decision
    ? day.proposals.filter(proposal => proposal.proposal_id && decision.result.selected_proposal_ids.includes(proposal.proposal_id))
    : []
  const refs = Array.from(new Set(selectedProposals.flatMap(proposal => proposal.evidence_refs ?? [])))
  const rehearsalNote = decision?.conversion.notes?.find(note => note.includes('预演'))
  const effect = useMemo(() => decisionEffect(day, decision, cursor), [day, decision, cursor])
  // 预演说明已经显示在结论条上，注释里不再重复占一行。
  const notes = (decision?.conversion.notes ?? []).filter(note => note !== rehearsalNote).map(plainText)
  // 右栏最多列 3 条依据，其余仍收在“依据原文”抽屉里。
  const shownRefs = refs.slice(0, 3)
  const moreRefs = refs.length - shownRefs.length
  const selectedEntity = entityAt(entities, selected)
  const evidenceEntity = rows[0]?.entityId
    ?? selectedEntity?.entityId
    ?? selectedProposals.flatMap(proposal => (proposal.actions ?? []).map(action => action.entity_id))[0]
    ?? null
  const evidenceTitle = decision?.result.summary ?? rows[0]?.label ?? '这一刻的依据'

  const orders = openWorkOrders(day, cursor)
  const blocked = humanHandoffs(day, cursor).tickets
  const handoffs = orders.length + blocked.length

  return (
    <div className="overview" data-page="overview">
      <aside className="panel rail left" aria-label="实时指标与当日累计统计">
        <div className="sec">
          <div className="sec-title">实时指标<span className="dm">竖线为安全阈值</span></div>
          {nowReadings.map(item => (
            <div className="reading" key={item.key}>
              <div className="reading-head">
                <span className="lb">{item.label}</span>
                <b className={'n' + (item.over ? ' or' : '')}><TweenNumber value={item.value} /></b>
                <span className="dm reading-unit">{item.unit}</span>
              </div>
              <div className="g">
                <i className={item.over ? 'x' : ''} style={{ width: `${item.pct ?? 0}%` }} />
                {item.tick !== null && <u style={{ left: `${item.tick}%` }} />}
              </div>
              <span className="dm">{item.note}</span>
            </div>
          ))}
        </div>

        <div className="sec">
          <div className="sec-title">当日累计统计</div>
          <div className="stats">
            <div><span className="lb">一二层结果为执行 / 三层裁决</span><b className="n">{stats.layer12Executed} / {stats.layer3}</b></div>
            <div><span className="lb">预演一致</span><b className="n">{stats.rehearsalOk} / {stats.rehearsalTotal}</b></div>
            <div><span className="lb">事件 / 事件单</span><b className="n">{stats.events} / {stats.tickets}</b></div>
            <div><span className="lb">引用资料</span><b className="n">{stats.evidence} 条</b></div>
          </div>
        </div>

        <div className="datanote" title={`${day.plant_id} · ${day.run_id} · ${day.events.length} 条事件`}>
          {plantLabel(day.plant_id)} · {replayDateText(day.local_date)} · {day.events.length} 条事件
        </div>
      </aside>

      <section className="campus" aria-label="厂区数字孪生">
        <div className="campus-head">
          <b>厂区数字孪生</b>
          <span className="lb">固定视角轴测图 · 滚轮缩放 · 点建筑选中 · 右侧图标看剖面</span>
        </div>
        <div className="campus-body">
          <IsoCampus
            ref={campusRef}
            entities={entities}
            selected={selected}
            onSelect={onSelect}
            margin={{ l: 40, r: 70, t: 70, b: 10 }}
            motion="show"
            spotlight={touring && entities.some(entity => entity.tone !== 'normal')}
          />
        </div>
      </section>

      <aside className="panel rail right" aria-label="Agent 结论与动作">
        <div className="railscroll">
          <div className="sec conclusion">
            <div className="sec-title">
              Agent 结论
              <span className="dm">{decision ? `${outcomeLabel(decision.result.outcome)} · ${clock(time(decision.at), day.timezone)}` : '等待事件'}</span>
            </div>

            {decision ? (
              <div className="conclusion-body" key={decision.decision_id}>
                <div className="conclusion-flag">
                  <b className="agent">{outcomeLabel(decision.result.outcome)}</b>
                  {/* 读数发生前只写“待观察”，不给未来数字；读数发生后写实际值。 */}
                  {effect
                    ? <span>
                      {effect.done
                        ? `实际 T1 负载 ${effect.before}% → ${effect.after}%，${effectDelta(effect)}`
                        : `已裁决，效果待观察（裁决时 ${effect.before}%）`}
                    </span>
                    : rehearsalNote && <span>{rehearsalNote}</span>}
                </div>
                <p className="conclusion-summary">{decision.result.summary}</p>
                <div className="conclusion-conflict">
                  <span className="lb">冲突</span>
                  <p>{decision.conflict.summary}</p>
                </div>
                {notes.length > 0 && (
                  <ul className="conclusion-notes">
                    {notes.map(note => <li key={note}>{note}</li>)}
                  </ul>
                )}
                <div className="conclusion-refs">
                  <span className="dm">依据</span>
                  {shownRefs.length
                    ? shownRefs.map(ref => (
                      <button type="button" className="ref" key={ref} title={ref}
                        onClick={() => openEvidence(evidenceEntity, evidenceTitle, 'rule')}>
                        {refLabel(ref)}
                      </button>
                    ))
                    : <span className="dm">—</span>}
                  {moreRefs > 0 && (
                    <button type="button" className="restref" title="其余依据在“依据原文”抽屉里"
                      onClick={() => openEvidence(evidenceEntity, evidenceTitle, 'rule')}>
                      另有 {moreRefs} 条
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <p className="empty">
                {rows.length ? `正在执行 ${rows.length} 条动作，等待下一次裁决。` : '此刻还没有裁决；等待事件到达。'}
              </p>
            )}

            {latestAny && latestAny.result.outcome !== 'executed' && latestAny.decision_id !== decision?.decision_id && (
              <div className="handoff">
                <div className="handoff-title">新增待确认事项<span className="badge">1</span></div>
                <div className="handoff-row">
                  <span className="chip hot">{outcomeLabel(latestAny.result.outcome)}</span>
                  <span title={latestAny.result.summary}>{latestAny.result.summary}</span>
                </div>
              </div>
            )}

            {handoffs > 0 && (
              <div className="handoff">
                <div className="handoff-title">待人工确认<span className="badge">{handoffs}</span></div>
                {orders.map(order => (
                  <div className="handoff-row" key={order.work_order_id}>
                    <span className="chip hot">操作工单</span>
                    <span title={order.reason}>{order.reason}</span>
                  </div>
                ))}
                {blocked.map(ticket => (
                  <div className="handoff-row" key={ticket.ticket_id}>
                    <span className="chip">受阻</span>
                    <span title={ticket.summary ?? '事件单等待人工处理'}>{ticket.summary ?? '事件单等待人工处理'}</span>
                  </div>
                ))}
              </div>
            )}
          </div>


          <div className="actionrows" role="group" aria-label="执行中的 Agent 动作">
            {rows.length ? rows.map((row, index) => (
              <button
                type="button"
                className="row actionrow"
                key={row.key}
                style={{ animationDelay: `${index * 90}ms` }}
                onClick={() => openEvidence(row.entityId, row.label)}
                title="打开这条动作的依据"
              >
                <i className="am" aria-hidden="true" />
                <span title={row.title}>{row.label}</span>
                <span className="dm actiontime">{row.detail}</span>
              </button>
            )) : <p className="empty actionempty">此刻没有执行中的动作</p>}
          </div>

          <div className="sec actionbtns">
            <button type="button" className="btn on" onClick={() => openEvidence(evidenceEntity, evidenceTitle, 'experts')}>
              查看冲突裁决<ArrowRightIcon size={14} />
            </button>
            <button type="button" className="btn" onClick={() => openEvidence(evidenceEntity, evidenceTitle, 'rule')}>依据原文</button>
            <button type="button" className="btn" onClick={() => openEvidence(evidenceEntity, evidenceTitle, 'runs')}>运行记录</button>
          </div>
        </div>

        <div className="sec curvecard">
          <div className="sec-title">T1 全天负载曲线<span className="dm">实线为已回放 · 虚线为后续时段</span></div>
          <div className="curvebody">
            <LoadCurve
              points={loadPoints}
              cursor={cursor}
              start={dayAtHour(day, 0)}
              end={dayEnd(day)}
              limit={100}
              alarm={limit ?? undefined}
              width={344}
              height={150}
              ariaLabel="T1 变压器负载全天曲线"
            />
          </div>
        </div>
      </aside>
    </div>
  )
}
