import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Check, Send } from 'lucide-react'
import { getDay, runAgent } from '../../api'
import { validateDay } from '../../shared/day/validate'
import { useDay, type Day } from '../../store'
import type { ConsoleModel, LaneStepView, StreamRow } from '../types'
import '../agent.css'

const fmtSecond = (at: number, timeZone: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).format(at)
const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`
const TYPE_LABEL: Record<string, string> = { tool_result: '工具结果', tool_running: '工具运行中' }
const TICKS = [0, 1, 2, 3, 4]
/** 块的左右内边距 8×2 + 描边 2（进行中的虚线框 3）。 */
const STEP_PAD = 18
const stepPad = (status: string) => (status === 'running' ? STEP_PAD + 1 : STEP_PAD)
/** jsdom 或字体未就绪时量不到文字宽度，按字宽兜底：汉字一个全角、其它半角。 */
const estimateText = (text: string) =>
  [...text].reduce((sum, char) => sum + (char.charCodeAt(0) > 0x2e7f ? 13 : 7), 0)
const timingOf = (step: LaneStepView) =>
  step.status === 'running' ? '进行中' : step.durationMs > 0 ? seconds(step.durationMs) : ''

/** Agent 运行席：按时间排开的泳道、选中步骤、事件流与向主 Agent 提问。 */
export function AgentRunsScreen({ model }: { model: ConsoleModel }) {
  const { load, ingest, liveAnswer, live } = useDay()
  const timeZone = model.day.timezone
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [follow, setFollow] = useState(true)
  const [runId, setRunId] = useState('')
  const [loadError, setLoadError] = useState('')
  const [loading, setLoading] = useState(false)
  const [query, setQuery] = useState('')
  const [asking, setAsking] = useState(false)
  const [askError, setAskError] = useState('')
  const askInFlight = useRef(false)
  const stepRefs = useRef(new Map<string, HTMLButtonElement>())

  const steps = useMemo(() => model.lanes.flatMap(lane => lane.steps), [model.lanes])
  const newest = useMemo(
    () => steps.reduce<LaneStepView | null>((latest, step) => (!latest || step.start > latest.start ? step : latest), null),
    [steps],
  )
  const selected = selectedId ? steps.find(step => step.id === selectedId) ?? null : follow ? newest : null

  const frame = useMemo(() => {
    if (!steps.length) return null
    let start = Infinity
    let end = -Infinity
    for (const step of steps) {
      start = Math.min(start, step.start)
      end = Math.max(end, step.start + Math.max(0, step.durationMs))
    }
    // 时间轴按本次运行第一笔调用的开始到最后一步结束铺满；零跨度时给 1 秒兜底。
    end = Math.max(end, start + 1000)
    return { start, end, span: end - start }
  }, [steps])
  const head = frame && model.cursor >= frame.start && model.cursor <= frame.end
    ? (model.cursor - frame.start) / frame.span
    : null
  const gatewayEvents = live.filter(event => event.type !== 'keepalive').length

  // 泳道块用像素定位：轨道实际宽度换算时间，文字宽度决定块的最小宽度。
  const [trackWidth, setTrackWidth] = useState(0)
  const [textWidths, setTextWidths] = useState<Record<string, { label: number; timing: number }>>({})
  const trackRef = useRef<HTMLDivElement | null>(null)
  const measureRef = useRef<HTMLDivElement | null>(null)

  useLayoutEffect(() => {
    const track = trackRef.current
    if (!track) return
    const measure = () => {
      const width = track.clientWidth
      if (width > 0) setTrackWidth(width)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const observer = new ResizeObserver(measure)
    observer.observe(track)
    return () => observer.disconnect()
  }, [model.lanes.length, model.lanes[0]?.agent])

  useLayoutEffect(() => {
    const host = measureRef.current
    if (!host) return
    const next: Record<string, { label: number; timing: number }> = {}
    for (const line of host.querySelectorAll<HTMLElement>('[data-step]')) {
      const id = line.dataset.step
      if (!id || textWidths[id]) continue
      const label = line.querySelector<HTMLElement>('.ar-step-tool')
      const timing = line.querySelector<HTMLElement>('.ar-step-dur')
      next[id] = {
        label: label?.getBoundingClientRect().width || estimateText(label?.textContent ?? ''),
        timing: timing ? timing.getBoundingClientRect().width || estimateText(timing.textContent ?? '') : 0,
      }
    }
    if (Object.keys(next).length) setTextWidths(prev => ({ ...prev, ...next }))
  }, [model.lanes, textWidths])

  function select(stepId: string) {
    setFollow(false)
    setSelectedId(stepId)
    stepRefs.current.get(stepId)?.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: 'smooth' })
  }

  function locate(row: StreamRow) {
    if (row.stepId) select(row.stepId)
  }

  async function loadRun() {
    const id = runId.trim()
    if (!id || loading) return
    setLoading(true)
    setLoadError('')
    try { load(validateDay(await getDay(id)) as unknown as Day, '已导出运行 · 只读') }
    catch (error) { setLoadError(error instanceof Error ? error.message : String(error)) }
    finally { setLoading(false) }
  }

  async function ask() {
    const text = query.trim()
    if (!text || asking || askInFlight.current) return
    askInFlight.current = true
    setAsking(true)
    setAskError('')
    try { await runAgent(text, model.day.run_id, model.cursor, ingest) }
    catch (error) { setAskError(error instanceof Error ? error.message : String(error)) }
    finally { askInFlight.current = false; setAsking(false) }
  }

  return (
    <div className="ar">
      <div className="ar-main">
        <section className="panel ar-runs" aria-label="Agent 运行泳道">
          <div className="sec">
            <div className="ar-head">
              <div className="sec-title">本次运行<b className="n ar-runid" title="本次运行 ID">{model.run?.id ?? (model.day.agent_runs.length === 0 ? model.day.run_id : '—')}</b></div>
              <span className="chip" title="快通道 / 慢通道">{model.run?.channel ?? '通道 —'}</span>
              <span className="chip">触发 <b className="n ar-trigger">{model.run?.trigger ?? '—'}</b> · 耗时 <b className="n ar-elapsed">{model.run?.elapsed ?? '—'}</b></span>
              <div className="ar-head-actions">
                {/* ConsoleModel 只给当前一次运行，没有更早运行的切换来源，所以这个按钮保持禁用。 */}
                <button type="button" className="btn" disabled title="当前回放只导出了这一次运行，没有更早的运行记录">上一次运行</button>
                <button
                  type="button"
                  className={'btn' + (follow ? ' on' : '')}
                  disabled={!steps.length}
                  aria-pressed={follow}
                  title="自动看最新的那一步"
                  onClick={() => { setSelectedId(null); setFollow(true) }}
                >跟随最新</button>
                <div className="ar-loadrun">
                  <input
                    value={runId}
                    onChange={event => setRunId(event.target.value)}
                    onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); void loadRun() } }}
                    placeholder="输入运行 ID"
                    aria-label="运行 ID"
                  />
                  <button type="button" className="btn" onClick={() => void loadRun()} disabled={loading || !runId.trim()}>
                    {loading ? '加载中' : '加载'}
                  </button>
                </div>
              </div>
            </div>
            {loadError && <p className="ar-error" role="alert">{loadError}</p>}
          </div>

          {steps.length && frame ? (
            <div className="ar-canvas">
              <div className="ar-axis" aria-hidden="true">
                {TICKS.map(index => (
                  <span
                    key={index}
                    className="n ar-tick"
                    style={index === 0 ? { left: 0 } : index === 4 ? { right: 0 } : { left: `${(index / 4) * 100}%`, transform: 'translateX(-50%)' }}
                  >{fmtSecond(frame.start + (frame.span * index) / 4, timeZone)}</span>
                ))}
              </div>
              <div className="ar-lane-body">
                <div className="ar-overlay" aria-hidden="true">
                  {TICKS.map(index => <i key={index} className="ar-gridline" style={index === 4 ? { right: 0 } : { left: `${(index / 4) * 100}%` }} />)}
                  {head !== null && <i className="ar-now" style={head >= 1 ? { right: 0 } : { left: `${head * 100}%` }} />}
                </div>
                <div className="ar-measure" ref={measureRef} aria-hidden="true">
                  {model.lanes.flatMap(lane => lane.steps).map(step => (
                    <span className="ar-measure-line" key={`m-${step.id}`} data-step={step.id}>
                      <span className="ar-step-tool">{step.label}</span>
                      {timingOf(step) && <span className="ar-step-dur n">{timingOf(step)}</span>}
                    </span>
                  ))}
                </div>
                <div className="ar-lanes">
                  {model.lanes.map((lane, laneIndex) => {
                    // 块按时间排开，最小宽度放得下动作名全文；时间上放不下就向右顺延，
                    // 都放不下时末块左移，宁可两点错开也不截断文字。
                    let spanCursor = 0
                    const layout = [...lane.steps]
                      .sort((a, b) => a.start - b.start)
                      .map(step => {
                        const text = textWidths[step.id]
                        const minWidth = (text?.label ?? estimateText(step.label)) + stepPad(step.status)
                        const natural = (Math.max(0, step.durationMs) / frame.span) * trackWidth
                        const width = Math.max(minWidth, natural)
                        const at = ((step.start - frame.start) / frame.span) * trackWidth
                        const left = trackWidth > 0
                          ? Math.max(0, Math.min(Math.max(spanCursor, at), Math.max(0, trackWidth - width)))
                          : 0
                        spanCursor = left + width + 4
                        const timing = timingOf(step)
                        // 耗时放不下就不显示，全名和耗时留给 title；1px 容差吸收文字宽度的亚像素测量误差。
                        const fits = Boolean(text) && width >= minWidth + 8 + (text?.timing ?? 0) - 1
                        return { step, left, width, timing, fits }
                      })
                    return (
                      <div className="ar-lane" key={lane.agent}>
                        <b className="ar-lane-name" title={lane.label}><span>{lane.label}</span></b>
                        <div className="ar-lane-track" ref={laneIndex === 0 ? trackRef : undefined}>
                          {layout.map(({ step, left, width, timing, fits }) => (
                            <button
                              type="button"
                              key={step.id}
                              ref={node => { if (node) stepRefs.current.set(step.id, node); else stepRefs.current.delete(step.id) }}
                              className={'ar-step' + (step.status === 'running' ? ' run' : '') + (selected?.id === step.id ? ' sel' : '')}
                              style={{ left, width }}
                              title={`${lane.label} · ${step.label} · ${fmtSecond(step.start, timeZone)} · ${timing || '耗时未记录'}${step.estimated ? '（推算）' : ''}`}
                              aria-pressed={selected?.id === step.id}
                              onClick={() => select(step.id)}
                            >
                              <span className="ar-step-inner">
                                <span className="ar-step-tool">{step.label}</span>
                                {fits && timing && <span className="ar-step-dur n">{timing}</span>}
                              </span>
                              {step.estimated && !fits && <span className="ar-step-est" title="没有工具调用的开始时间与耗时记录，按运行耗时推算">推算</span>}
                            </button>
                          ))}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            </div>
          ) : (
            <div className="ar-empty">
              <span>这个时刻没有运行记录</span>
              <span className="dm">把回放推进到有 Agent 运行的时段，这里会按时间排开泳道</span>
            </div>
          )}
        </section>

        <section className="panel ar-stream" aria-label="事件流">
          <div className="sec">
            <div className="sec-title">事件流<span className="dm">实时流原始事件，点一行在泳道里定位</span></div>
          </div>
          {model.stream.length ? (
            <div className="ar-lines">
              {model.stream.map(row => (
                <button
                  type="button"
                  key={row.id}
                  className={'ar-line' + (row.stepId && row.stepId === selected?.id ? ' sel' : '')}
                  title={row.stepId ? '在泳道里定位这一步' : `${row.type}｜${row.summary}`}
                  onClick={() => locate(row)}
                >
                  <b className="n ar-line-time">{fmtSecond(row.at, timeZone)}</b>
                  <span className="n ar-line-type">{TYPE_LABEL[row.type] ?? row.type}</span>
                  <span className="ar-line-agent">{row.agent}</span>
                  <span className="ar-line-sum">{row.summary}</span>
                </button>
              ))}
            </div>
          ) : <div className="ar-empty">这个时刻没有事件</div>}
        </section>
      </div>

      <aside className="ar-side">
        <section className="panel ar-detail" aria-label="选中步骤">
          {selected ? (
            <>
              <div className="sec">
                <div className="sec-title">选中步骤<span className="ar-tool">{selected.label}</span></div>
                <div className="ar-kv">
                  <div><span className="lb">开始</span><b className="n">{fmtSecond(selected.start, timeZone)}</b></div>
                  <div><span className="lb">耗时</span><b className="n">{selected.status === 'running' ? '进行中' : selected.durationMs > 0 ? seconds(selected.durationMs) : '—'}</b></div>
                  <div><span className="lb">工具</span><b className="ar-kv-tool">{selected.tool}</b></div>
                </div>
                {selected.estimated && <p className="dm ar-est-note">没有工具调用记录，时间与耗时按运行记录推算</p>}
              </div>
              <div className="sec">
                <span className="lb">输入</span>
                <p className="ar-text">{selected.input || '—'}</p>
              </div>
              <div className="sec">
                <span className="lb">比较</span>
                {selected.compare.length ? selected.compare.map((row, index) => (
                  <div className={'ar-cmp' + (row.chosen ? ' chosen' : '')} key={`${row.label}-${index}`}>
                    <span>{row.label}</span>
                    <span className={'ar-cmp-note' + (row.chosen ? '' : ' dm')}>{row.note}</span>
                  </div>
                )) : <p className="dm ar-none">这一步没有比较项</p>}
              </div>
              <div className="sec">
                <span className="lb">结果</span>
                <p className="ar-text">{selected.result || '—'}</p>
              </div>
              <div className="sec ar-btns">
                <button type="button" className="btn" disabled title="这一步只记录了输入摘要，没有完整入参原文">看完整入参</button>
                <button type="button" className="btn" disabled title="这一步没有关联的依据原文">看依据原文</button>
              </div>
            </>
          ) : (
            <div className="sec">
              <div className="sec-title">选中步骤<span className="dm">—</span></div>
              <p className="dm ar-none">点泳道里的块，这里显示这一步的输入、比较和结果</p>
            </div>
          )}
        </section>

        <section className="panel ar-foot" aria-label="后台预演与提问">
          <div className="sec">
            <div className="sec-title">后台预演</div>
            {model.rehearsal?.ok ? (
              <div className="ar-rehearsal"><Check size={18} aria-hidden="true" /><span>{model.rehearsal.text}</span></div>
            ) : <p className="dm ar-none">{model.rehearsal?.text ?? '这次处置没有对应的预演记录'}</p>}
          </div>
          <div className="sec">
            <div className="sec-title">向主 Agent 提问</div>
            <div className="ar-ask">
              <input
                value={query}
                onChange={event => setQuery(event.target.value)}
                onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); void ask() } }}
                placeholder="向主 Agent 提问"
                aria-label="向主 Agent 提问"
              />
              <button type="button" className="btn" aria-label="发送" disabled={asking || !query.trim()} onClick={() => void ask()}>
                <Send size={14} aria-hidden="true" />{asking ? '发送中' : '发送'}
              </button>
            </div>
            {askError && <p className="ar-error" role="alert">{askError}</p>}
            {liveAnswer && <div className="ar-answer">{liveAnswer}</div>}
            <p className="dm ar-gateway">网关事件 <b className="n">{gatewayEvents}</b> 条{asking ? ' · 运行中' : ''}</p>
          </div>
        </section>
      </aside>
    </div>
  )
}
