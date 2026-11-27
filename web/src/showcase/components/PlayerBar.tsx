import { useMemo } from 'react'
import { PauseIcon, PlayIcon, RestoreIcon, SkipIcon } from '../../shared/icons'
import { hourActivity, knowledgeMarkers, latestKnowledge } from '../data/selectors'
import { chapterAt, chapters } from '../data/rollup'
import { clock, dayAtHour, dayEnd, dayStart, time } from '../data/time'
import type { Day } from '../data/types'

const pad = (value: number) => String(value).padStart(2, '0')
const clamp01 = (value: number) => Math.min(1, Math.max(0, value))

export type TourMode = 'running' | 'free' | 'done'

export function PlayerBar({ day, cursor, playing, speed, tourMode, tourFrame, onToggle, onSpeed, onSeek, onSkip, onResumeTour }: {
  day: Day
  cursor: number
  playing: boolean
  speed: 1 | 2
  tourMode: TourMode
  tourFrame: number
  onToggle: () => void
  onSpeed: (speed: 1 | 2) => void
  onSeek: (at: number) => void
  onSkip: () => void
  onResumeTour: () => void
}) {
  const start = dayStart(day)
  const end = dayEnd(day)
  const span = Math.max(1, end - start)
  const position = clamp01((cursor - start) / span)
  const currentHour = Math.min(23, Math.floor(position * 24))
  const hours = useMemo(() => hourActivity(day), [day])
  const chapterNow = chapterAt(day, cursor)
  const chaptersList = chapters()
  const knowledge = latestKnowledge(day, cursor)
  const markers = useMemo(
    () => knowledgeMarkers(day).filter(version => {
      const at = time(version.activated_at)
      return at >= start && at < end
    }),
    [day, start, end],
  )
  const touring = tourMode === 'running'

  return (
    <footer className="player" aria-label="回放控制">
      <div className="playctl">
        <button
          type="button"
          className="ic playbtn"
          aria-label={touring ? '暂停巡演' : playing ? '暂停' : '播放'}
          title={touring ? '暂停巡演，转为自由探索' : playing ? '暂停回放' : '播放回放'}
          onClick={onToggle}
        >
          {touring || playing ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
        </button>
        <div className="playtime">
          <b className="n">{clock(cursor, day.timezone)}</b>
          <span>{day.local_date} · {day.timezone}</span>
        </div>
      </div>

      <div className="ptimeline">
        <div className="pchapters" role="group" aria-label="场景章节">
          {chaptersList.map(chapter => {
            const on = chapter.key === chapterNow.key
            return (
              <div
                key={chapter.key}
                className={'pchapter' + (on ? ' on' : '')}
                style={{ flexGrow: chapter.to - chapter.from }}
                title={`${chapter.index} · ${chapter.name} · ${pad(chapter.from)}:00–${pad(chapter.to)}:00`}
              >
                <b>{chapter.index}</b>
                <span>{chapter.name}</span>
              </div>
            )
          })}
        </div>

        <div className="pmarkers" aria-label="知识版本激活时刻">
          {markers.map(version => (
            <button
              key={version.version}
              type="button"
              className={'knowledge-mark' + (knowledge?.version === version.version ? ' on' : '')}
              style={{ left: `${clamp01((time(version.activated_at) - start) / span) * 100}%` }}
              aria-label={`跳到知识 v${version.version} 激活时刻`}
              title={`知识 v${version.version} · ${clock(time(version.activated_at), day.timezone)} 激活 · ${version.summary ?? '未记录摘要'}`}
              onClick={() => onSeek(time(version.activated_at))}
            >
              v{version.version}
            </button>
          ))}
        </div>

        <div className="phours" role="group" aria-label="按小时跳转">
          {hours.map(hour => {
            const tone = hour.count === 0 ? 'idle' : hour.maxSeverity >= 4 ? 'hot' : hour.maxSeverity >= 3 ? 'warm' : 'low'
            const state = hour.hour === currentHour ? 'current' : hour.hour < currentHour ? 'past' : 'future'
            return (
              <button
                key={hour.hour}
                type="button"
                className={`hour ${tone} ${state}`}
                title={`${pad(hour.hour)}:00 · ${hour.count ? `${hour.count} 个事件 · 最高严重度 ${hour.maxSeverity}` : '无事件'}`}
                aria-label={`跳到 ${pad(hour.hour)}:00`}
                onClick={() => onSeek(dayAtHour(day, hour.hour))}
              >
                {pad(hour.hour)}
              </button>
            )
          })}
          <div className="cursorline" style={{ left: `${position * 100}%` }} aria-hidden="true" />
        </div>

        <div className="pticks" aria-hidden="true">
          <span>00</span><span>06</span><span>12</span><span>18</span><span>24</span>
        </div>
      </div>

      <div className="ptail">
        <button
          type="button"
          className="knowledge-badge"
          onClick={() => onSeek(knowledge ? time(knowledge.activated_at) : cursor)}
          title={knowledge?.summary ?? '当前还没有生效的知识版本'}
          aria-label={`当前知识 v${knowledge?.version ?? '—'}，跳到激活时刻`}
        >
          <span className="gem" aria-hidden="true" />
          知识 v{knowledge?.version ?? '—'}
        </button>

        <div className="speedctl" role="group" aria-label="播放倍速">
          {([1, 2] as const).map(value => (
            <button
              key={value}
              type="button"
              className={'speedbtn' + (speed === value ? ' on' : '')}
              aria-pressed={speed === value}
              onClick={() => onSpeed(value)}
            >
              {value}×
            </button>
          ))}
        </div>

        <button
          type="button"
          className="btn skipbtn"
          onClick={onSkip}
          disabled={!touring}
          title={touring ? (tourFrame >= 7 ? '结束巡演' : `跳到第 ${tourFrame + 2} 段`) : '自由探索时没有可跳过的段落'}
        >
          <SkipIcon size={14} />跳过本段
        </button>

        {touring ? (
          <span className="playerhint">点击画面切换自由探索</span>
        ) : (
          <button type="button" className="btn on returnbtn" onClick={onResumeTour}>
            <RestoreIcon size={15} />{tourMode === 'done' ? '重新巡演' : '回到剧情'}
          </button>
        )}
      </div>
    </footer>
  )
}
