/** B35 五类处置卡片：操作工单、纠错、抽查、一键批准、回答。数据由 Desk 传入，卡片只管交互与就地展示。 */
import { useState } from 'react'
import { getEvolutionTimeline, type EvolutionEntryRaw } from '../../api'
import type { QueueItemView } from '../../console/types'
import { useDay, type Day } from '../../store'
import { NatureBadge } from '../common/nature'
import './review.css'

export interface CardHandlers {
  busy: boolean
  /** 确认：批准 / 判对 / 到场确认 / 提交纠错 / 提交回答。真实动作失败时 Desk 不记裁定，保留这一条待处理。 */
  confirm: (item: QueueItemView, reason: string) => Promise<void>
  /** 驳回：判错 / 退回 / 暂缓。meta 只进前端处置记录，不进确认端点请求体。 */
  reject: (item: QueueItemView, reason: string, meta?: { systemError?: boolean }) => Promise<void>
}

export type CardProps = { item: QueueItemView } & CardHandlers

const show = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value)?.slice(0, 160) ?? ''

function evolutionSummary(entry: EvolutionEntryRaw): string {
  const details = entry.details ?? {}
  const parts: string[] = []
  for (const key of ['outcome', 'candidate_id', 'candidate', 'status', 'result', 'note']) {
    if (details[key] !== undefined && details[key] !== null) parts.push(`${key === 'candidate_id' ? '候选' : key} ${show(details[key])}`)
  }
  return `${entry.event} · ${entry.occurred_at} · ${parts.length ? parts.join(' · ') : show(details)}`
}

/**
 * 这条进化记录是不是本次纠错的。确认纠错卡走 confirm_from_pending → record_feedback，时间线写的是
 * feedback_recorded（ticket_id、candidate_id 常为 null、outcome、system_error）；确认接口不回 candidate_id。
 * 所以取“提交前最大 seq 之后新增的、ticket_id 等于本事件单”的 feedback_recorded；旧记录不算本次结果（#248-4）。
 */
function isThisCorrection(item: QueueItemView, entry: EvolutionEntryRaw, beforeSeq: number): boolean {
  const ticket = item.payload?.ticket_id
  return entry.event === 'feedback_recorded' && entry.seq > beforeSeq
    && typeof ticket === 'string' && entry.details?.ticket_id === ticket
}

/** 演示模式没有进化接口时，只引用演示日记录里那条纠错沉淀，不编造候选。 */
function demoEvolution(day: Day): string {
  const version = [...day.knowledge_versions].reverse().find(item => (item.summary ?? '').includes('纠错'))
  return version
    ? `离线演示模式：本次纠错将沉淀为知识 v${version.version}「${version.summary}」`
    : '离线演示模式：纠错记录已提交，待进化模块处理。'
}

/** 一句话纠正 + 可选 steps + 附件一起组成确认原因；确认接口只收一段文字，附件正文按资料名写进去。 */
function correctionReason(text: string, steps: string, files: { name: string; text: string }[]): string {
  const parts = [text]
  const lines = steps.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  if (lines.length) parts.push(`排查步骤：${lines.join(' → ')}`)
  for (const file of files) parts.push(`附资料《${file.name}》：${file.text}`)
  return parts.join('；')
}

/** 操作工单：区分伤亡与重大损失；处置动作确认后才下发，保护动作只显示确实执行过的。 */
export function WorkOrderCard({ item, busy, confirm, reject }: CardProps) {
  const locked = busy || item.verdict !== null
  const source = item.workOrderSource ?? 'other'
  const sourceLabel = source === 'casualty' ? '伤亡（人员安全）' : source === 'major_loss' ? '重大损失（试算超线）' : '其他'
  const disposal = item.disposalActions ?? []
  const protections = item.protections ?? []
  return (
    <div className="rv">
      <div className="rv-head">
        <span className="chip desk-chip">操作工单</span>
        <b className={source === 'casualty' ? 'or' : undefined}>{sourceLabel}</b>
        {item.synthetic && <NatureBadge nature="synthetic" />}
      </div>
      {disposal.length > 0 && (
        <div className="rv-block">
          <span className="lb">确认后执行的处置动作</span>
          <ul className="rv-list">
            {disposal.map(action => <li key={action}>{action}</li>)}
          </ul>
          <p className="rv-note">这些动作确认后才下发；退回则不执行。</p>
        </div>
      )}
      <div className="rv-block">
        <span className="lb">已自动执行的预案保护动作{item.protectionsSynthetic && <NatureBadge nature="synthetic" />}</span>
        {protections.length > 0
          ? <ul className="rv-list">{protections.map(protection => <li key={protection}>{protection}</li>)}</ul>
          : <p className="rv-note">后端没有返回已自动执行的预案保护动作，这里不补写。</p>}
      </div>
      <div className="rv-actions">
        <button type="button" className="btn go desk-action" disabled={locked} onClick={() => void confirm(item, `值班已到场确认：${item.title}`)}>已到场确认</button>
        <button type="button" className="btn desk-action" disabled={locked} onClick={() => void reject(item, `值班退回：${item.title}`)}>退回</button>
        <span className="dm">确认后回填处置结果</span>
      </div>
    </div>
  )
}

/** 纠错：低置信事件，一句话纠正 + 可选 steps + 可附资料；提交后只显示本次纠错的进化记录。 */
export function CorrectionCard({ item, busy, confirm }: CardProps) {
  const day = useDay(state => state.day)
  const locked = busy || item.verdict !== null
  const [correction, setCorrection] = useState('')
  const [steps, setSteps] = useState('')
  const [files, setFiles] = useState<{ name: string; text: string }[]>([])
  const [fileName, setFileName] = useState('')
  const [fileText, setFileText] = useState('')
  const [result, setResult] = useState('')
  const [fallback, setFallback] = useState('')

  function addFile() {
    const text = fileText.trim()
    if (!text) return
    setFiles(list => [...list, { name: fileName.trim() || `资料-${list.length + 1}.txt`, text }])
    setFileName('')
    setFileText('')
  }

  async function submit() {
    const text = correction.trim()
    if (!text || busy) return
    let beforeSeq = 0
    let beforeFailed = false
    try {
      for (const entry of await getEvolutionTimeline()) beforeSeq = Math.max(beforeSeq, entry.seq)
    } catch { beforeFailed = true }
    await confirm(item, correctionReason(text, steps, files))
    if (beforeFailed) {
      // 不知道提交前的位置就没法分辨哪条是本次的：不做匹配，如实写读取失败
      setResult('')
      setFallback(item.actionId ? '已提交；读取进化记录失败，无法确认本次纠错的进化结果。' : demoEvolution(day))
      return
    }
    try {
      const entries = await getEvolutionTimeline(beforeSeq)
      const mine = entries.filter(entry => isThisCorrection(item, entry, beforeSeq))
      const latest = mine[mine.length - 1]
      setFallback('')
      setResult(latest
        ? `本次纠错的进化记录：${evolutionSummary(latest)}`
        : '已提交，等待进化记录。')
    } catch {
      setResult('')
      // 演示模式没有进化接口：不拿别的记录冒充本次结果
      setFallback(item.actionId ? '已提交；进化时间线读取失败，等待进化引擎处理。' : demoEvolution(day))
    }
  }

  return (
    <div className="rv">
      <div className="rv-head">
        <span className="chip desk-chip">纠错</span>
        <b>用一句话纠正低置信判断</b>
        {item.synthetic && <NatureBadge nature="synthetic" />}
      </div>
      <div className="rv-form">
        <input className="rv-input" aria-label="一句话纠正" value={correction} onChange={event => setCorrection(event.target.value)}
          placeholder="例如：先查配电回路，照明与空压同时异常多半是同一路电源" />
        <textarea className="rv-area" aria-label="排查步骤" value={steps} onChange={event => setSteps(event.target.value)}
          placeholder="可选：排查步骤，一行一步（会沉淀成调查 Skill）" />
        <div className="rv-row">
          <input className="rv-input" aria-label="资料文件名" value={fileName} onChange={event => setFileName(event.target.value)}
            placeholder="资料文件名（可选）" />
          <button type="button" className="btn desk-small" disabled={locked || !fileText.trim()} onClick={addFile}>附资料</button>
        </div>
        <textarea className="rv-area" aria-label="资料内容" value={fileText} onChange={event => setFileText(event.target.value)}
          placeholder="资料正文（可选）：贴一段原文或现场说明" />
        {files.length > 0 && (
          <ul className="rv-list">
            {files.map((file, index) => <li key={`${index}-${file.name}`}>{file.name} · {file.text.slice(0, 60)}{file.text.length > 60 ? '…' : ''}</li>)}
          </ul>
        )}
        <div className="rv-actions">
          <button type="button" className="btn go desk-action" disabled={locked || !correction.trim()} onClick={() => void submit()}>提交纠错</button>
          <span className="dm">纠正与附资料一起写进确认原因，接口只收一段文字</span>
        </div>
        {result && <p className="rv-result" role="status">{result}</p>}
        {fallback && <p className="rv-result" role="status">{fallback}</p>}
      </div>
    </div>
  )
}

/** 抽查：现有 review 项复用；判对=确认，判错=驳回。 */
export function SamplingCard({ item, busy, confirm, reject }: CardProps) {
  const locked = busy || item.verdict !== null
  const [systemError, setSystemError] = useState(false)
  // 演示替身按复核项处理；真实动作只有 review / review_decision 才允许勾选
  const supportsEdits = item.backendKind ? item.backendKind === 'review' || item.backendKind === 'review_decision' : item.actionId === null
  const checked = supportsEdits && systemError
  return (
    <div className="rv">
      <div className="rv-head">
        <span className="chip desk-chip">抽查</span>
        <b>{item.backendKind === 'sampling' ? '判对 / 判错（抽检）' : '判对 / 判错（复核）'}</b>
        {item.synthetic && <NatureBadge nature="synthetic" />}
      </div>
      <label className="rv-check">
        <input type="checkbox" checked={systemError} disabled={!supportsEdits || locked}
          onChange={event => setSystemError(event.target.checked)} />
        <span>不是系统判断有误</span>
      </label>
      <p className="rv-note">该选项仅对复核项生效，勾选状态将同步记录至处置日志。</p>
      <div className="rv-actions">
        <button type="button" className="btn go desk-action" disabled={locked} onClick={() => void confirm(item, `抽查通过：${item.title}`)}>判对（抽查通过）</button>
        <button type="button" className="btn desk-action" disabled={locked}
          onClick={() => void reject(item, `抽查不通过：${item.title}`, checked ? { systemError: true } : undefined)}>判错（要复核）</button>
      </div>
      {item.verdict === 'rejected' && systemError && (
        <p className="rv-result">「非系统判断有误」已记录至本地处置日志。</p>
      )}
    </div>
  )
}

/** 一键批准：L0 / 待批准方案，批准或驳回，原因可选。 */
export function ApproveCard({ item, busy, confirm, reject }: CardProps) {
  const locked = busy || item.verdict !== null
  const [reason, setReason] = useState('')
  const note = reason.trim()
  return (
    <div className="rv">
      <div className="rv-head">
        <span className="chip desk-chip">批准</span>
        <b>批准或驳回这一条</b>
        {item.synthetic && <NatureBadge nature="synthetic" />}
      </div>
      <div className="rv-form">
        <input className="rv-input" aria-label="批准原因" value={reason} onChange={event => setReason(event.target.value)}
          placeholder="原因可选，例如：延长到 09:30" />
        <div className="rv-actions">
          <button type="button" className="btn go desk-action" disabled={locked}
            onClick={() => void confirm(item, note || `值班批准：${item.title}`)}>批准</button>
          <button type="button" className="btn desk-action" disabled={locked}
            onClick={() => void reject(item, note || `值班驳回：${item.title}`)}>驳回</button>
          <span className="dm">填写说明将随审批记录保存</span>
        </div>
      </div>
    </div>
  )
}

/** 回答：主 Agent 只问一个问题，答案原文经确认端点回写。 */
export function QuestionCard({ item, busy, confirm }: CardProps) {
  const locked = busy || item.verdict !== null
  const [answer, setAnswer] = useState('')
  const question = typeof item.payload?.question === 'string' && item.payload.question.trim()
    ? item.payload.question
    : item.title
  return (
    <div className="rv">
      <div className="rv-head">
        <span className="chip desk-chip">回答</span>
        <b>主 Agent 只问这一个问题</b>
        {item.synthetic && <NatureBadge nature="synthetic" />}
      </div>
      <p className="rv-question">{question}</p>
      <div className="rv-form">
        <textarea className="rv-area" aria-label="回答" value={answer} onChange={event => setAnswer(event.target.value)}
          placeholder="例如：有人，已被困 5 分钟；维保已到现场" />
        <div className="rv-actions">
          <button type="button" className="btn go desk-action" disabled={locked || !answer.trim()} onClick={() => void confirm(item, answer.trim())}>提交回答</button>
          <span className="dm">提交后将同步回写给主 Agent 继续本轮研判</span>
        </div>
      </div>
    </div>
  )
}

export function ReviewCard(props: CardProps) {
  switch (props.item.kind) {
    case 'work_order': return <WorkOrderCard {...props} />
    case 'correction': return <CorrectionCard {...props} />
    case 'review': return <SamplingCard {...props} />
    case 'approve': return <ApproveCard {...props} />
    case 'question': return <QuestionCard {...props} />
    default: return null
  }
}
