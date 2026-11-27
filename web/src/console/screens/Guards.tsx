import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { ReasonKind, RevokePreview } from '../override'
import '../desk.css'

type Close = (after?: () => void) => void

type ModalProps = {
  title: string
  onCancel: () => void
  children: (close: Close) => ReactNode
}

/** 遮罩 + 面板：Esc、点遮罩、Tab 循环与进出场都在这里统一处理。 */
function Modal({ title, onCancel, children }: ModalProps) {
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<number | null>(null)
  const closingRef = useRef(false)
  const cancelRef = useRef(onCancel)
  cancelRef.current = onCancel
  const [closing, setClosing] = useState(false)

  const close = (after?: () => void) => {
    if (closingRef.current) return
    closingRef.current = true
    setClosing(true)
    timerRef.current = window.setTimeout(after ?? cancelRef.current, 160)
  }

  useEffect(() => {
    const panel = panelRef.current
    const previous = document.activeElement as HTMLElement | null
    const initial = panel?.querySelector<HTMLElement>('[data-autofocus]') ?? panel
    initial?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        close()
        return
      }
      if (event.key !== 'Tab' || !panel) return
      const items = [...panel.querySelectorAll<HTMLElement>('button, input, select, textarea, [href], [tabindex]:not([tabindex="-1"])')]
        .filter(item => !item.hasAttribute('disabled'))
      if (!items.length) { event.preventDefault(); return }
      const first = items[0]
      const last = items[items.length - 1]
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) { event.preventDefault(); last.focus() }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      previous?.focus?.()
    }
    // close 只依赖稳定的 ref，挂载一次即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return createPortal(
    <div
      className={'dlg-overlay' + (closing ? ' is-closing' : '')}
      onMouseDown={event => { if (event.target === event.currentTarget) close() }}
    >
      <div className="dlg-panel" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} ref={panelRef}>
        <div className="dlg-title" id={titleId}>{title}</div>
        {children(close)}
      </div>
    </div>,
    document.body,
  )
}

/** 暂停确认：后端在线（live）时要填原因，写入暂停记录；否则只是演示替身。 */
export function PauseDialog({ onCancel, onConfirm, live = false }: {
  onCancel: () => void
  onConfirm: (reason: string) => void
  live?: boolean
}) {
  const [reason, setReason] = useState('')
  const inputId = useId()
  return (
    <Modal title="暂停全部自动执行？" onCancel={onCancel}>
      {close => (
        <>
          <div className="dlg-rows">
            <div className="dlg-row"><span className="lb">新事件</span><span>所有等级都改为只提议，等你批准后才执行</span></div>
            <div className="dlg-row"><span className="lb">已下发动作</span><span>继续运行到结束时刻，需要停止的请单独撤销</span></div>
            <div className="dlg-row"><span className="lb">不受影响</span><span>涉及人员安全或重大损失的操作工单照常派发，预案保护动作照常执行</span></div>
            <div className="dlg-row"><span className="lb">恢复后</span><span>积压的提议不会自动放行，需要逐条批准</span></div>
          </div>
          <label className="dlg-label" htmlFor={inputId}>暂停原因</label>
          <input
            id={inputId}
            className="dlg-input"
            data-autofocus
            value={reason}
            onChange={event => setReason(event.target.value)}
            placeholder="一句话说明，写入暂停记录"
          />
          {!live && <p className="dlg-demo"><span className="desk-demo">离线演示</span>当前为离线演示模式，暂停状态仅在本地界面生效。</p>}
          <div className="dlg-foot">
            <button type="button" className="btn" onClick={() => close(onCancel)}>保持自动</button>
            <button type="button" className="btn go" disabled={live && !reason.trim()} onClick={() => close(() => onConfirm(reason.trim()))}>确认暂停</button>
          </div>
        </>
      )}
    </Modal>
  )
}

export type RetractResult = { reasonKind: ReasonKind; note: string; confirmProtection: boolean }

/** 真接口模式的撤销/取消信息：stage 决定走撤销还是取消；preview 只有撤销才有。 */
export type RetractLive = {
  stage: 'active' | 'scheduled'
  protection: boolean
  preview: RevokePreview | null
  /** 预计影响读取失败的原因；读取中为空串且 preview 为 null */
  error: string
}

type RetractProps = {
  label: string
  impact: string
  onCancel: () => void
  onConfirm: (result: RetractResult) => void
  live?: RetractLive
}

const signed = (value: number, digits: number, unit: string) =>
  `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(digits)} ${unit}`

function impactRows(preview: RevokePreview) {
  const windowMinutes = (Date.parse(preview.window.end) - Date.parse(preview.window.start)) / 60000
  return [
    ['试算窗口', `当前分钟起剩余 ${Math.round(windowMinutes)} 分钟`],
    ['费用', `${signed(preview.cost_delta_cny, 2, '元')}（撤销 ${preview.revoke.cost_cny.toFixed(2)} → 保持 ${preview.keep.cost_cny.toFixed(2)}）`],
    ['越界分钟数', `${signed(preview.violation_minutes_delta, 0, '分钟')}（撤销 ${preview.revoke.violation_minutes} → 保持 ${preview.keep.violation_minutes}）`],
    ['T1 峰值', `${signed(preview.t1_peak_delta_kw, 1, 'kW')}（撤销 ${preview.revoke.t1_peak_kw.toFixed(1)} → 保持 ${preview.keep.t1_peak_kw.toFixed(1)}）`],
  ] as const
}

export function RetractDialog({ label, impact, onCancel, onConfirm, live }: RetractProps) {
  const [reasonKind, setReasonKind] = useState<ReasonKind>('system_wrong')
  const [note, setNote] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const inputId = useId()
  const cancelling = live?.stage === 'scheduled'
  const protection = Boolean(live?.protection)
  // 取消必须写原因；保护动作无论取消还是撤销都要写说明并二次确认
  const ready = !live || ((!(cancelling || protection) || Boolean(note.trim())) && (!protection || confirmed))
  return (
    <Modal title={`${cancelling ? '取消' : '撤销'}“${label}”？`} onCancel={onCancel}>
      {close => (
        <>
          <div className="dlg-rows">
            <div className="dlg-row"><span className="lb">立即发生</span><span>{cancelling ? `“${label}”还没生效，取消后不会下发` : `“${label}”执行立即停止，下一分钟设备回到没有这条动作的状态`}</span></div>
            {!live && <div className="dlg-row"><span className="lb">预计影响</span><span>{impact}</span></div>}
            {live && !cancelling && !live.preview && (
              <div className="dlg-row"><span className="lb">预计影响</span><span role={live.error ? 'alert' : 'status'}>{live.error || '正在试算撤销与保持两种情况…'}</span></div>
            )}
            {live && !cancelling && live.preview && impactRows(live.preview).map(([name, text]) => (
              <div className="dlg-row" key={name}><span className="lb">{name}</span><span className="n">{text}</span></div>
            ))}
            {!cancelling && (
              <div className="dlg-row"><span className="lb">留下记录</span><span>{reasonKind === 'system_wrong' || !live ? '计入人工纠错，这类事件的自主等级可能下调' : '只记录，不计入人工纠错'}</span></div>
            )}
            {protection && <div className="dlg-row"><span className="lb">保护动作</span><span>这是预案里的保护动作，{cancelling ? '取消' : '撤销'}后变压器可能失去保护</span></div>}
          </div>
          {live && !cancelling && (
            <fieldset className="dlg-kind">
              <legend className="dlg-label">撤销原因</legend>
              <label><input type="radio" name="reason-kind" checked={reasonKind === 'system_wrong'} onChange={() => setReasonKind('system_wrong')} />系统判断有误</label>
              <label><input type="radio" name="reason-kind" checked={reasonKind === 'other'} onChange={() => setReasonKind('other')} />其他</label>
            </fieldset>
          )}
          <label className="dlg-label" htmlFor={inputId}>{cancelling ? '取消原因' : '说明'}{protection && '（必填）'}</label>
          <input
            id={inputId}
            className="dlg-input"
            data-autofocus
            value={note}
            onChange={event => setNote(event.target.value)}
            placeholder={cancelling ? '一句话说明取消原因' : '一句话说明，会用于后续纠错'}
          />
          {protection && (
            <label className="dlg-confirm">
              <input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />我确认要{cancelling ? '取消' : '撤销'}这条保护动作
            </label>
          )}
          {!live && <p className="dlg-demo"><span className="desk-demo">离线演示</span>当前为离线演示模式，撤销操作仅在本地界面生效。</p>}
          <div className="dlg-foot">
            <button type="button" className="btn" onClick={() => close(onCancel)}>{cancelling ? '不取消' : '不撤销'}</button>
            <button type="button" className="btn go" disabled={!ready}
              onClick={() => close(() => onConfirm({ reasonKind, note: note.trim(), confirmProtection: confirmed }))}>
              {cancelling ? '确认取消' : '确认撤销'}
            </button>
          </div>
        </>
      )}
    </Modal>
  )
}
