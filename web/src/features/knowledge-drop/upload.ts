import type { DataNature } from '../common/nature'

export type IngestReceipt = {
  asset_version_id: string
  document_id: string
  expert?: string
  content_hash: string
  available_at: string
  nature: DataNature
  status: string
  candidate_ids: string[]
  ontology_candidate_ids: string[]
  entity_ids: string[]
  published: boolean
  note: string
  received_at?: string
  source_name?: string
  manifest_ids?: string[]
  action_ids?: string[]
  reviews?: Record<string, string>
  backtests?: Record<string, string>
}

export async function uploadDocument(file: File, text: string, expert: string, nature: DataNature, documentId: string, entityIds: string[], key: string): Promise<IngestReceipt> {
  const response = await fetch('/api/v1/assets:ingest', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name, text, expert, nature, document_id: documentId || null, entity_ids: entityIds, idempotency_key: key }),
    signal: AbortSignal.timeout(40000),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { error?: { message?: string } }
    throw new Error(body.error?.message ?? `HTTP ${response.status}`)
  }
  return response.json() as Promise<IngestReceipt>
}

export async function getFragments(receipt: IngestReceipt): Promise<{ text: string | null; locator?: { line_start?: number } }[]> {
  const path = `/api/v1/assets/${encodeURIComponent(receipt.asset_version_id)}/fragments?as_of=${encodeURIComponent(receipt.available_at)}`
  const response = await fetch(path)
  if (!response.ok) throw new Error(`原文读取失败 (HTTP ${response.status})`)
  return ((await response.json()) as { items: { text: string | null; locator?: { line_start?: number } }[] }).items
}

export async function getReceipt(assetId: string, documentId?: string): Promise<IngestReceipt> {
  const params = new URLSearchParams({ as_of: new Date().toISOString() })
  if (documentId !== undefined) params.set('document_id', documentId)
  const response = await fetch(`/api/v1/assets/${encodeURIComponent(assetId)}/ingestion?${params}`)
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { error?: { message?: string } }
    const message = body.error?.message ?? `回执读取失败 (HTTP ${response.status})`
    throw new Error(documentId === undefined && response.status === 409
      ? `${message}；请在链接中添加 knowledge_document=资料编号` : message)
  }
  const receipt = await response.json() as IngestReceipt
  if (receipt.asset_version_id !== assetId || (documentId !== undefined && receipt.document_id !== documentId)) {
    throw new Error('回执资料编号与链接不一致，请检查后端版本')
  }
  return receipt
}

export type SearchHit = { document_id: string; asset_version_id: string; text: string; locator: { line_start?: number }; score: number }
export async function searchDocuments(expert: string, query: string): Promise<SearchHit[]> {
  const response = await fetch(`/api/v1/knowledge/documents:search?expert=${encodeURIComponent(expert)}&q=${encodeURIComponent(query)}&as_of=${encodeURIComponent(new Date().toISOString())}`)
  if (!response.ok) throw new Error(`检索失败 (HTTP ${response.status})`)
  return ((await response.json()) as { items: SearchHit[] }).items
}

export async function getPublishedGraph(manifestId: string): Promise<{ subject: string; predicate: string; object: string }[]> {
  const items: { subject: string; predicate: string; object: string }[] = []
  let cursor: string | null = null
  const asOf = new Date().toISOString()
  do {
    const response = await fetch(`/api/v1/knowledge/graph?manifest_id=${encodeURIComponent(manifestId)}&as_of=${encodeURIComponent(asOf)}&page_size=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`)
    if (!response.ok) {
      const body = await response.json().catch(() => ({})) as { error?: { message?: string } }
      throw new Error(body.error?.message ?? `HTTP ${response.status}`)
    }
    const body = await response.json() as { ok: boolean; data?: { items?: { subject: string; predicate: string; object: string }[] }; page?: { next_cursor?: string | null }; error?: { message?: string } }
    if (!body.ok) throw new Error(body.error?.message ?? '图谱查询失败')
    items.push(...body.data?.items ?? [])
    cursor = body.page?.next_cursor ?? null
  } while (cursor)
  return items
}
