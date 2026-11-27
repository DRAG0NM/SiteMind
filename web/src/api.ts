import type { Day, UIEvent } from './store'

/** 接口错误带 HTTP 状态码；连不上后端时没有状态码。 */
class HttpError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'HttpError'
    this.status = status
  }
}

async function failure(response: Response, fallback: string) {
  let detail = ''
  try {
    const body = await response.json() as { error?: { message?: unknown } }
    if (typeof body?.error?.message === 'string') detail = body.error.message
  } catch { /* 非 JSON 错误体只保留状态码 */ }
  return new HttpError(`${fallback} (${response.status})${detail ? `：${detail}` : ''}`, response.status)
}
export async function getDay(runId: string): Promise<Day> {
  const response = await fetch(`/api/v1/demo/day/${encodeURIComponent(runId)}`)
  if (!response.ok) throw await failure(response, '加载失败')
  return response.json() as Promise<Day>
}
export async function runAgent(query: string, runId: string, asOf: number, onEvent: (e: UIEvent) => void) {
  const response = await fetch('/api/v1/agent/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent_name: 'plantkg_main', query, run_id: runId, as_of: new Date(asOf).toISOString() }) })
  if (!response.ok || !response.body) throw await failure(response, '网关请求失败')
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let terminal = false
  while (true) {
    const { value, done } = await reader.read()
    buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, '\n')
    const chunks = buffer.split('\n\n'); buffer = chunks.pop() ?? ''
    for (const chunk of chunks) {
      const data = chunk.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
      if (data) { const event = JSON.parse(data) as UIEvent; onEvent(event); if (['run_finished','run_failed','run_cancelled'].includes(event.type)) terminal = true }
    }
    if (done) break
  }
  if (!terminal) throw new Error('网关连接已结束，但未收到运行终态')
}

export type WatchdogTicket = { ticket_id:string; ticket_status:string; reason:string; checked_at:string; recovery:string }
export async function getWatchdog(): Promise<WatchdogTicket[]> {
  const response = await fetch('/api/v1/watchdog/blocked')
  if (!response.ok) throw await failure(response, 'Watchdog 查询失败')
  return (await response.json() as {items:WatchdogTicket[]}).items
}
/** Watchdog 502/404 说明后端或网关没起：轮询据此退避；其它失败保持原频率。 */
export function watchdogBackoff(reason: unknown): boolean {
  return reason instanceof HttpError && (reason.status === 502 || reason.status === 404)
}
export async function explainWatchdog(question:string): Promise<string> {
  const response = await fetch(`/api/v1/watchdog/explain?question=${encodeURIComponent(question)}`)
  if (!response.ok) throw await failure(response, 'Watchdog 查询失败')
  return (await response.json() as {answer:string}).answer
}

/** 统一确认与驳回：POST /api/v1/actions/{action_id}/confirm、/reject。 */
export type ActionDecisionResult = { action_id?: string; status?: string }
export async function confirmAction(actionId: string, body: Record<string, unknown>): Promise<ActionDecisionResult> {
  const response = await fetch(`/api/v1/actions/${encodeURIComponent(actionId)}/confirm`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) throw await failure(response, '确认失败')
  return response.json() as Promise<ActionDecisionResult>
}
export async function rejectAction(actionId: string, body: Record<string, unknown>): Promise<ActionDecisionResult> {
  const response = await fetch(`/api/v1/actions/${encodeURIComponent(actionId)}/reject`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) throw await failure(response, '驳回失败')
  return response.json() as Promise<ActionDecisionResult>
}

// ---------------------------------------------------------------- B35 只读接口
// 契约见 plantkg/core/contracts.py 的 API_ROUTES；非 2xx 一律抛错，由 features 层退回演示数据。

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw await failure(response, '读取失败')
  return response.json() as Promise<T>
}

export type Page<T> = { items: T[]; next_cursor?: string | null }

/** 证据事件：model_params 是模型填值，params 是系统绑定值，path 区分检索与推理。 */
export type EvidenceEventRaw = {
  event_id?: string
  seq?: number
  occurred_at?: string | null
  agent_run_id?: string | null
  run_id?: string | null
  action_id?: string | null
  event_type?: string | null
  tool?: string | null
  path?: 'retrieval' | 'reasoning' | 'none' | null
  rule_ids?: string[] | null
  sources?: { asset_version_id?: string | null; locator?: string | null; evidence_id?: string | null; excerpt?: string | null }[] | null
  params?: Record<string, unknown> | null
  model_params?: Record<string, unknown> | null
  result_status?: string | null
  summary?: string | null
  nature?: string | null
}

export async function getEvidenceEvents(runId: string, actionId?: string | null): Promise<EvidenceEventRaw[]> {
  const query = new URLSearchParams({ run_id: runId, page_size: '200' })
  if (actionId) query.set('action_id', actionId)
  return (await getJson<Page<EvidenceEventRaw>>(`/api/v1/evidence?${query}`)).items
}

export async function getPlans(runId: string): Promise<{ plan_id: string }[]> {
  return (await getJson<Page<{ plan_id: string }>>(`/api/v1/plans?run_id=${encodeURIComponent(runId)}`)).items
}

export async function getPlanExplanation(planId: string, actionId?: string | null): Promise<EvidenceEventRaw[]> {
  const query = new URLSearchParams()
  if (actionId) query.set('action_id', actionId)
  return (await getJson<Page<EvidenceEventRaw>>(`/api/v1/plans/${encodeURIComponent(planId)}/explanation?${query}`)).items
}

export type AssetFragmentRaw = { fragment_id: string; locator?: string | null; kind?: string | null; text?: string | null }
export async function getAssetFragments(assetVersionId: string): Promise<AssetFragmentRaw[]> {
  return (await getJson<Page<AssetFragmentRaw>>(`/api/v1/assets/${encodeURIComponent(assetVersionId)}/fragments?page_size=20`)).items
}

/** 待确认动作：kind 见 PendingActionKind，payload 随 kind 不同。 */
export type PendingActionRaw = {
  action_id: string
  kind: string
  status: string
  revision: number
  payload: Record<string, unknown>
  target_ref?: string | null
  proposed_at?: string
  reason?: string | null
}

export async function getActions(kind?: string, status = 'proposed'): Promise<PendingActionRaw[]> {
  const query = new URLSearchParams({ status, page_size: '100' })
  if (kind) query.set('kind', kind)
  return (await getJson<Page<PendingActionRaw>>(`/api/v1/actions?${query}`)).items
}

export async function getReviews(): Promise<PendingActionRaw[]> {
  return (await getJson<Page<PendingActionRaw>>('/api/v1/reviews?page_size=100')).items
}

/** B33 的三组对照报告：metrics 是 policy -> 指标名 -> 值。 */
export type EvaluationReportRaw = {
  report_id: string
  eval_days?: string[]
  policies: string[]
  metrics: Record<string, Record<string, number | null>>
  comparable?: boolean
  notes?: string[]
}

export async function getBaselines(): Promise<EvaluationReportRaw[]> {
  return (await getJson<Page<EvaluationReportRaw>>('/api/v1/baselines?page_size=10')).items
}

/** 复核量-F1 / 复核量-费用误差：points 是 (复核量, 指标值)。 */
export type MetricSeriesRaw = {
  metric: string
  dataset: string
  split_id: string
  points: [number, number][]
  note?: string | null
}

export async function getReviewMetrics(dataset = 'demo'): Promise<MetricSeriesRaw> {
  return getJson<MetricSeriesRaw>(`/api/v1/review-metrics?dataset=${encodeURIComponent(dataset)}`)
}

export type EvolutionEntryRaw = { seq: number; event: string; occurred_at: string; details: Record<string, unknown> }
/** 时间线按 seq 升序分页（cursor = 已读到的 seq）：从 fromSeq 之后一路翻到最后一页，最新记录在数组末尾。最多翻 25 页（5000 条）。 */
export async function getEvolutionTimeline(fromSeq?: number): Promise<EvolutionEntryRaw[]> {
  const entries: EvolutionEntryRaw[] = []
  let cursor: string | null | undefined = fromSeq ? String(fromSeq) : undefined
  for (let page = 0; page < 25; page += 1) {
    const query = new URLSearchParams({ page_size: '200' })
    if (cursor) query.set('cursor', cursor)
    const result = await getJson<Page<EvolutionEntryRaw>>(`/api/v1/evolution/timeline?${query}`)
    entries.push(...result.items)
    cursor = result.next_cursor
    if (!cursor) break
  }
  return entries
}
