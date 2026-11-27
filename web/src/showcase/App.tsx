import { useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react'
import type { CampusSpot } from '../shared/campus'
import { ErrorBoundary } from '../shared/day/ErrorBoundary'
import type { IsoCampusHandle } from '../shared/iso/IsoCampus'
import { EvidenceDrawer, type EvidenceTab } from './components/EvidenceDrawer'
import { PlayerBar } from './components/PlayerBar'
import { StageView } from './components/Stage'
import { TopBar } from './components/TopBar'
import { evidenceFor } from './data/evidence'
import { loadDay, parseDay, SAMPLE_LABEL, type DaySource } from './data/loadDay'
import { onlineRunId } from './data/online'
import { dayEnd, dayStart } from './data/time'
import type { Day } from './data/types'
import { Overview } from './pages/Overview'
import { ComparePage } from './pages/Compare'
import { ElevatorPage } from './pages/Elevator'
import { EnergyPage } from './pages/Energy'
import { EquipmentPage } from './pages/Equipment'
import { HvacPage } from './pages/Hvac'
import { KnowledgePage } from './pages/Knowledge'
import { MainAgentPage } from './pages/MainAgent'
import type { PageProps } from './pages/types'
import type { TabKey } from './tabs'
import { STAGE_SHARE, TOUR_FRAMES, TOUR_INTRO_MS, tourFrameAt, tourStart } from './tour'
import '../shared/theme.css'
import './showcase.css'

type TourMode = 'running' | 'free' | 'done'

/** 自由回放 1× 时，每 100ms 推进的回放时间（300× 实时）。 */
const REPLAY_STEP_MS = 30_000
const TICK_MS = 100

function readFile(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('读取文件失败'))
    reader.readAsText(file)
  })
}

export function App({ initialDay }: { initialDay?: Day } = {}) {
  const [loaded, setLoaded] = useState<{ day: Day; source: DaySource } | null>(() =>
    initialDay ? { day: initialDay, source: { kind: 'sample', label: SAMPLE_LABEL } } : null,
  )
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [importError, setImportError] = useState('')
  const [offline] = useState(() => typeof location !== 'undefined' && location.protocol === 'file:')
  const [onlineRun] = useState(() => onlineRunId())
  const [tab, setTab] = useState<TabKey>('overview')
  const [cursor, setCursor] = useState(() => (initialDay ? tourStart(initialDay) : 0))
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<1 | 2>(1)
  const [selected, setSelected] = useState<CampusSpot | null>(null)
  const [tour, setTour] = useState<{ mode: TourMode; frame: number }>({ mode: 'running', frame: 0 })
  const [elapsed, setElapsed] = useState(0)
  const [evidenceReq, setEvidenceReq] = useState<{ entityId: string | null; title: string; tab: EvidenceTab; ticketId?: string; nonce: number } | null>(null)

  const campusRef = useRef<IsoCampusHandle | null>(null)
  const cursorRef = useRef(cursor)
  cursorRef.current = cursor
  const speedRef = useRef(speed)
  speedRef.current = speed

  useEffect(() => {
    if (initialDay) return
    let alive = true
    loadDay()
      .then(result => {
        if (!alive) return
        setLoaded(result)
        setCursor(tourStart(result.day))
      })
      .catch((reason: unknown) => {
        if (alive) setError(reason instanceof Error ? reason.message : String(reason))
      })
    return () => { alive = false }
  }, [initialDay, attempt])

  const activeFrame = tour.mode === 'running' ? TOUR_FRAMES[tour.frame] : null
  const staged = Boolean(activeFrame?.stageMode && elapsed < activeFrame.durationMs * STAGE_SHARE)
  const intro = tour.mode === 'running' && tour.frame === 0 && elapsed < TOUR_INTRO_MS

  /** 自动巡演：每段切页、推进回放头、停留；段末换下一段。 */
  useEffect(() => {
    if (!loaded || tour.mode !== 'running') return
    const frame = TOUR_FRAMES[tour.frame]
    if (!frame) return
    const to = tourFrameAt(loaded.day, frame)
    const from = cursorRef.current
    setElapsed(0)
    setTab(frame.index === 0 ? 'overview' : frame.tab)
    // 事件段与带 leadMs 的段直接推到分镜时刻，进入时就看得到刚生效的数据；其他段让回放头跟着走。
    const jump = frame.stageMode || frame.leadMs != null
    if (jump) setCursor(to)
    let introPending = frame.index === 0
    let elapsedMs = 0
    const id = window.setInterval(() => {
      elapsedMs += TICK_MS * speedRef.current
      if (introPending && elapsedMs >= TOUR_INTRO_MS) {
        introPending = false
        setTab(frame.tab)
      }
      if (elapsedMs >= frame.durationMs) {
        setElapsed(0)
        setCursor(to)
        setPlaying(false)
        if (tour.frame >= TOUR_FRAMES.length - 1) {
          setTour({ mode: 'done', frame: tour.frame })
        } else {
          const next = TOUR_FRAMES[tour.frame + 1]
          setTab(next.index === 0 ? 'overview' : next.tab)
          setTour({ mode: 'running', frame: tour.frame + 1 })
        }
        return
      }
      setElapsed(elapsedMs)
      if (!jump) setCursor(Math.round(from + (to - from) * Math.min(1, elapsedMs / frame.durationMs)))
    }, TICK_MS)
    return () => window.clearInterval(id)
  }, [loaded, tour])

  /** 自由探索时的播放：1× 为 300× 实时，2× 翻倍。 */
  useEffect(() => {
    if (!loaded || !playing) return
    const end = dayEnd(loaded.day)
    const id = window.setInterval(() => {
      setCursor(current => Math.min(end, current + REPLAY_STEP_MS * speed))
    }, TICK_MS)
    return () => window.clearInterval(id)
  }, [loaded, playing, speed])

  useEffect(() => {
    if (playing && loaded && cursor >= dayEnd(loaded.day)) setPlaying(false)
  }, [playing, loaded, cursor])

  function takeOver() {
    setTour(current => (current.mode === 'running' ? { mode: 'free', frame: current.frame } : current))
    setPlaying(false)
  }

  function seek(at: number) {
    if (!loaded) return
    takeOver()
    setCursor(Math.min(dayEnd(loaded.day), Math.max(dayStart(loaded.day), at)))
    setPlaying(false)
  }

  function togglePlay() {
    if (tour.mode === 'running') { takeOver(); return }
    setPlaying(value => !value)
  }

  function skipFrame() {
    if (!loaded || tour.mode !== 'running') return
    setElapsed(0)
    setCursor(tourFrameAt(loaded.day, TOUR_FRAMES[tour.frame]))
    setTour(tour.frame >= TOUR_FRAMES.length - 1
      ? { mode: 'done', frame: tour.frame }
      : { mode: 'running', frame: tour.frame + 1 })
  }

  function resumeTour() {
    if (!loaded) return
    setElapsed(0)
    setPlaying(false)
    const restart = tour.mode === 'done'
    if (restart) setCursor(tourStart(loaded.day))
    setTour({ mode: 'running', frame: restart ? 0 : tour.frame })
  }

  function openEvidence(entityId: string | null, fallbackTitle: string, drawerTab: EvidenceTab = 'rule', ticketId?: string) {
    setPlaying(false)
    setEvidenceReq(current => ({ entityId, title: fallbackTitle, tab: drawerTab, ticketId, nonce: (current?.nonce ?? 0) + 1 }))
  }

  async function importDay(file: File | undefined) {
    if (!file) return
    try {
      const day = parseDay(JSON.parse(await readFile(file)))
      setLoaded({ day, source: { kind: 'imported', label: `导入 · ${file.name}` } })
      setCursor(tourStart(day))
      setTab('overview')
      setPlaying(false)
      setSelected(null)
      setEvidenceReq(null)
      setElapsed(0)
      setTour({ mode: 'running', frame: 0 })
      setImportError('')
      setError('')
    } catch (reason) {
      setImportError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  function importFrom(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0]
    event.currentTarget.value = ''
    void importDay(file)
  }

  const evidence = useMemo(
    () => (evidenceReq && loaded ? evidenceFor(loaded.day, cursor, evidenceReq.entityId, evidenceReq.title, evidenceReq.ticketId) : null),
    [evidenceReq, loaded, cursor],
  )

  if (error && !loaded) {
    return (
      <div className="boot bootcard">
        <p className="booterror" role="alert">{error}</p>
        <div className="bootactions">
          <label className="importbtn" title="选择导出的 day.json 文件以加载自定义运行记录">
            导入 day.json
            <input type="file" accept="application/json,.json" onChange={importFrom} />
          </label>
          <button className="retrybtn" onClick={() => { setError(''); setAttempt(value => value + 1) }}>重试</button>
        </div>
      </div>
    )
  }
  if (!loaded) return <div className="boot"><span className="spinner" />正在加载 day.json…</div>

  const { day, source } = loaded
  const pageProps: PageProps = {
    day,
    cursor,
    openEvidence,
    focusSpot: spot => { takeOver(); setSelected(spot) },
    source: source.kind,
    sourceLabel: source.label,
  }
  const pages: Record<Exclude<TabKey, 'overview'>, (props: PageProps) => React.ReactElement> = {
    agent: MainAgentPage, knowledge: KnowledgePage, compare: ComparePage,
    elevator: ElevatorPage, energy: EnergyPage, hvac: HvacPage, equipment: EquipmentPage,
  }
  const Page = tab === 'overview' ? null : pages[tab]

  return (
    <div className="showcase">
      <TopBar
        day={day}
        source={source}
        cursor={cursor}
        tab={tab}
        tourIndex={tour.mode === 'running' ? tour.frame : null}
        onTab={key => { takeOver(); setTab(key) }}
        onImport={file => { void importDay(file) }}
        importError={importError}
      />

      {offline && source.kind !== 'imported' && (
        <div className="offlinenote">离线回放模式：点击右上角「导入 day.json」可加载自定义运行记录。</div>
      )}

      {error && (
        // 已经加载出数据时只加一条错误条，不拿错误页或样例替换当前回放。
        <div className="offlinenote" role="alert">
          {error}
          <button type="button" className="retrybtn" onClick={() => { setError(''); setAttempt(value => value + 1) }}>重试</button>
        </div>
      )}

      <main className="stage" onClick={() => { if (tour.mode === 'running') takeOver() }}>
        <ErrorBoundary title="回放渲染出错" resetKey={`${tab}:${day.run_id}`}>
          {staged && activeFrame ? (
            <StageView
              day={day}
              // 舞台态固定在这一幕的分镜时刻；切进来的第一帧回放头还没跳过去，不能用旧时刻
              cursor={tourFrameAt(day, activeFrame)}
              frame={activeFrame}
              campusRef={campusRef}
              onSelect={spot => { takeOver(); setSelected(spot) }}
            />
          ) : tab === 'overview' ? (
            <Overview
              day={day}
              cursor={cursor}
              selected={selected}
              onSelect={spot => { takeOver(); setSelected(spot) }}
              campusRef={campusRef}
              openEvidence={openEvidence}
              tourSpot={tour.mode === 'running' ? TOUR_FRAMES[tour.frame]?.spot ?? null : null}
            />
          ) : Page ? (
            <div className="tabpage" data-page={tab}>
              <Page {...pageProps} />
            </div>
          ) : null}

          {intro && activeFrame && (
            <div className="tour-intro" aria-live="polite">
              <div className="intro-caption">
                <span className="intro-eyebrow">{activeFrame.title} · {activeFrame.hour}</span>
                <b>{activeFrame.camera}</b>
                <p>{activeFrame.stage}</p>
              </div>
            </div>
          )}
        </ErrorBoundary>
      </main>

      <PlayerBar
        day={day}
        cursor={cursor}
        playing={playing}
        speed={speed}
        tourMode={tour.mode}
        tourFrame={tour.frame}
        onToggle={togglePlay}
        onSpeed={setSpeed}
        onSeek={seek}
        onSkip={skipFrame}
        onResumeTour={resumeTour}
      />

      {evidence && evidenceReq && (
        <EvidenceDrawer
          key={evidenceReq.nonce}
          evidence={evidence}
          cursor={cursor}
          timezone={day.timezone}
          initialTab={evidenceReq.tab}
          source={source}
          onlineRunId={onlineRun}
          onClose={() => setEvidenceReq(null)}
        />
      )}
    </div>
  )
}
