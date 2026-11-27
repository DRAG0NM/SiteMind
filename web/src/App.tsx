import { useEffect, useState } from 'react'
import { getWatchdog, watchdogBackoff, type WatchdogTicket } from './api'
import { fmtClock, useConsole } from './console/data'
import { TopBar } from './console/TopBar'
import { AgentRunsScreen } from './console/screens/AgentRuns'
import { DeskScreen } from './console/screens/Desk'
import { PauseDialog } from './console/screens/Guards'
import { WallScreen } from './console/screens/Wall'
import type { ScreenKey } from './console/types'
import { KnowledgeDropScreen } from './features/knowledge-drop/KnowledgeDropScreen'
import { MetricsScreen } from './features/metrics/MetricsScreen'
import { ErrorBoundary } from './shared/day/ErrorBoundary'
import { PauseIcon } from './shared/icons'
import { useDay } from './store'
import './shared/theme.css'
import './console/console.css'

const HASH_SCREEN: Record<string, ScreenKey> = {
  wall: 'wall', agent: 'agent', desk: 'desk', metrics: 'metrics', knowledge: 'knowledge',
}
const KEY_SCREEN: Record<string, ScreenKey> = { '1': 'wall', '2': 'agent', '3': 'desk', '4': 'metrics', '5': 'knowledge' }

const screenFromHash = (): ScreenKey => HASH_SCREEN[window.location.hash.slice(1)] ?? 'wall'

/** 控制台外壳：顶栏、三屏切换、暂停横幅与回放推进。 */
export function App() {
  const day = useDay(state => state.day)
  if (!day) return <div className="console-loading">正在加载运行数据…</div>
  return <Console />
}

function Console() {
  const [screen, setScreen] = useState<ScreenKey>(screenFromHash)
  const [pauseOpen, setPauseOpen] = useState(false)
  const [watchdog, setWatchdog] = useState<WatchdogTicket[]>([])
  const [watchdogDown, setWatchdogDown] = useState(false)
  const model = useConsole(watchdog)
  const tick = useDay(state => state.tick)
  const seek = useDay(state => state.seek)
  const play = useDay(state => state.play)
  const setPaused = useDay(state => state.setPaused)

  // Watchdog 挂载时查一次；502/404（后端或网关没起）时退避，间隔从 60 秒逐步拉到 5 分钟，恢复后回到 60 秒。
  // 连续失败只打一次 console.debug，不在控制台每轮刷红字；界面继续显示“Watchdog 暂不可用”。
  useEffect(() => {
    let active = true
    let timer: number | undefined
    let failures = 0
    const refresh = () => {
      getWatchdog()
        .then(items => { if (active) { failures = 0; setWatchdog(items); setWatchdogDown(false); schedule() } })
        .catch((reason: unknown) => {
          if (!active) return
          setWatchdogDown(true)
          if (watchdogBackoff(reason)) {
            failures += 1
            if (failures === 1) console.debug('Watchdog 暂不可用，轮询间隔逐步退避到 5 分钟')
          } else {
            failures = 0
          }
          schedule()
        })
    }
    const schedule = () => { timer = window.setTimeout(refresh, Math.min(failures * 60000, 300000) || 60000) }
    refresh()
    return () => { active = false; window.clearTimeout(timer) }
  }, [])

  useEffect(() => {
    if (!model.playing) return
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [model.playing, tick])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) return
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) return
      const next = KEY_SCREEN[event.key]
      if (!next) return
      event.preventDefault()
      setScreen(next)
      window.history.replaceState(null, '', `#${next}`)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    const onHash = () => setScreen(screenFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (next: ScreenKey) => {
    setScreen(next)
    window.history.replaceState(null, '', `#${next}`)
  }
  const pausedMinutes = model.pausedAt == null ? null : Math.max(0, Math.round((model.cursor - model.pausedAt) / 60000))

  return (
    <div className="console">
      <TopBar model={model} screen={screen} onScreen={go} onPause={() => setPauseOpen(true)}
        onTogglePlay={() => play(!model.playing)} onSeek={seek}
        watchdog={{ blocked: watchdog.length, unavailable: watchdogDown }} />

      {model.paused && (
        <div className="console-banner" role="status" aria-live="polite">
          <PauseIcon size={13} />
          <b>自动执行已暂停</b>
          <span className="banner-text">
            {model.pausedAt == null ? '—' : fmtClock(model.pausedAt, model.day.timezone)} 由值班人员暂停 · 已转为只提议 {pausedMinutes ?? '—'} 分钟 · 积压 {model.counts.approve} 条待批准
          </span>
          <button type="button" className="btn go banner-restore" onClick={() => setPaused(false)}>恢复自动执行</button>
        </div>
      )}

      <div className="console-body">
        <div className="console-screen" key={screen}>
          <ErrorBoundary title="控制台渲染出错" resetKey={`${screen}:${model.day.run_id}`}>
            {screen === 'wall' && <WallScreen model={model} onGoDesk={() => go('desk')} />}
            {screen === 'agent' && <AgentRunsScreen model={model} />}
            {screen === 'desk' && <DeskScreen model={model} />}
            {screen === 'metrics' && <MetricsScreen />}
            {screen === 'knowledge' && <KnowledgeDropScreen />}
          </ErrorBoundary>
        </div>
      </div>

      {pauseOpen && <PauseDialog onCancel={() => setPauseOpen(false)} onConfirm={() => { setPaused(true); setPauseOpen(false) }} />}
    </div>
  )
}
