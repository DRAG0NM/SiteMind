import { useEffect, useRef, useState } from 'react'
import { CloseIcon } from '../../shared/icons'
import { OnlineRunsHistory, useOnlineRuns } from '../../showcase/components/OnlineRuns'
import type { Evidence } from '../../showcase/data/evidence'
import { refLabel } from '../../showcase/data/labels'
import type { DaySource } from '../../showcase/data/loadDay'
import { clockWithSeconds } from '../../showcase/data/time'

export type EvidenceTab = 'rule' | 'graph' | 'experts' | 'runs'

const TABS: { key: EvidenceTab; label: string }[] = [
  { key: 'rule', label: '规程原文' },
  { key: 'graph', label: '图谱路径' },
  { key: 'experts', label: '专家意见' },
  { key: 'runs', label: 'Agent 运行记录' },
]

const EMPTY = '当日数据没有这一段'

/** 展示页右侧证据抽屉：内容全部来自 evidenceFor 的 rule / notes / graph / experts / runs。 */
export function EvidenceDrawer({ evidence, cursor, timezone, initialTab, onClose, source, onlineRunId }: {
  evidence: Evidence
  cursor: number
  timezone: string
  initialTab: EvidenceTab
  onClose: () => void
  /** 数据来源：只有 backend 才走在线证据链；回退到 day.json 或内置样例时用 day 里的 agent_runs。 */
  source: DaySource
  /** URL 上的运行 ID，只在来源是 backend 时使用。 */
  onlineRunId?: string | null
}) {
  const [tab, setTab] = useState<EvidenceTab>(initialTab)
  const [closing, setClosing] = useState(false)
  const closeRef = useRef<HTMLButtonElement>(null)
  const onlineRun = source.kind === 'backend' ? onlineRunId ?? null : null
  const onlineRuns = useOnlineRuns(onlineRun, timezone, tab === 'runs')
  // 在线接口返回整个业务运行的证据，只展示当前对象已关联且已生效的 Agent 运行。
  const allowedRuns = new Set(evidence.runs.map(run => run.runId))
  const onlineState = onlineRuns.state.state === 'ready'
    ? { ...onlineRuns.state, groups: onlineRuns.state.groups.filter(group => allowedRuns.has(group.runId)) }
    : onlineRuns.state

  useEffect(() => { setTab(initialTab) }, [initialTab, evidence.entityId, evidence.title])

  const close = () => {
    if (closing) return
    setClosing(true)
    window.setTimeout(onClose, 200)
  }

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // close 只读取当前 closing，用 ref 风格即可；依赖省略避免反复绑定
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const empty = <p className="drawer-empty">{EMPTY}</p>
  const subtitle = evidence.subtitle.replace(/^—\s*·\s*/, '').trim()

  // 同一专家可能对同一事件单提过两轮方案：只留一条，优先选中的、有诊断的。
  const experts = (() => {
    const kept = new Map<string, (typeof evidence.experts)[number]>()
    for (const expert of evidence.experts) {
      const current = kept.get(expert.expert)
      if (!current
        || (!current.selected && expert.selected)
        || (current.diagnosis === '—' && expert.diagnosis !== '—')) {
        kept.set(expert.expert, expert)
      }
    }
    return [...kept.values()]
  })()

  return (
    <aside className={'drawer panel' + (closing ? ' closing' : '')} role="dialog" aria-label="证据抽屉">
      <header className="drawer-head">
        <div className="drawer-title">
          <span className="lb">动作依据 · {clockWithSeconds(cursor, timezone)}</span>
          <b>{evidence.title}</b>
          {subtitle && subtitle !== '—' && <span className="dm">{subtitle}</span>}
        </div>
        <button ref={closeRef} type="button" className="ic drawer-close" aria-label="关闭证据抽屉" title="关闭证据抽屉（Esc）" onClick={close}>
          <CloseIcon size={16} />
        </button>
      </header>

      <div className="drawer-tabs" role="tablist" aria-label="证据分页">
        {TABS.map(item => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={tab === item.key}
            className={'drawer-tab' + (tab === item.key ? ' on' : '')}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="drawer-body">
        {tab === 'rule' && (
          <>
            {evidence.rule ? (
              <section className="drawer-section">
                <span className="lb">{evidence.rule.source}</span>
                <p className="ruletext">{evidence.rule.text || EMPTY}</p>
              </section>
            ) : null}
            {evidence.notes.length ? (
              <section className="drawer-section rulebox">
                <span className="lb">这段话变成了哪条判断</span>
                <ul>{evidence.notes.map(note => <li key={note}>{note}</li>)}</ul>
              </section>
            ) : null}
            {!evidence.rule && !evidence.notes.length && empty}
          </>
        )}

        {tab === 'graph' && (
          evidence.graph.length ? (
            <section className="drawer-section">
              <span className="lb">图谱路径</span>
              <div className="graphpath">
                {evidence.graph.map((node, index) => (
                  <span key={node} className="graphstep">
                    {index > 0 && <span className="grapharrow" aria-hidden="true">→</span>}
                    <span className={'graphnode' + (index === 0 ? ' on' : '')}>{refLabel(node)}</span>
                  </span>
                ))}
              </div>
            </section>
          ) : empty
        )}

        {tab === 'experts' && (
          experts.length ? (
            <section className="drawer-section">
              <span className="lb">同事件单的专家研判</span>
              <div className="expertlist">
                {experts.map((expert, index) => (
                  <div key={`${expert.name}-${index}`} className={'expertrow' + (expert.selected ? ' sel' : '')}>
                    <div className="experthead">
                      <b>{expert.name}</b>
                      {expert.selected && <span className="chip hot experttag">选中</span>}
                      <span className="dm experttail">{expert.cost} · {expert.confidence}</span>
                    </div>
                    <p>{expert.diagnosis}</p>
                  </div>
                ))}
              </div>
            </section>
          ) : empty
        )}

        {tab === 'runs' && (
          onlineRun ? (
            <OnlineRunsHistory
              state={onlineState}
              timezone={timezone}
              only={onlineRuns.only}
              onPick={onlineRuns.pick}
              onShowAll={onlineRuns.showAll}
              onRetry={onlineRuns.retry}
            />
          ) : evidence.runs.length ? (
            <section className="drawer-section">
              <span className="lb">Agent 运行记录</span>
              <div className="runlist">
                {evidence.runs.map(run => (
                  <div key={run.runId} className="runrow" title={run.runId}>
                    <div className="runhead">
                      <b>{run.agent}</b>
                      <span className="chip runstatus">{run.status}</span>
                      <span className="n runlatency">触发 {run.trigger} · 耗时 {run.latency}</span>
                    </div>
                    <p className="dm runlink">关联事件单 {run.ticket}</p>
                    {run.nodes.length > 0 && (
                      <div className="agenttree">
                        <span className="lb">Agent 调用层级</span>
                        {run.nodes.map((node, index) => (
                          <div key={`${node.agent}-${index}`} className="agentnode" style={{ paddingLeft: node.depth * 14 }}>
                            <span className={'graphnode' + (node.depth === 0 ? ' on' : '')}>{node.agent}</span>
                            <span className="dm">L{node.depth} · {node.duration}</span>
                          </div>
                        ))}
                      </div>
                    )}
                    {run.tools.length ? (
                      <div className="toollist">
                        {run.tools.map((tool, index) => (
                          <div key={`${tool.tool}-${index}`} className="toolrow">
                            <span className="toolname">{tool.tool}</span>
                            {tool.agent && <span className="dm toolagent">{tool.agent}</span>}
                            <span className="n tooldur">{tool.duration}</span>
                            {tool.status !== '—' && <span className="chip runstatus">{tool.status}</span>}
                            <span className="dm toolsum">{tool.summary}</span>
                          </div>
                        ))}
                      </div>
                    ) : <p className="dm">这次运行没有工具调用记录</p>}
                    {run.finalAnswer && <p className="dm runanswer">最终回答：{run.finalAnswer}</p>}
                  </div>
                ))}
              </div>
            </section>
          ) : empty
        )}
      </div>

      <footer className="drawer-foot">
        <span className="lb">依据引用</span>
        {evidence.refs.length
          ? <span className="refs">{evidence.refs.map(ref => <span key={ref} className="ref" title={ref}>{refLabel(ref)}</span>)}</span>
          : <span className="dm">—</span>}
      </footer>
    </aside>
  )
}
