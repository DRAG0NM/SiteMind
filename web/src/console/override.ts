/** 人工干预的数据访问与共享状态：暂停自动执行、取消未生效动作、撤销已生效动作（#245）。
 *
 * 真实接口优先：后端在线就以后端状态为准；读取失败（接口不通、返回的不是预期结构）时 `status` 为 null，
 * 界面退回前端演示替身（useDay 里的 paused / retracted）。写入接口由开发代理替页面带上运维令牌。
 */
import { create } from 'zustand'
import { useDay } from '../store'
import type { ConsoleActionView } from './types'

export interface AutonomyStatus {
  paused: boolean
  scope: 'plant' | 'run'
  run_id: string | null
  by: string | null
  since: string | null
  reason: string | null
  resumed_by: string | null
  resumed_at: string | null
  /** 暂停以来积压、仍待批准的提议条数 */
  backlog: number
}

export type ActionStage = 'scheduled' | 'active' | 'ended' | 'cancelled' | 'revoked'

export interface AppliedAction {
  action_id: string
  run_id: string
  entity_id: string
  command: string
  params: Record<string, unknown>
  interval: { start: string; end: string }
  stage: ActionStage
  protection: boolean
  ticket_id: string | null
  proposal_id: string | null
  temp_rule_id: string | null
}

export interface RevokeOutcome { cost_cny: number; violation_minutes: number; t1_peak_kw: number }

export interface RevokePreview {
  action_id: string
  run_id: string
  window: { start: string; end: string }
  revoke: RevokeOutcome
  keep: RevokeOutcome
  cost_delta_cny: number
  violation_minutes_delta: number
  t1_peak_delta_kw: number
  protection: boolean
  note: string
}

export type ReasonKind = 'system_wrong' | 'other'

async function failure(response: Response, fallback: string) {
  let detail = ''
  try {
    const body = await response.json() as { error?: { message?: unknown } }
    if (typeof body?.error?.message === 'string') detail = body.error.message
  } catch { /* 非 JSON 错误体只保留状态码 */ }
  return new Error(`${fallback} (${response.status})${detail ? `：${detail}` : ''}`)
}

async function request<T>(method: 'GET' | 'POST', path: string, fallback: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, method === 'GET' ? undefined : {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) throw await failure(response, fallback)
  // 读副本：不消耗原响应体（被测试替身共用同一个 Response 时，别处还要读它）
  if (response.bodyUsed) throw new Error('响应已被读取')
  return response.clone().json() as Promise<T>
}

const query = (runId?: string | null) => runId ? `?run_id=${encodeURIComponent(runId)}` : ''

/** 返回的结构不对（例如被别的接口占位）时当作后端不可用。 */
const isStatus = (value: unknown): value is AutonomyStatus =>
  typeof value === 'object' && value !== null && typeof (value as AutonomyStatus).paused === 'boolean'

export async function getAutonomyStatus(runId?: string | null): Promise<AutonomyStatus> {
  const status = await request<unknown>('GET', `autonomy/status${query(runId)}`, '读取暂停状态失败')
  if (!isStatus(status)) throw new Error('暂停状态接口返回的结构不对')
  return status
}

export async function getAppliedActions(runId?: string | null): Promise<AppliedAction[]> {
  const page = await request<{ items?: unknown }>('GET', `autonomy/actions${query(runId)}`, '读取已下发动作失败')
  return Array.isArray(page.items) ? page.items as AppliedAction[] : []
}

export const getRevokePreview = (actionId: string, runId?: string | null) =>
  request<RevokePreview>('GET', `actions/${encodeURIComponent(actionId)}/revoke-preview${query(runId)}`, '读取撤销预计影响失败')

export const cancelApplied = (actionId: string, reason: string, confirmProtection: boolean, runId?: string | null) =>
  request<unknown>('POST', `actions/${encodeURIComponent(actionId)}/cancel`, '取消失败',
    { reason, confirm_protection: confirmProtection, ...(runId ? { run_id: runId } : {}) })

export const revokeApplied = (
  actionId: string, reasonKind: ReasonKind, note: string, confirmProtection: boolean, runId?: string | null,
) => request<unknown>('POST', `actions/${encodeURIComponent(actionId)}/revoke`, '撤销失败',
  { reason_kind: reasonKind, note, confirm_protection: confirmProtection, ...(runId ? { run_id: runId } : {}) })

interface AutonomyStore {
  /** 后端暂停状态；null 表示读取失败，界面用前端演示替身 */
  status: AutonomyStatus | null
  /** 后端已下发的动作；读取失败时为空 */
  applied: AppliedAction[]
  /** 暂停/恢复请求失败的原因，显示在横幅区；成功后清空 */
  error: string
  refresh: () => Promise<void>
}

export const useAutonomy = create<AutonomyStore>(set => ({
  status: null,
  applied: [],
  error: '',
  refresh: async () => {
    const [status, applied] = await Promise.allSettled([getAutonomyStatus(), getAppliedActions()])
    set({
      status: status.status === 'fulfilled' ? status.value : null,
      applied: applied.status === 'fulfilled' ? applied.value : [],
    })
  },
}))

/** 暂停/恢复：后端在线走真接口并刷新；不在线退回前端演示替身。失败原因写进 error，不静默吞掉。 */
async function toggle(path: 'autonomy/pause' | 'autonomy/resume', fallback: string, body: unknown, demo: boolean) {
  const { status, refresh } = useAutonomy.getState()
  if (!status) { useDay.getState().setPaused(demo); return }
  try {
    await request<AutonomyStatus>('POST', path, fallback, body)
    useAutonomy.setState({ error: '' })
  } catch (reason) {
    useAutonomy.setState({ error: reason instanceof Error ? reason.message : String(reason) })
  }
  await refresh()
}

export const pauseAutonomy = (reason: string) => toggle('autonomy/pause', '暂停失败', { reason }, true)
export const resumeAutonomy = () => toggle('autonomy/resume', '恢复失败', {}, false)

const time = (value: string) => Date.parse(value)

/** 控制台的动作行对应后端哪条已下发动作：同对象同命令，优先结束时刻一致，其次开始时刻最近。 */
export function matchApplied(row: ConsoleActionView, applied: AppliedAction[]): AppliedAction | null {
  const same = applied.filter(item => item.entity_id === row.entityId && item.command === row.command
    && (item.stage === 'active' || item.stage === 'scheduled'))
  return same.find(item => time(item.interval.end) === row.endsAt)
    ?? [...same].sort((a, b) => Math.abs(time(a.interval.start) - row.startedAt) - Math.abs(time(b.interval.start) - row.startedAt))[0]
    ?? null
}

/** 暂停了多久（分钟）：后端状态里的暂停时刻到回放游标（或当前时刻）。 */
export function pausedMinutes(status: AutonomyStatus, nowMs: number): number | null {
  return status.since ? Math.max(0, Math.round((nowMs - time(status.since)) / 60000)) : null
}
