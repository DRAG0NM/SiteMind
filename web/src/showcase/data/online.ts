import { evidenceSummary, eventTypeLabel, refLabel, resultStatusLabel, toolLabel } from './labels'
import { clockWithSeconds } from './time'

/**
 * 在线模式：URL 带 ?run_id 时向 dev 后端要那一天与证据链；请求、解析和失败话术都收在这层，
 * 页面组件只负责显示。离线（单文件、file://）不经过这里。
 */

/** 离线打开（file://）时后端必然连不上；这句白话原因显示在顶栏数据来源旁。 */
export const OFFLINE_NOTE = '离线打开，连不上后端'

export interface PageLocation {
  search?: string
  protocol?: string
}

function pageLocation(env: PageLocation) {
  const here = typeof location === 'undefined' ? null : location
  return { search: env.search ?? here?.search ?? '', protocol: env.protocol ?? here?.protocol ?? 'http:' }
}

/** URL 上的 ?run_id，不管当前页面能不能连后端。 */
export function requestedRunId(env: PageLocation = {}): string | null {
  return new URLSearchParams(pageLocation(env).search).get('run_id')?.trim() || null
}

/** 能连后端的在线模式：http(s) 页面带 ?run_id；file:// 打开一律当离线。 */
export function onlineRunId(env: PageLocation = {}): string | null {
  return pageLocation(env).protocol === 'file:' ? null : requestedRunId(env)
}

/** 请求失败换成的白话原因，直接给人看；status 是 HTTP 状态码，连不上后端时为 null。 */
class BackendFailure extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null = null) {
    super(message)
    this.name = 'BackendFailure'
    this.status = status
  }
}

function statusReason(status: number): string {
  if (status === 400) return '运行编号不对'
  if (status === 404) return '后端没有这次运行'
  if (status === 401 || status === 403) return '后端不允许读取'
  if (status === 502 || status === 503) return '实时服务未连接，已切换至演示场景'
  if (status >= 500) return '后端暂时不可用'
  return '后端返回出错'
}

/** 400 展示后端 ErrorResponse 里的具体原因（如文件与运行编号不一致），其余按状态码兜底。 */
async function failureMessage(response: Response): Promise<string> {
  if (response.status === 400) {
    try {
      const body = await response.json() as { error?: { message?: unknown } }
      const message = typeof body?.error?.message === 'string' ? body.error.message.trim() : ''
      if (message) return message
    } catch {
      // 400 没有 JSON 体时走状态码兜底。
    }
  }
  return statusReason(response.status)
}

async function getJson<T>(fetcher: typeof fetch, url: string, parse: (raw: unknown) => T, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetcher(url, { cache: 'no-store', ...(signal ? { signal } : {}) })
  } catch {
    throw new BackendFailure('连不上后端')
  }
  if (!response.ok) throw new BackendFailure(await failureMessage(response), response.status)
  try {
    return parse(await response.json())
  } catch (reason) {
    throw reason instanceof BackendFailure ? reason : new BackendFailure('返回数据格式异常')
  }
}

export function failureReason(reason: unknown): string {
  return reason instanceof BackendFailure ? reason.message : '未连接到实时服务'
}

/** 后端 5xx 是服务本身出错，不能用样例掩盖；返回状态码，非 5xx 返回 null。 */
export function serverStatus(reason: unknown): number | null {
  return reason instanceof BackendFailure && (reason.status ?? 0) >= 500 ? reason.status : null
}

/** GET /api/v1/demo/day/{run_id}：day.json 同构，非 2xx 是 ErrorResponse。 */
export async function fetchBackendDay<T>(runId: string, parse: (raw: unknown) => T, fetcher: typeof fetch = fetch): Promise<T> {
  return getJson(fetcher, `/api/v1/demo/day/${encodeURIComponent(runId)}`, parse)
}

/** 证据链只取展示要用的字段；内部编号（event_id/run_id/trace_id 等）不往下传。 */
export interface ApiEvidenceEvent {
  seq?: number
  occurred_at?: string | null
  agent_run_id?: string | null
  event_type?: string | null
  tool?: string | null
  rule_ids?: string[] | null
  sources?: { evidence_id?: string | null }[] | null
  result_status?: string | null
  summary?: string | null
}

export interface EvidenceRunEvent {
  time: string
  event: string
  tool: string | null
  summary: string
  refs: string[]
  status: string
}

export interface EvidenceRunGroup {
  /** 只用于分组和按运行重取，不显示。 */
  runId: string
  ordinal: number
  startedAt: string
  events: EvidenceRunEvent[]
}

interface EvidencePage {
  items: ApiEvidenceEvent[]
  nextCursor: string | null
}

function parseEvidencePage(raw: unknown): EvidencePage {
  const page = raw as { items?: unknown; next_cursor?: unknown } | null
  if (!Array.isArray(page?.items)) throw new Error('证据页缺少 items')
  return {
    items: page.items.filter((item): item is ApiEvidenceEvent => Boolean(item) && typeof item === 'object'),
    nextCursor: typeof page.next_cursor === 'string' && page.next_cursor ? page.next_cursor : null,
  }
}

/**
 * GET /api/v1/evidence?run_id=…（可带 agent_run_id 只看一次运行）。
 * page_size=200 是后端上限，超过一页时按 next_cursor 取完，别静默少显示。
 */
export async function fetchEvidence(
  runId: string, agentRunId?: string | null, fetcher: typeof fetch = fetch, signal?: AbortSignal,
): Promise<ApiEvidenceEvent[]> {
  const items: ApiEvidenceEvent[] = []
  let cursor: string | null = null
  for (;;) {
    const query = new URLSearchParams({ run_id: runId, page_size: '200' })
    if (agentRunId) query.set('agent_run_id', agentRunId)
    if (cursor) query.set('cursor', cursor)
    const page = await getJson(fetcher, `/api/v1/evidence?${query}`, parseEvidencePage, signal)
    items.push(...page.items)
    // next_cursor 不前进说明后端没有下一页，停住防止循环请求。
    if (!page.nextCursor || page.nextCursor === cursor) return items
    cursor = page.nextCursor
  }
}

function occurredAt(item: ApiEvidenceEvent): number {
  const at = item.occurred_at ? Date.parse(item.occurred_at) : Number.NaN
  return Number.isFinite(at) ? at : 0
}

/** 依据只给 refLabel 认得出的中文名；认不出的纯英文编号不展示，避免露出内部编号。 */
function evidenceRefs(item: ApiEvidenceEvent): string[] {
  const raw = [
    ...(item.rule_ids ?? []),
    ...(item.sources ?? []).map(source => source?.evidence_id),
  ].filter((ref): ref is string => typeof ref === 'string' && ref.trim() !== '')
  const labels = raw.map(ref => {
    const label = refLabel(ref)
    return label === ref && !/[\u4e00-\u9fa5]/.test(ref) ? '' : label
  })
  return Array.from(new Set(labels.filter(Boolean)))
}

/** 按 agent_run_id 分组：第 N 次运行、开始时刻与记录条数；组内按时间、seq 排序。 */
export function buildEvidenceRuns(items: ApiEvidenceEvent[], timezone: string): EvidenceRunGroup[] {
  const buckets = new Map<string, { start: number; events: { at: number; seq: number; item: ApiEvidenceEvent }[] }>()
  for (const item of items) {
    const at = occurredAt(item)
    if (!at) continue
    const key = item.agent_run_id ?? ''
    const bucket = buckets.get(key) ?? { start: at, events: [] }
    bucket.start = Math.min(bucket.start, at)
    bucket.events.push({ at, seq: typeof item.seq === 'number' ? item.seq : 0, item })
    buckets.set(key, bucket)
  }
  let ordinal = 0
  return [...buckets.entries()]
    .sort(([, a], [, b]) => a.start - b.start)
    .map(([runId, bucket]) => {
      // 空 agent_run_id 归到“这次运行”，不占“第 N 次运行”的序号，否则会出现“这次运行 + 第 2 次”。
      if (runId) ordinal += 1
      return {
        runId,
        ordinal,
        startedAt: new Date(bucket.start).toISOString(),
        events: bucket.events
          .sort((a, b) => a.at - b.at || a.seq - b.seq)
          .map(({ at, item }) => ({
            time: clockWithSeconds(at, timezone),
            event: eventTypeLabel(item.event_type ?? ''),
            tool: item.tool ? toolLabel(item.tool) : null,
            summary: item.summary ? evidenceSummary(item.summary, item.event_type ?? '') : '',
            refs: evidenceRefs(item),
            status: resultStatusLabel(item.result_status ?? ''),
          })),
      }
    })
}
