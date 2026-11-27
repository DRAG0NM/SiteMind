import { useState, type CSSProperties } from 'react'
import { AgentScreenIcon, BrandMark, DeskScreenIcon, KnowledgeScreenIcon, MetricsScreenIcon, PauseIcon, PlayIcon, WallScreenIcon } from '../shared/icons'
import { dayEnd, dayStart } from '../store'
import { dataDelaySeconds, fmtClock } from './data'
import { PauseBanner } from './PauseBanner'
import { pauseAutonomy, useAutonomy } from './override'
import { PauseDialog } from './screens/Guards'
import type { ConsoleModel, ScreenKey } from './types'

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const TABS: { key: ScreenKey; label: string }[] = [
  { key: 'wall', label: '态势墙' },
  { key: 'agent', label: 'Agent 运行席' },
  { key: 'desk', label: '处置席' },
  { key: 'metrics', label: '数字对比' },
  { key: 'knowledge', label: '资料与图谱' },
]

/** Watchdog 查询结果：多少条受阻，或者接口整个不可用。 */
export interface WatchdogState {
  blocked: number
  unavailable: boolean
}

export interface TopBarProps {
  model: ConsoleModel
  screen: ScreenKey
  onScreen: (screen: ScreenKey) => void
  onPause: () => void
  onTogglePlay: () => void
  onSeek: (at: number) => void
  watchdog: WatchdogState
}

export function TopBar({ model, screen, onScreen, onPause, onTogglePlay, onSeek, watchdog }: TopBarProps) {
  const { day, cursor } = model
  const backendPaused = useAutonomy(state => state.status?.paused)
  const paused = backendPaused ?? model.paused  // 后端在线以后端为准，否则按前端演示替身
  const [pauseOpen, setPauseOpen] = useState(false)
  const start = dayStart(day)
  const end = dayEnd(day)
  const progress = Math.min(100, Math.max(0, ((cursor - start) / Math.max(1, end - start)) * 100))

  // 日界按当天真实偏移算：午夜被夏令时跳过的日子起点是 01:00；终点是次日零点，按 24:00 显示。
  const endLabel = (() => { const text = fmtClock(end, day.timezone); return text === '00:00' ? '24:00' : text })()
  const weekday = (() => {
    const [year, month, date] = day.local_date.split('-').map(Number)
    if (!year || !month || !date) return ''
    return WEEKDAYS[new Date(Date.UTC(year, month - 1, date)).getUTCDay()] ?? ''
  })()

  const latency = day.metrics.find(metric => metric.metric === 'agent_latency_p95_ms')?.value
  const jev = latency == null ? '—' : latency >= 1000 ? `${(latency / 1000).toFixed(1)} s` : `${Math.round(latency)} ms`
  const delay = dataDelaySeconds(day, cursor)
  const delayText = delay == null ? '—' : delay < 90 ? `${delay} s` : `${Math.round(delay / 60)} 分钟`

  const watchdogText = watchdog.unavailable ? 'Watchdog · 回放模式' : watchdog.blocked > 0 ? 'Watchdog 受阻' : 'Watchdog 正常'
  const watchdogHot = watchdog.unavailable || watchdog.blocked > 0

  const icon = (key: ScreenKey) => key === 'wall' ? <WallScreenIcon />
    : key === 'agent' ? <AgentScreenIcon />
      : key === 'desk' ? <DeskScreenIcon />
        : key === 'metrics' ? <MetricsScreenIcon /> : <KnowledgeScreenIcon />

  return (
    <>
    <header className="console-topbar">
      <div className="console-brand">
        <BrandMark size={22} />
        <b>SiteMind</b>
        <span className="lb">控制台</span>
      </div>

      <nav className="console-tabs" role="tablist" aria-label="屏幕">
        {TABS.map(tab => (
          <button key={tab.key} type="button" role="tab" data-screen={tab.key} aria-selected={screen === tab.key}
            className={'tab' + (screen === tab.key ? ' on' : '')} onClick={() => onScreen(tab.key)}>
            {icon(tab.key)}{tab.label}
            {tab.key === 'desk' && model.counts.pending > 0 && <span className="badge">{model.counts.pending}</span>}
          </button>
        ))}
      </nav>

      <div className="console-chips">
        <span className={"chip nexent" + (model.mode === 'live' ? ' live' : '')}><i className="dot-live" aria-hidden="true" />{model.mode === 'live' ? 'Nexent 实时' : 'Nexent 回放'}</span>
        <span className="chip extra">Jev <b className="n">{jev}</b></span>
        <span className="chip extra">数据延迟 <b className="n">{delayText}</b></span>
        <span className={'chip watchdog' + (watchdogHot ? ' hot' : '')}>{watchdogText}</span>
      </div>

      <div className="top-actions">
        <button type="button" className="btn pause-auto" aria-label="暂停自动执行" title="暂停全部自动执行"
          disabled={paused} onClick={() => backendPaused === undefined ? onPause() : setPauseOpen(true)}><PauseIcon size={12} />暂停自动执行</button>

        {model.mode === 'replay' && (
          <div className="replay">
            <button type="button" className="ic play" aria-label={model.playing ? '暂停回放' : '播放回放'}
              data-tip={model.playing ? '暂停回放' : '播放回放'} onClick={onTogglePlay}>
              {model.playing ? <PauseIcon size={12} /> : <PlayIcon size={12} />}
            </button>
            <div className="replay-track">
              <span className="replay-edge replay-start n">{fmtClock(start, day.timezone)}</span>
              <input type="range" aria-label="回放时间" min={start} max={end} step={60000} value={cursor}
                style={{ '--p': `${progress}%` } as CSSProperties}
                onChange={event => onSeek(Number(event.target.value))} />
              <span className="replay-edge replay-end n">{endLabel}</span>
            </div>
          </div>
        )}

        <div className="top-clock">
          <span className="dm">{day.local_date} {weekday}</span>
          <b className="n">{model.now}</b>
        </div>
      </div>
    </header>
    {/* 后端在线时横幅与暂停对话框由这里接管；不在线仍走外壳里的前端演示替身 */}
    <PauseBanner model={model} />
    {pauseOpen && <PauseDialog live onCancel={() => setPauseOpen(false)} onConfirm={reason => { void pauseAutonomy(reason); setPauseOpen(false) }} />}
    </>
  )
}

