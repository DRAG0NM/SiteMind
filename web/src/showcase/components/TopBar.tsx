import { useRef } from 'react'
import { BrandMark } from '../../shared/icons'
import { clock, dayAtHour } from '../data/time'
import type { Day } from '../data/types'
import type { DaySource } from '../data/loadDay'
import { TABS, type TabKey } from '../tabs'
import { TOUR_FRAMES } from '../tour'

/** 顶栏：品牌、八个标签页、巡演计数、数据来源、导入 day.json、日期与时钟。 */
export function TopBar({ day, source, cursor, tab, tourIndex, onTab, onImport, importError }: {
  day: Day
  source: DaySource
  cursor: number
  tab: TabKey
  /** 巡演中的段序号（0 起）；null 表示自由探索 */
  tourIndex: number | null
  onTab: (tab: TabKey) => void
  onImport: (file: File | undefined) => void
  importError: string
}) {
  const weekday = new Intl.DateTimeFormat('zh-CN', { timeZone: day.timezone, weekday: 'short' })
    .format(dayAtHour(day, 12))
  const fileRef = useRef<HTMLInputElement>(null)

  return (
    <header className="topbar">
      <div className="brand" title="SiteMind · 设施智能体">
        <BrandMark size={22} />
        <b>SiteMind</b>
      </div>

      <nav className="tabs" role="tablist" aria-label="展示页标签页">
        {TABS.map(item => (
          <button
            key={item.key}
            type="button"
            role="tab"
            data-tab={item.key}
            aria-selected={tab === item.key}
            className={'tab' + (tab === item.key ? ' on' : '')}
            onClick={() => onTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <div className="topright">
        <span className={'chip tourchip' + (tourIndex !== null ? ' on' : '')}
          aria-live="polite" data-tour={tourIndex !== null ? tourIndex + 1 : 'free'}>
          {tourIndex !== null ? `自动巡演 ${tourIndex + 1} / ${TOUR_FRAMES.length}` : '自由探索'}
        </span>

        {tourIndex !== null && TOUR_FRAMES[tourIndex] && (
          <span className="tourtitle" title={`镜头：${TOUR_FRAMES[tourIndex].camera} · 舞台：${TOUR_FRAMES[tourIndex].stage} · 落点：${TOUR_FRAMES[tourIndex].landing}`}>
            {TOUR_FRAMES[tourIndex].title} · {TOUR_FRAMES[tourIndex].hour}
          </span>
        )}

        <span className="chip source" title={`数据来源 · ${source.label}`}>{source.label}</span>
        {day.qa.some(item => item.answer.startsWith('【模拟预设回答】')) && (
          <span className="chip offlinenotice" role="status">离线仿真 · 预设问答</span>
        )}
        {source.note && <span className="sourcenote" title={source.note}>{source.note}</span>}

        <button
          type="button"
          className="btn importbtn"
          title="选择导出的 day.json 文件回放"
          onClick={() => fileRef.current?.click()}
        >
          导入 day.json
        </button>
        <input
          ref={fileRef}
          className="fileinput"
          type="file"
          accept="application/json,.json"
          aria-label="导入 day.json"
          onChange={event => {
            const file = event.currentTarget.files?.[0]
            event.currentTarget.value = ''
            onImport(file)
          }}
        />

        {importError && <span className="importerror" role="alert" title={importError}>{importError}</span>}

        <div className="now" title={`${day.local_date} · ${day.timezone}`}>
          <small>{day.local_date} {weekday}</small>
          <b className="n">{clock(cursor, day.timezone)}</b>
        </div>
      </div>
    </header>
  )
}

