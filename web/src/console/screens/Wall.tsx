import { useState } from 'react'
import type { CampusSpot } from '../../shared/campus'
import { IsoCampus } from '../../shared/iso/IsoCampus'
import { LoadCurve } from '../../shared/LoadCurve'
import { t1, t1Note } from '../../shared/readings'
import { dayEnd, dayStart, time } from '../../store'
import { wallOutcomeLabel } from '../../shared/labels'
import { fmtClock } from '../data'
import type { ConsoleModel } from '../types'

const MAP_MARGIN = { l: 60, r: 80, t: 70, b: 10 }
const MAP_TOOLBAR = { top: 8, right: 8 }

export interface WallScreenProps {
  model: ConsoleModel
  onGoDesk: () => void
}

/** 态势墙：厂区图 + 全厂负荷/能源策略/告警 + 底部正在执行与待你处理。 */
export function WallScreen({ model, onGoDesk }: WallScreenProps) {
  const [selected, setSelected] = useState<CampusSpot | null>(null)
  const { day, cursor } = model
  const zone = day.timezone

  const note = (() => {
    const alert = model.alerts[0]
    if (alert) return `${fmtClock(alert.at, zone)} ${alert.title} · ${alert.state}`
    const latest = day.decisions.filter(decision => time(decision.at) <= cursor).slice(-1)[0]
    if (latest) return `${fmtClock(time(latest.at), zone)} ${latest.result.summary} · ${wallOutcomeLabel(latest.result.outcome)}`
    return '当前没有进行中的事件'
  })()

  const load = model.energy.load
  const mix = load ? [
    { key: 'grid', label: '市电', kw: load.grid, className: 'mix-grid' },
    { key: 'pv', label: '光伏', kw: load.pv, className: 'mix-pv' },
    { key: 'storage', label: '储能放电', kw: load.storage, className: 'mix-storage' },
  ].filter(part => part.kw > 0) : []
  const point = model.loadPoints.filter(item => item.at <= cursor).slice(-1)[0]
  // T1 余量、已到上限、超限的口径统一在 shared/readings（与厂区图标注、展示页同源）。
  const t1Read = t1(day, cursor)
  const running = model.actions.filter(action => !action.retracted)

  return (
    <div className="wall">
      <main className="wall-main">
        <section className="panel wall-map" aria-label="厂区态势">
          <div className="wall-map-head">
            <b>厂区态势</b>
            <span className="lb">{note}</span>
          </div>
          <div className="wall-map-body">
            <IsoCampus entities={model.campus} selected={selected} onSelect={setSelected}
              margin={MAP_MARGIN} toolbarStyle={MAP_TOOLBAR} />
          </div>
        </section>

        <aside className="wall-side">
          <section className="panel wall-load">
            <div className="sec">
              <div className="sec-title">全厂负荷
                <span className="dm">合同容量 {load?.capacity != null ? `${load.capacity} kW` : '—'}</span>
              </div>
              <div className="load-big">
                <b className={'n' + (load ? '' : ' load-unknown')}>{load ? load.total : '—'}</b>
                <span className="lb">kW</span>
                <span className="load-margin dm">T1 <b className="n or">{t1Read.loadPct === null ? '—' : t1Note(t1Read)}</b></span>
              </div>
              <div className="load-mix" aria-label="全厂负荷来源构成">
                {mix.length
                  ? mix.map(part => (
                    <div key={part.key} className={part.className} style={{ flex: `${part.kw} 1 0` }}>
                      {part.label} <b className="n">{part.kw}</b>
                    </div>
                  ))
                  : <div className="load-mix-empty">{load ? '各来源出力为 0' : '暂无分钟级账本数据'}</div>}
              </div>
              <div className="load-curve">
                <LoadCurve points={model.loadPoints} cursor={cursor} start={dayStart(day)} end={dayEnd(day)}
                  height={132} width={398} ariaLabel={`T1 变压器负载全天曲线，当前 ${point ? Math.round(point.value) : '—'}%`} />
              </div>
            </div>
          </section>

          <section className="panel wall-energy">
            <div className="sec">
              <div className="sec-title">能源策略<span className="strategy">{model.energy.strategy}</span></div>
              <div className="energy-kv">
                <div><span className="lb">电价 · {model.energy.priceTier}</span><b className="n">{model.energy.price}</b></div>
                <div><span className="lb">储能 SOC</span><b className="n">{model.energy.soc}</b></div>
                <div><span className="lb">充放功率</span><b className={'n' + (model.energy.power.startsWith('−') ? ' agent' : '')}>{model.energy.power}</b></div>
              </div>
              <div className="energy-plan dm">
                {model.energy.plan.length
                  ? model.energy.plan.map(item => <span key={item.at + item.text}><b className="n">{item.at}</b> {item.text}</span>)
                  : <span>没有后续计划</span>}
              </div>
            </div>
          </section>

          <section className="panel wall-alerts">
            <div className="sec wall-alerts-head">
              <div className="sec-title">告警</div>
              <span className="dm">{model.alerts.length ? `最近 ${model.alerts.length} 条` : '暂无记录'}</span>
            </div>
            <div className="wall-alert-list">
              {model.alerts.length
                ? model.alerts.map(alert => (
                  <div className="alert-row" key={alert.id}>
                    <i className={alert.severity >= 4 ? 'sev-high' : alert.severity >= 3 ? 'sev-mid' : 'sev-low'} aria-hidden="true" />
                    <b className="n">{fmtClock(alert.at, zone)}</b>
                    <span className="alert-title" title={alert.title}>{alert.title}</span>
                    <span className="dm">{alert.state}</span>
                  </div>
                ))
                : <div className="wall-empty dm">当前时段没有告警</div>}
            </div>
          </section>
        </aside>
      </main>

      <footer className="wall-footer">
        <section className="panel wall-running">
          <b className="foot-title">正在执行</b>
          {running.length
            ? running.slice(0, 3).map(action => (
              <div className="run-item" key={action.key}>
                <span className="run-label"><i className="am" aria-hidden="true" /><span title={action.title}>{action.label}</span></span>
                <span className="dm">{action.detail}</span>
              </div>
            ))
            : <span className="dm wall-empty-inline">当前没有正在执行的动作</span>}
          {running.length > 3 && <span className="dm run-more">还有 {running.length - 3} 条</span>}
        </section>

        <section className="panel wall-todo">
          <b className="foot-title">待处理事项</b>
          {model.counts.pending > 0
            ? <>
              <span className="todo-counts">
                <span className="lb">批准 <b className="n or">{model.counts.approve}</b></span>
                <span className="lb">工单 <b className="n or">{model.counts.work_order}</b></span>
                <span className="lb">抽查 <b className="n or">{model.counts.review}</b></span>
                <span className="lb">纠错 <b className="n or">{model.counts.correction}</b></span>
                <span className="lb">回答 <b className="n or">{model.counts.question}</b></span>
                <span className="lb">发布 <b className="n or">{model.counts.publish}</b></span>
              </span>
              <span className="lb todo-total">共 <b className="n or">{model.counts.pending}</b> 件</span>
            </>
            : <span className="dm">暂无待处理事项</span>}
          <button type="button" className="btn on go-desk" onClick={onGoDesk}>去处置席</button>
        </section>
      </footer>
    </div>
  )
}

