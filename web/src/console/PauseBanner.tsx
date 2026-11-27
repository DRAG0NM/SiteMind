import { useEffect } from 'react'
import { PauseIcon } from '../shared/icons'
import { fmtClock } from './data'
import { pausedMinutes, resumeAutonomy, useAutonomy } from './override'
import type { ConsoleModel } from './types'

/** 暂停横幅：后端在线时显示谁暂停的、暂停了多久、积压几条；后端不在线退回前端演示替身。 */
export function PauseBanner({ model }: { model: ConsoleModel }) {
  const status = useAutonomy(state => state.status)
  const refresh = useAutonomy(state => state.refresh)
  const error = useAutonomy(state => state.error)

  // 挂载时读一次，之后每 5 秒刷新：别的值班人员暂停或恢复，这里也能跟上
  useEffect(() => {
    void refresh()
    const id = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(id)
  }, [refresh])

  const failed = error ? <div className="console-banner" role="alert"><b>操作没有生效</b><span className="banner-text">{error}</span></div> : null
  if (status === null) return failed  // 后端不在线：没有错误时交给外壳的前端演示横幅
  const paused = status.paused
  if (!paused) return failed

  const since = status.since ? Date.parse(status.since) : null
  const minutes = pausedMinutes(status, status.scope === 'run' ? model.cursor : Date.now())
  const who = status.by ?? '值班人员'

  return (
    <>
    {failed}
    <div className="console-banner" role="status" aria-live="polite">
      <PauseIcon size={13} />
      <b>自动执行已暂停</b>
      <span className="banner-text">
        {since == null ? '—' : fmtClock(since, model.day.timezone)} 由 {who} 暂停
        {status.reason ? `（${status.reason}）` : ''} · 已转为只提议 {minutes ?? '—'} 分钟 · 积压 {status.backlog} 条待批准
      </span>
      <button type="button" className="btn go banner-restore" onClick={() => void resumeAutonomy()}>恢复自动执行</button>
    </div>
    </>
  )
}
