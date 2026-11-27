import { useEffect, useMemo, useState, type RefObject } from 'react'
import { TweenNumber } from '../../shared/TweenNumber'
import type { CampusEntity, CampusSpot } from '../../shared/campus'
import { buildCampus } from '../../shared/campus'
import { IsoCampus, type IsoCampusHandle } from '../../shared/iso/IsoCampus'
import { prefersReducedMotion } from '../../shared/motion'
import { ticketLabel } from '../data/labels'
import { campusInput, decisionEffect, effectDelta } from '../data/rollup'
import { time } from '../data/time'
import type { Day } from '../data/types'
import { tourFrameAt, tourStage, type TourFrame } from '../tour'

/**
 * 舞台态：事件段把厂区图压暗当背景，左边大标题与三句说明、专家并行泳道，
 * 右边冲突 → 裁决 → 合并后的效果三段与预演记录。数字全部从 day.json 算。
 */
export function StageView({ day, cursor, frame, campusRef, onSelect }: {
  day: Day
  cursor: number
  frame: TourFrame
  campusRef: RefObject<IsoCampusHandle | null>
  onSelect: (spot: CampusSpot | null) => void
}) {
  const data = useMemo(() => tourStage(day, tourFrameAt(day, frame), cursor), [day, cursor, frame])
  // 舞台把这一幕压成“出事 → 裁决下发”：事件取此刻，动作取裁决下发后的那一刻，厂区图上能看到 Agent 接手。
  const entities: CampusEntity[] = useMemo(() => {
    const now = campusInput(day, cursor)
    const decided = data.decision?.result.outcome === 'executed' ? time(data.decision.at) + 60_000 : null
    if (!decided || decided <= cursor) return buildCampus(now)
    return buildCampus({
      ...now,
      at: decided,
      actions: campusInput(day, decided).actions,
    })
  }, [day, cursor, data.decision])
  const effect = useMemo(() => decisionEffect(day, data.decision, cursor), [day, cursor, data.decision])
  // 数字在派发线到达设备前后滚落到下发后的负载；副标题等数字落定再出，别先写“降 15”而数字还是 96%。
  const [landed, setLanded] = useState(false)
  const [settled, setSettled] = useState(false)
  useEffect(() => {
    const roll = prefersReducedMotion() ? 0 : 1100
    const start = window.setTimeout(() => setLanded(true), 1800)
    const done = window.setTimeout(() => setSettled(true), 1800 + roll)
    return () => { window.clearTimeout(start); window.clearTimeout(done) }
  }, [])
  const dark = frame.index === 5
  // 镜头把事件和它派出的动作一起框住，派发线整条落在画面里。
  const lit = entities.filter(entity => entity.tone !== 'normal').map(entity => entity.spot).join(',')
  useEffect(() => {
    const id = window.setTimeout(() => campusRef.current?.focusSpots(lit ? lit.split(',') as CampusSpot[] : [], [0.36, 0.72]), 60)
    return () => window.clearTimeout(id)
  }, [campusRef, lit])

  return (
    <section className={'stageview' + (dark ? ' dark' : '')} data-stage={frame.index + 1}
      aria-label={`${frame.title} · 舞台态`}>
      <div className="stageview-campus" aria-hidden="true">
        <IsoCampus
          ref={campusRef}
          entities={entities}
          onSelect={onSelect}
          margin={{ l: 0, r: 0, t: 24, b: 0 }}
          toolbarStyle={{ display: 'none' }}
          legend={false}
          motion="show"
          spotlight
          replayOnMount
          ariaLabel="舞台态厂区轴测图"
        />
      </div>
      <div className="stageview-shade" aria-hidden="true" />

      <div className="stageview-copy">
        <span className="stageview-eyebrow">
          {frame.title} · {frame.hour}
          {data.ticket ? ` · ${ticketLabel(data.ticket.ticket_id)}` : ''}
        </span>
        <h1>{data.title}</h1>
        <p>{data.sentences.join('')}</p>
      </div>

      <section className="stageview-lanes panel" aria-label="专家并行会诊">
        <div className="lanes-head">
          <b>专家并行会诊</b>
          <span className="dm">按知识图谱挑出相关专家</span>
        </div>
        <div className="lanes-body">
          {data.lanes.map(lane => (
            <div className={'lane' + (lane.selected ? ' sel' : '')} key={lane.expert}>
              <span className="lane-name">{lane.name}</span>
              <div className="lane-bar">
                <i style={{ width: `${Math.round(lane.progress * 100)}%` }} />
                <span>{lane.diagnosis}</span>
              </div>
              <span className="n lane-time">{lane.latency}</span>
            </div>
          ))}
        </div>
      </section>

      <div className="stageview-right">
        <section className="stagecard">
          <span className="lb">① 冲突</span>
          {data.conflict.length ? (
            <div className="conflict-grid" style={{ gridTemplateColumns: `repeat(${Math.min(3, data.conflict.length)}, minmax(0, 1fr))` }}>
              {data.conflict.map(item => (
                <div className="conflict-cell" key={`${item.name}-${item.demand}`}>
                  <b>{item.name}</b>
                  <span>{item.demand}</span>
                </div>
              ))}
            </div>
          ) : <p className="dm stageempty">{data.decision ? '没有专家提案' : '当日数据没有这一段'}</p>}
        </section>

        <section className="stagecard">
          <span className="lb">② 裁决</span>
          {data.verdict.length ? (
            <div className="verdictlist">
              {data.verdict.map(text => <div className="verdictrow" key={text}>{text}</div>)}
            </div>
          ) : <p className="dm stageempty">{data.verdictNote}</p>}
          {data.verdict.length ? <span className="dm verdictnote">{data.verdictNote}</span> : null}
        </section>

        {effect ? (
          <section className="stagecard effectcard" aria-label="合并后的效果">
            <span className="lb agent">③ 合并后的效果<span className="dm">{effect.done ? '实际' : '待观察'}</span></span>
            {effect.done ? (
              <>
                <div className="effect-big">
                  <span className="n or">{effect.before}%</span>
                  <i aria-hidden="true">→</i>
                  <b className="n agent"><TweenNumber value={String(landed ? effect.after : effect.before)} ms={1100} />%</b>
                </div>
                <span className="effect-sub" style={{ visibility: settled ? 'visible' : 'hidden' }}>
                  实际 T1 负载{effectDelta(effect)}
                  {effect.limit !== null && effect.after <= effect.limit && effect.after < effect.before ? '，回到安全线内' : ''}
                </span>
              </>
            ) : (
              <>
                <div className="effect-big"><span className="n or">{effect.before}%</span></div>
                <span className="effect-sub">已裁决，效果待观察</span>
              </>
            )}
            <span className="dm">{data.conversionHeadline}</span>
          </section>
        ) : (
          <section className="stagecard">
            <span className="lb">③ 换算</span>
            <b className="conversion">{data.conversionHeadline}</b>
          </section>
        )}

        <section className="stagecard rehearsalline" aria-label="预演与转人工记录">
          <div>{data.rehearsal}</div>
          {data.handoffFlow && <div className="dm handoffflow">{data.handoffFlow}</div>}
        </section>
      </div>
    </section>
  )
}
