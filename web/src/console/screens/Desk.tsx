import { useEffect, useMemo, useState } from 'react'
import { confirmAction, getActions, rejectAction, type PendingActionRaw } from '../../api'
import { EvidenceButton } from '../../features/evidence'
import { useRemote } from '../../features/common/remote'
import { ReviewCard } from '../../features/review/ReviewCards'
import { useDay, type Verdict } from '../../store'
import { actionQueueItems, fmtClock } from '../data'
import {
  cancelApplied, getRevokePreview, matchApplied, pauseAutonomy, pausedMinutes, resumeAutonomy, revokeApplied, useAutonomy,
  type AppliedAction,
} from '../override'
import type { ConsoleActionView, ConsoleModel, QueueItemView, QueueKind } from '../types'
import { PauseDialog, RetractDialog, type RetractLive, type RetractResult } from './Guards'
import '../desk.css'

const KIND_ORDER: QueueKind[] = ['approve', 'work_order', 'review', 'correction', 'question', 'publish']
const KIND_LABEL: Record<QueueKind, string> = {
  approve: '批准', work_order: '工单', review: '抽查', correction: '纠错', question: '回答', publish: '发布',
}

function verdictLabel(item: QueueItemView) {
  if (!item.verdict) return ''
  if (item.verdict === 'approved') {
    if (item.kind === 'work_order') return '已到场确认'
    if (item.kind === 'correction') return '已提交纠错'
    if (item.kind === 'question') return '已回答'
    return '已批准'
  }
  if (item.verdict === 'sampled') return '已抽查通过'
  if (item.verdict === 'published') return '已发布'
  if (item.kind === 'review') return '已要求复核'
  if (item.kind === 'publish') return '已暂缓'
  if (item.kind === 'correction') return '已驳回纠错'
  if (item.kind === 'question') return '已驳回'
  return '已驳回'
}

function idempotencyKey(item: QueueItemView) {
  const safe = item.id.replace(/[^a-zA-Z0-9:_-]/g, '-')
  return `desk-${safe}-${Date.now()}`
}

/** 处置席：左边按紧急程度处理的队列，中间是选中件，右边是正在执行的控制与自动执行开关。 */
export function DeskScreen({ model }: { model: ConsoleModel }) {
  const retract = useDay(state => state.retract)
  const recordVerdict = useDay(state => state.recordVerdict)
  const cancelRule = useDay(state => state.cancelRule)
  const verdicts = useDay(state => state.verdicts)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [demoNote, setDemoNote] = useState('')
  /** 前端处置记录：拒绝请求不能带修订，抽查卡勾选的“不是系统判断有误”只记在这里（#234） */
  const [systemErrorNotes, setSystemErrorNotes] = useState<Record<string, boolean>>({})
  /** 真实动作失败的原因，按条目记住：切换选中不清除，重试成功后清掉 */
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({})
  const [pauseOpen, setPauseOpen] = useState(false)
  const [retractTarget, setRetractTarget] = useState<{ action: ConsoleActionView; backend: AppliedAction | null; live: RetractLive | null } | null>(null)
  /** 撤销、取消或暂停/恢复请求失败的原因：这件事没有生效，原因留在侧栏 */
  const [controlError, setControlError] = useState('')
  const autonomy = useAutonomy(state => state.status)
  const applied = useAutonomy(state => state.applied)
  const refreshAutonomy = useAutonomy(state => state.refresh)
  const backendPaused = autonomy?.paused ?? model.paused
  // 真实接口优先：/actions 有同一类待确认动作时以真接口为准，失败或没有该类时保留 day 记录合成的演示卡片
  const pendingActions = useRemote<PendingActionRaw[]>(() => getActions(), () => [], [])

  const queue = useMemo(() => {
    const resolve = (item: QueueItemView): QueueItemView => ({ ...item, verdict: verdicts[item.id] ?? item.verdict })
    const live = pendingActions?.live ? actionQueueItems(pendingActions.data) : []
    if (live.length === 0) return model.queue.map(resolve)
    const liveKinds = new Set(live.map(item => item.kind))
    return [...live.map(resolve), ...model.queue.filter(item => !liveKinds.has(item.kind)).map(resolve)]
      .sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.at - b.at)
  }, [model.queue, pendingActions, verdicts])

  const selected = queue.find(item => item.id === selectedId) ?? null
  const pendingCount = queue.filter(item => !item.verdict).length

  useEffect(() => {
    if (selectedId && queue.some(item => item.id === selectedId)) return
    setSelectedId(queue.find(item => !item.verdict)?.id ?? queue[0]?.id ?? null)
  }, [queue, selectedId])

  useEffect(() => {
    setDemoNote('')
  }, [selectedId])

  useEffect(() => { void refreshAutonomy() }, [refreshAutonomy])

  /** 点“撤销/取消”：对得上后端已下发动作就走真接口（撤销先试算预计影响），否则是前端演示替身。 */
  async function openRetract(action: ConsoleActionView) {
    const backend = matchApplied(action, applied)
    if (!backend) { setRetractTarget({ action, backend: null, live: null }); return }
    const stage = backend.stage === 'scheduled' ? 'scheduled' : 'active'
    const base: RetractLive = { stage, protection: backend.protection, preview: null, error: '' }
    setRetractTarget({ action, backend, live: base })
    if (stage === 'scheduled') return
    try {
      const preview = await getRevokePreview(backend.action_id, backend.run_id)
      setRetractTarget(current => current && current.backend === backend ? { ...current, live: { ...base, preview } } : current)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setRetractTarget(current => current && current.backend === backend ? { ...current, live: { ...base, error: `预计影响读取失败：${message}` } } : current)
    }
  }

  /** 取消临时规则：后端认得这条规则（TR- 编号）就按撤销处理；后端没有它（404）退回前端演示替身。 */
  async function cancelRuleLive(ruleId: string) {
    if (!autonomy) { cancelRule(ruleId); return }
    try {
      await revokeApplied(ruleId, 'other', '值班取消临时规则', false)
      setControlError('')
      cancelRule(ruleId)
      await refreshAutonomy()
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (message.includes('(404)')) cancelRule(ruleId)
      else setControlError(`取消临时规则没有生效：${message}`)
    }
  }

  async function confirmRetract(result: RetractResult) {
    const target = retractTarget
    setRetractTarget(null)
    if (!target) return
    const { action, backend } = target
    if (!backend) { retract(action.key); return }
    try {
      if (backend.stage === 'scheduled') await cancelApplied(backend.action_id, result.note, result.confirmProtection, backend.run_id)
      else await revokeApplied(backend.action_id, result.reasonKind, result.note, result.confirmProtection, backend.run_id)
      setControlError('')
      retract(action.key)
      await refreshAutonomy()
    } catch (error) {
      setControlError(`${backend.stage === 'scheduled' ? '取消' : '撤销'}没有生效：${error instanceof Error ? error.message : String(error)}`)
    }
  }

  const groups = useMemo(() => KIND_ORDER.map(kind => ({
    kind,
    items: queue.filter(item => item.kind === kind),
    live: queue.filter(item => item.kind === kind && !item.verdict).length,
  })), [queue])

  async function decide(item: QueueItemView, verdict: Verdict, via: 'confirm' | 'reject' | null, reason: string,
                        meta?: { systemError?: boolean }) {
    if (busy || item.verdict) return
    setBusy(true)
    const actionId = item.actionId
    const real = Boolean(actionId) && via !== null
    if (real) {
      try {
        // ActionDecisionRequest 只有 reason/expected_revision/idempotency_key；拒绝端不接受 edits（#234）
        await (via === 'confirm' ? confirmAction : rejectAction)(actionId!, {
          reason, expected_revision: item.revision ?? 0, idempotency_key: idempotencyKey(item),
        })
      } catch (error) {
        // 真实动作没生效：不记裁定，保留这一条待处理，失败原因留到重试成功为止
        const message = error instanceof Error ? error.message : String(error)
        setActionErrors(errors => ({ ...errors, [item.id]: `后端接口失败：${message}。这一条仍待处理，可重试。` }))
        setBusy(false)
        return
      }
      setActionErrors(errors => {
        if (!(item.id in errors)) return errors
        const next = { ...errors }
        delete next[item.id]
        return next
      })
    }
    if (meta?.systemError) setSystemErrorNotes(notes => ({ ...notes, [item.id]: true }))
    recordVerdict(item.id, verdict)
    setDemoNote(real ? '' : '当前为离线演示模式，裁定结果已在本地界面记录。')
    setBusy(false)
  }

  const confirmCard = (item: QueueItemView, reason: string) =>
    decide(item, item.kind === 'review' ? 'sampled' : 'approved', 'confirm', reason)
  const rejectCard = (item: QueueItemView, reason: string, meta?: { systemError?: boolean }) =>
    decide(item, 'rejected', 'reject', reason, meta)

  const hasBackend = Boolean(selected?.actionId)
  const demoText = '当前为离线演示模式，审批操作仅在本地界面生效。'

  return (
    <div className="desk">
      <section className="panel desk-col desk-left" aria-label="待处理事项">
        <div className="sec desk-head">
          <div className="sec-title">待处理事项<span className="dm desk-sub">{pendingCount} 件 · 按优先级排序</span></div>
        </div>
        <div className="desk-scroll desk-queue">
          {queue.length === 0 && <div className="desk-empty">没有待处理的事</div>}
          {queue.length > 0 && groups.map(group => (group.items.length > 0 || group.kind === 'publish') && (
            <div className="desk-group" key={group.kind}>
              <div className="desk-grouphead">
                <span>{KIND_LABEL[group.kind]}</span>
                <span className="n desk-groupcount">{group.live}</span>
              </div>
              {group.items.map(item => (
                <button
                  type="button"
                  key={item.id}
                  className={'desk-item' + (item.id === selectedId ? ' on' : '') + (item.verdict ? ' done' : '')}
                  aria-pressed={item.id === selectedId}
                  onClick={() => setSelectedId(item.id)}
                >
                  <span className="desk-itemtop">
                    <span className="chip desk-chip">{KIND_LABEL[item.kind]}</span>
                    <b>{item.title}</b>
                    {item.verdict && <span className="desk-decided">{verdictLabel(item)}</span>}
                  </span>
                  <span className="dm desk-itemsub">{item.subtitle}</span>
                  {item.synthetic && <span className="desk-demo">离线演示</span>}
                  {item.kind === 'work_order' && item.title.startsWith('Watchdog') && (
                    <span className="dm desk-itemsub">恢复：{item.facts.find(fact => fact.label === '恢复')?.value ?? '—'}</span>
                  )}
                </button>
              ))}
              {group.kind === 'publish' && group.items.length === 0 && (
                <div className="desk-empty desk-empty-publish">没有待发布的知识更新。第三幕复盘后会出现在这里。</div>
              )}
              {group.kind !== 'publish' && group.items.length === 0 && <div className="desk-none-line">这一类暂时没有事项</div>}
            </div>
          ))}
        </div>
      </section>

      <section className="panel desk-col desk-center" aria-label="选中件">
        {!selected ? (
          <div className="desk-none">从左边选一件处理</div>
        ) : (
          <>
            <div className="sec desk-midhead">
              <div className="desk-midheadtop">
                <span className="chip desk-chip">{KIND_LABEL[selected.kind]}</span>
                <EvidenceButton
                  subject={{
                    title: selected.title,
                    entityId: selected.entityId,
                    actionId: selected.actionId,
                    runId: model.day.run_id,
                    kind: selected.kind === 'work_order' ? 'work_order' : 'queue',
                  }}
                  className="desk-small"
                  label="查看证据"
                />
              </div>
              <h2>{selected.title}</h2>
              <div className="lb">{selected.subtitle}</div>
            </div>
            <div className="desk-scroll desk-mid">
              <div className="sec">
                <span className="lb">触发原因</span>
                <p className="desk-copy">{selected.detail}</p>
              </div>
              <div className="sec">
                <span className="lb">{selected.kind === 'approve' ? '执行影响' : '处理影响'}</span>
                <div className="desk-facts">
                  {selected.facts.map(fact => (
                    <div key={fact.label} className={fact.value.length > 22 ? 'wide' : undefined}>
                      <span className="lb">{fact.label}</span>
                      <b className="n">{fact.value}</b>
                    </div>
                  ))}
                  {!hasBackend && selected.facts.length < 3 && (
                    <div className="desk-factnote">
                      <span className="lb">演示模式</span>
                      <b>只在前端记录</b>
                    </div>
                  )}
                </div>
                {selected.facts.length === 0 && <p className="dm desk-copy">这一条没有可用的权衡数据，处理结果会记入处置记录。</p>}
                {!hasBackend && (
                  <p className="desk-demo-note">
                    <span className="desk-demo">离线演示</span>
                    <span>{demoText}</span>
                  </p>
                )}
              </div>
              <div className="sec desk-basis">
                <span className="lb">判断依据</span>
                <ul className="desk-notes">
                  {selected.notes.map((note, index) => <li key={`${index}-${note}`}>{note}</li>)}
                  {selected.notes.length === 0 && <li className="dm">这一条没有额外的判断依据。</li>}
                </ul>
                <div className="desk-source">
                  <span className="dm">来源</span>
                  <span className="n">{selected.source}</span>
                  <span className="dm">处理结果写入处置记录</span>
                </div>
              </div>
            </div>
            <div className="sec desk-actbar">
              {selected.verdict && (
                <div className="desk-result">
                  <span>{verdictLabel(selected)}（可回看，不再计入待办）</span>
                  {systemErrorNotes[selected.id] && <span className="desk-demo">不是系统判断有误 · 仅前端处置记录</span>}
                  {(demoNote || !hasBackend) && <span className="desk-demo">离线演示</span>}
                </div>
              )}
              {demoNote && <p className="dm desk-resultnote">{demoNote}</p>}
              {actionErrors[selected.id] && (
                <p className="dm desk-resultnote desk-error" role="alert">{actionErrors[selected.id]}</p>
              )}
              {!selected.verdict && !hasBackend && (
                <p className="desk-demo-note">
                  <span className="desk-demo">离线演示</span><span>{demoText}</span>
                </p>
              )}
              {selected.kind === 'publish' ? (
                !selected.verdict && (
                  <div className="desk-buttons">
                    <button type="button" className="btn go desk-action" disabled={busy} onClick={() => void decide(selected, 'published', 'confirm', `发布知识更新：${selected.title}`)}>发布</button>
                    <button type="button" className="btn desk-action" disabled={busy} onClick={() => void decide(selected, 'rejected', 'reject', `暂缓发布：${selected.title}`)}>暂缓</button>
                    <span className="dm desk-hint">有真实动作编号时走确认 / 驳回接口</span>
                  </div>
                )
              ) : (
                <ReviewCard key={selected.id} item={selected} busy={busy} confirm={confirmCard} reject={rejectCard} />
              )}
            </div>
          </>
        )}
      </section>

      <div className="desk-side">
        <section className="panel desk-sidepanel" aria-label="正在执行的控制">
          <div className="sec desk-head">
            <div className="sec-title">正在执行的控制<span className="dm desk-sub">{model.actions.length} 条</span></div>
          </div>
          <div className="desk-scroll desk-sidebody">
            {model.actions.length === 0 && <div className="desk-none-line">当前没有正在执行的控制</div>}
            {controlError && <p className="dm desk-resultnote desk-error" role="alert">{controlError}</p>}
            {model.actions.map(action => (
              <div className={'desk-control' + (action.retracted ? ' done' : '')} key={action.key}>
                <i className="am" aria-hidden="true" />
                <div className="desk-controlmain">
                  <div>{action.label}</div>
                  <div className="dm">{action.detail}</div>
                </div>
                {action.retracted ? (
                  <span className="desk-state">已撤销 <span className="n">{model.now}</span></span>
                ) : (
                  <span className="desk-controlact">
                    <EvidenceButton
                      subject={{ title: action.label, entityId: action.entityId, runId: model.day.run_id, kind: 'action' }}
                      className="desk-small"
                      label="证据"
                    />
                    {matchApplied(action, applied) ? (
                      <button type="button" className="btn desk-small" onClick={() => void openRetract(action)}>
                        {matchApplied(action, applied)?.stage === 'scheduled' ? '取消' : '撤销'}
                      </button>
                    ) : (
                      <>
                        <span className="desk-demo" title="离线演示模式下，撤销操作仅在本地界面生效">离线演示</span>
                        <button type="button" className="btn desk-small" onClick={() => void openRetract(action)}>撤销</button>
                      </>
                    )}
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>

        <section className="panel desk-sidepanel" aria-label="临时规则">
          <div className="sec desk-head">
            <div className="sec-title">临时规则<span className="dm desk-sub">{model.rules.length} 条</span></div>
          </div>
          <div className="desk-scroll desk-sidebody">
            {model.rules.length === 0 && <div className="desk-none-line">当前没有生效中的临时规则</div>}
            {model.rules.map(rule => (
              <div className={'desk-control' + (rule.cancelled ? ' done' : '')} key={rule.id}>
                <div className="desk-controlmain">
                  <div>{rule.text}</div>
                  <div className="dm">到 {rule.until} 失效</div>
                </div>
                {rule.cancelled ? (
                  <span className="desk-state">已取消</span>
                ) : (
                  <span className="desk-controlact">
                    <span className="desk-demo" title="离线演示模式下，取消操作仅在本地界面生效">离线演示</span>
                    <button type="button" className="btn desk-small" onClick={() => void cancelRuleLive(rule.id)}>取消</button>
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>

        <section className={'panel desk-sidepanel desk-auto' + (backendPaused ? ' paused' : '')} aria-label="自动执行">
          <div className="sec">
            <div className="sec-title">自动执行<span className={backendPaused ? 'desk-state or' : 'desk-state desk-running'}>{backendPaused ? '已暂停' : '运行中'}</span></div>
            {backendPaused && autonomy && autonomy.paused && (
              <div className="dm desk-paused">
                由 {autonomy.by ?? '值班人员'} 暂停 · {autonomy.since ? fmtClock(Date.parse(autonomy.since), model.day.timezone) : '—'}
                {' '}· 已暂停 {pausedMinutes(autonomy, autonomy.scope === 'run' ? model.cursor : Date.now()) ?? '—'} 分钟 · 积压 {autonomy.backlog} 条
              </div>
            )}
            {backendPaused && !autonomy && model.pausedAt !== null && (
              <div className="dm desk-paused">由值班人员暂停 · {fmtClock(model.pausedAt, model.day.timezone)}</div>
            )}
            {autonomy && !autonomy.paused && autonomy.backlog > 0 && (
              <div className="dm desk-paused">已恢复 · 还有 {autonomy.backlog} 条积压的提议待逐条批准</div>
            )}
            <p className="desk-copy">暂停后 Agent 只提建议，不再自己下发。预案里的保护动作照常执行，变压器不会失去保护。恢复后积压的提议要逐条批准。</p>
            <button
              type="button"
              className={'btn desk-wide' + (backendPaused ? '' : ' on')}
              aria-label={backendPaused ? '恢复自动执行' : '暂停自动执行'}
              onClick={() => { if (backendPaused) void resumeAutonomy(); else setPauseOpen(true) }}
            >
              {backendPaused ? '恢复自动执行' : '暂停自动执行'}
            </button>
            {!autonomy && (
              <p className="desk-demo-note"><span className="desk-demo">离线演示</span>当前为离线演示模式，暂停与恢复仅在本地界面生效。</p>
            )}
          </div>
        </section>
      </div>

      {pauseOpen && (
        <PauseDialog
          live={autonomy !== null}
          onCancel={() => setPauseOpen(false)}
          onConfirm={reason => { void pauseAutonomy(reason); setPauseOpen(false) }}
        />
      )}
      {retractTarget && (
        <RetractDialog
          label={retractTarget.action.label}
          impact={retractTarget.action.impact}
          live={retractTarget.live ?? undefined}
          onCancel={() => setRetractTarget(null)}
          onConfirm={result => void confirmRetract(result)}
        />
      )}
    </div>
  )
}
