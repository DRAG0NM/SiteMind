import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'motion/react'
import { CloseIcon } from '../../shared/icons'
import { prefersReducedMotion } from '../../shared/motion'
import { useDay } from '../../store'
import { NatureBadge } from '../common/nature'
import { useRemote } from '../common/remote'
import {
  buildSyntheticView, loadEvidence,
  type EvidenceFragment, type EvidenceRow, type EvidenceSubject, type EvidenceView,
} from './evidenceData'
import './evidence.css'

const KIND_LABEL: Record<NonNullable<EvidenceSubject['kind']>, string> = {
  queue: '待办依据',
  action: '动作依据',
  work_order: '工单依据',
}

/**
 * 控制台里的证据入口：按钮自己管开关，点开后从右侧弹出四段证据抽屉。
 * 数据链在 evidenceData 里，真实接口读不到时退回当天记录合成，抽屉里标出数据性质与退回原因。
 */
export function EvidenceButton({ subject, className, label }: {
  subject: EvidenceSubject
  className?: string
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  const reduced = prefersReducedMotion()

  return (
    <>
      <button
        type="button"
        className={'btn evidence-open' + (className ? ` ${className}` : '')}
        onClick={() => setOpen(true)}
      >
        {label ?? '查看证据'}
      </button>
      {typeof document !== 'undefined' && createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              key="evidence-overlay"
              className="evidence-overlay"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: reduced ? 0 : 0.18 }}
              onMouseDown={event => { if (event.target === event.currentTarget) close() }}
            >
              <EvidencePanel subject={subject} onClose={close} />
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  )
}

function EvidencePanel({ subject, onClose }: { subject: EvidenceSubject; onClose: () => void }) {
  const day = useDay(state => state.day)
  const cursor = useDay(state => state.cursor)
  const closeRef = useRef<HTMLButtonElement>(null)
  const remote = useRemote(
    () => loadEvidence(subject, day),
    () => buildSyntheticView(day, cursor, subject),
    [subject.entityId, subject.actionId, subject.runId, subject.title],
  )

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const view = remote?.data
  const reduced = prefersReducedMotion()
  const path = view?.path ?? 'reasoning'

  return (
    <motion.aside
      className={`evidence-panel path-${path}`}
      role="dialog"
      aria-modal="true"
      aria-label="证据抽屉"
      initial={{ x: reduced ? 0 : 32 }}
      animate={{ x: 0 }}
      exit={{ x: reduced ? 0 : 24 }}
      transition={{ duration: reduced ? 0 : 0.18, ease: 'easeOut' }}
    >
      <header className="evidence-head">
        <div className="evidence-title">
          <span className="lb">{KIND_LABEL[subject.kind ?? 'queue']}</span>
          <b>{subject.title}</b>
          {view && (
            <div className="evidence-meta">
              <span
                className={`chip evidence-path ${view.path}`}
                title={view.path === 'retrieval' ? '检索类依据（冷蓝）' : '推理类依据（暖橙）'}
              >
                {view.path === 'retrieval' ? '检索' : '推理'}
              </span>
              <NatureBadge nature={view.nature} source={view.natureSource} />
            </div>
          )}
        </div>
        <button
          ref={closeRef}
          type="button"
          className="ic evidence-close"
          aria-label="关闭证据抽屉"
          title="关闭证据抽屉（Esc）"
          onClick={onClose}
        >
          <CloseIcon size={16} />
        </button>
      </header>

      {remote?.note && <p className="remote-note evidence-fallback">{remote.note}</p>}
      {!view && <p className="evidence-empty">正在读取证据…</p>}

      {view && (
        <>
          <div className="evidence-body">
            <EvidenceSection index={1} title="模型填值" hint="模型提出时自己填的参数">
              {view.model.length
                ? <ValueList rows={view.model} />
                : <p className="evidence-empty">{view.real ? '后端没有记录模型填值' : '演示记录里没有模型填值'}</p>}
            </EvidenceSection>

            <EvidenceSection index={2} title="系统绑定值" hint="后端派发时绑定的运行、版本与评分">
              {view.bound.length
                ? <ValueList rows={view.bound} />
                : <p className="evidence-empty">{view.real ? '后端没有返回系统绑定值' : '演示记录里没有系统绑定值'}</p>}
            </EvidenceSection>

            <EvidenceSection index={3} title="图谱路径" hint="这条判断经过的对象与依据">
              <GraphList
                nodes={view.graph}
                empty={view.real ? '后端没有返回图谱路径' : '演示记录里没有图谱路径'}
              />
            </EvidenceSection>

            <EvidenceSection index={4} title="原文片段" hint="资料与规则的原文，可回到出处">
              <FragmentList
                fragments={view.fragments}
                empty={view.real ? '后端没有返回原文片段' : '演示记录里没有可引用的原文'}
              />
            </EvidenceSection>
          </div>

          <footer className="evidence-foot">
            <span className="lb">数据来源</span>
            <span className="dm">{view.real ? view.origin : '仿真演示场景记录'}</span>
            {subject.actionId && (
              <span className="evidence-action n" title={subject.actionId}>{subject.actionId}</span>
            )}
          </footer>
        </>
      )}
    </motion.aside>
  )
}

function EvidenceSection({ index, title, hint, children }: {
  index: number
  title: string
  hint: string
  children: ReactNode
}) {
  return (
    <section className="evidence-section" aria-label={title}>
      <div className="evidence-sechead">
        <span className="evidence-index n">{index}</span>
        <b>{title}</b>
        <span className="dm">{hint}</span>
      </div>
      {children}
    </section>
  )
}

function ValueList({ rows }: { rows: EvidenceRow[] }) {
  return (
    <dl className="evidence-kv">
      {rows.map((row, index) => (
        <div className="evidence-kvrow" key={`${row.label}-${index}`}>
          <dt>{row.label}</dt>
          <dd className="n" title={row.value}>{row.value}</dd>
        </div>
      ))}
    </dl>
  )
}

function GraphList({ nodes, empty }: { nodes: string[]; empty: string }) {
  if (!nodes.length) return <p className="evidence-empty">{empty}</p>
  return (
    <div className="evidence-graph">
      {nodes.map((node, index) => (
        <span className="evidence-step" key={`${node}-${index}`}>
          {index > 0 && <span className="evidence-arrow" aria-hidden="true">→</span>}
          <span className={'evidence-node' + (index === 0 ? ' on' : '')} title={node}>{node}</span>
        </span>
      ))}
    </div>
  )
}

function FragmentList({ fragments, empty }: { fragments: EvidenceFragment[]; empty: string }) {
  if (!fragments.length) return <p className="evidence-empty">{empty}</p>
  return (
    <div className="evidence-fragments">
      {fragments.map((fragment, index) => (
        <article className="evidence-fragment" key={`${fragment.id}-${index}`}>
          <div className="evidence-fraghead">
            <span className="evidence-locator">{fragment.locator}</span>
            {fragment.kind && <span className="dm">{fragment.kind}</span>}
          </div>
          <p>{fragment.text}</p>
        </article>
      ))}
    </div>
  )
}

/** 供测试或别的视图直接用面板数据；按钮已包含开关，一般不用这个。 */
export type { EvidenceView }
