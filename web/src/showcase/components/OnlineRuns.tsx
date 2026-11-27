import { useEffect, useRef, useState } from 'react'
import { buildEvidenceRuns, failureReason, fetchEvidence, type EvidenceRunGroup } from '../data/online'
import { clockWithSeconds } from '../data/time'

export type OnlineRunsState =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'error'; reason: string; retryOnly: EvidenceRunGroup | null }
  | { state: 'ready'; groups: EvidenceRunGroup[] }

/**
 * 在线模式的运行记录状态：首次点开「Agent 运行记录」页签时向后端要一次证据链，
 * 切走页签再回来沿用已有结果。请求与分组映射都在 data/online.ts。
 * StrictMode 只发一次；快速切组时 abort 上一请求，慢响应回来也不会覆盖新结果。
 */
export function useOnlineRuns(runId: string | null, timezone: string, enabled: boolean) {
  const [state, setState] = useState<OnlineRunsState>({ state: 'idle' })
  const [only, setOnly] = useState<EvidenceRunGroup | null>(null)
  const requestRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const startedRef = useRef(false)
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false }
  }, [])

  async function load(target: EvidenceRunGroup | null) {
    if (!runId) return
    const request = ++requestRef.current
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setState({ state: 'loading' })
    try {
      const items = await fetchEvidence(runId, target?.runId ?? null, fetch, controller.signal)
      if (!aliveRef.current || requestRef.current !== request) return
      const groups = buildEvidenceRuns(items, timezone)
      setState({
        state: 'ready',
        // 只看一次运行重取时，仍沿用点开那组的序号与开始时刻。
        groups: target && groups[0] ? [{ ...groups[0], ordinal: target.ordinal, startedAt: target.startedAt }] : groups,
      })
    } catch (reason) {
      if (!aliveRef.current || requestRef.current !== request || controller.signal.aborted) return
      // 只看某组的请求失败时回到全部范围，重试或看全部都不再卡在坏组上。
      setState({ state: 'error', reason: failureReason(reason), retryOnly: target })
      setOnly(null)
    }
  }

  useEffect(() => {
    if (enabled && runId && !startedRef.current) {
      startedRef.current = true
      void load(null)
    }
    // 只在第一次点开页签时拉取；runId、timezone 随抽屉打开固定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, runId])

  return {
    state,
    only,
    pick: (group: EvidenceRunGroup) => {
      setOnly(group)
      void load(group)
    },
    showAll: () => {
      setOnly(null)
      void load(null)
    },
    retry: () => { void load(state.state === 'error' ? state.retryOnly : only) },
  }
}

/** 「Agent 运行记录」在线内容：按运行分组，组标题只写第几次、开始时刻和记录条数。 */
export function OnlineRunsHistory({ state, timezone, only, onPick, onShowAll, onRetry }: {
  state: OnlineRunsState
  timezone: string
  only: EvidenceRunGroup | null
  onPick: (group: EvidenceRunGroup) => void
  onShowAll: () => void
  onRetry: () => void
}) {
  if (state.state !== 'ready') {
    if (state.state === 'error') {
      return (
        <div className="onlineerror">
          <p className="drawer-empty">{state.reason}</p>
          <div className="onlineactions">
            {state.retryOnly && <button type="button" className="btn allruns" onClick={onShowAll}>看全部</button>}
            <button type="button" className="btn" onClick={onRetry}>重试</button>
          </div>
        </div>
      )
    }
    return <p className="drawer-empty onlineloading"><span className="spinner" />正在读取运行记录…</p>
  }
  if (!state.groups.length) {
    return (
      <div className="onlineempty">
        <p className="drawer-empty">后端还没有这次运行的 Agent 记录</p>
        {only && <button type="button" className="btn allruns" onClick={onShowAll}>看全部</button>}
      </div>
    )
  }

  return (
    <section className="drawer-section">
      <span className="lb">Agent 运行记录</span>
      {only && <button type="button" className="btn allruns" onClick={onShowAll}>看全部</button>}
      <div className="runlist">
        {state.groups.map(group => (
          <div key={group.runId || group.startedAt} className="runrow onlinerun">
            <button
              type="button"
              className="rungroup"
              disabled={Boolean(only) || !group.runId}
              onClick={() => onPick(group)}
            >
              {group.runId
                ? `第 ${group.ordinal} 次运行 · ${clockWithSeconds(Date.parse(group.startedAt), timezone)} 开始 · ${group.events.length} 条记录`
                : `这次运行 · ${clockWithSeconds(Date.parse(group.startedAt), timezone)} 起 · ${group.events.length} 条证据`}
            </button>
            <div className="eventlist">
              {group.events.map((event, index) => (
                <div key={`${event.time}-${index}`} className="eventrow">
                  <div className="eventhead">
                    <span className="n eventtime">{event.time}</span>
                    <b className="eventname">{event.event}</b>
                    {event.tool && <span className="eventtool">{event.tool}</span>}
                    <span className="chip runstatus">{event.status}</span>
                  </div>
                  {event.summary && <p className="eventsum">{event.summary}</p>}
                  {event.refs.length > 0 && (
                    <div className="eventrefs">
                      <span className="lb">依据</span>
                      {event.refs.map(ref => <span key={ref} className="ref">{ref}</span>)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
