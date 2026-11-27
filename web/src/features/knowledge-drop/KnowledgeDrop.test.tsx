// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { sample, type Day } from '../../store'
import { growGraph } from './graph'
import { KnowledgeDropScreen } from './KnowledgeDropScreen'

let container: HTMLDivElement
let root: Root

const wait = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const settle = async (ms = 40) => {
  await act(async () => { await wait(ms) })
}

const waitFor = async (check: () => boolean, timeout = 2500) => {
  const start = Date.now()
  while (!check()) {
    if (Date.now() - start > timeout) throw new Error(`等待超时：${container.textContent?.slice(0, 500)}`)
    await settle(30)
  }
}

/** jsdom 没有 DataTransfer 构造器，用带 files 的普通事件模拟一次 drop。 */
async function dropFile(name: string, body: string, type = 'text/markdown') {
  const zone = container.querySelector('[data-testid="kd-dropzone"]')
  expect(zone).toBeTruthy()
  const file = new File([body], name, { type })
  const event = new Event('drop', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'dataTransfer', { value: { files: [file] } })
  await act(async () => {
    zone!.dispatchEvent(event)
    await wait(30)
  })
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  // 减弱动效，测试里直接看最终图，不用等每个节点 180ms 的动画。
  document.documentElement.dataset.motion = 'reduce'
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  delete document.documentElement.dataset.motion
  vi.unstubAllGlobals()
})

it('渲染拖放区、进度条与上传说明', async () => {
  await act(async () => root.render(<KnowledgeDropScreen/>))
  expect(container.querySelector('[data-testid="kd-dropzone"]')).toBeTruthy()
  expect(container.querySelector('[role="progressbar"]')).toBeTruthy()
  expect(container.textContent).toContain('把资料拖到这里')
  expect(container.textContent).toContain('上传不等于发布')
  expect(container.textContent).toContain('新增节点')
  expect(container.textContent).toContain('新增边')
})

it('拖入含实体名的资料后长出合成节点，jsdom 下不崩', async () => {
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await act(async () => (container.querySelector('input[type="checkbox"]') as HTMLInputElement).click())
  await dropFile('manual.md', '# 设备手册\n\nT1 变压器负载 96%，储能 BESS1 放电 390 kW；行政楼 3F 参观期间保持 24 ℃。')
  await waitFor(() => (container.textContent ?? '').includes('manual.md'))

  const nodes = container.querySelector('.kd-nodes')
  expect(nodes?.textContent).toContain('T1 变压器')
  expect(nodes?.textContent).toContain('合成')
  expect(nodes?.textContent).toContain('原文命中')
  // jsdom 量不到画布尺寸，视图降级成清单而不是抛错。
  expect(container.textContent).toContain('图谱画布不可用')
  expect(container.textContent).toContain('离线演示：本地匹配，资料未上传到后端')
  expect(container.querySelector('.kd-edges')?.textContent).toContain('提及')
  expect(container.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('100')
})

it('上传失败时不生成合成图', async () => {
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await dropFile('manual.md', 'T1 变压器')
  expect(container.textContent).toContain('上传失败')
  expect(container.querySelector('.kd-nodes')?.textContent).not.toContain('T1 变压器')
})

it('上传成功后展示资产回执和服务端原文', async () => {
  const receipt = { asset_version_id: 'asset://manufacturing/12345678/process_doc/source', document_id: 'doc-a',
    content_hash: 'sha256:test', available_at: '2026-11-15T00:00:00Z', nature: 'synthetic',
    expert: 'equipment', entity_ids: [], ontology_candidate_ids: [], status: 'needs_review', candidate_ids: ['candidate-1'], published: false, note: '候选待复核' }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, json: async () => receipt })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ items: [{ text: '服务器原文' }] }) }))
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await dropFile('manual.md', '资料文本')
  await waitFor(() => (container.textContent ?? '').includes('candidate-1'))
  expect(container.textContent).toContain(receipt.asset_version_id)
  expect(container.textContent).toContain('服务器原文')
  expect(container.textContent).toContain('未发布')
  expect(new URL(window.location.href).searchParams.get('knowledge_document')).toBe('doc-a')
})

it('连续上传不继承回执身份，手填改版失败后可用原键重试', async () => {
  const posts: Record<string, any>[] = []
  let fail = false
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); posts.push(body)
      if (fail) { fail = false; return { ok: false, status: 500, json: async () => ({}) } }
      return { ok: true, json: async () => ({ asset_version_id: `asset-${posts.length === 2 ? 1 : posts.length}`, document_id: body.document_id || `doc-${posts.length === 2 ? 1 : posts.length}`,
        expert: body.expert, entity_ids: ['old-entity'], nature: body.nature, available_at: '2026-11-15T00:00:00Z',
        candidate_ids: [], ontology_candidate_ids: [], status: posts.length === 2 ? 'published' : 'needs_review',
        published: posts.length === 2, manifest_ids: posts.length === 2 ? ['manifest-a'] : [], note: '' }) }
    }
    if (String(input).includes('/knowledge/graph')) return { ok: true, json: async () => ({ ok: true, data: { items: [] }, page: { next_cursor: null } }) }
    return { ok: true, json: async () => ({ items: [{ text: '原文' }] }) }
  })
  vi.stubGlobal('fetch', fetchMock)
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await dropFile('a.md', '资料 A')
  await waitFor(() => container.textContent!.includes('doc-1'))
  await dropFile('a.md', '资料 A')
  await waitFor(() => container.textContent!.includes('已确认发布'))
  expect(posts[1].idempotency_key).toBe(posts[0].idempotency_key)
  await waitFor(() => container.textContent!.includes('此清单没有图谱记录'))
  expect(container.textContent).toContain('已确认发布')
  await dropFile('b.md', '资料 B')
  await waitFor(() => container.textContent!.includes('doc-3'))
  expect(posts[2].document_id).toBeNull(); expect(posts[2].entity_ids).toEqual([])
  expect(posts[2].idempotency_key).not.toBe(posts[0].idempotency_key)
  const select = container.querySelector('select')!
  await act(async () => { select.value = 'energy'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await dropFile('c.md', '资料 C')
  await waitFor(() => container.textContent!.includes('doc-4'))
  expect(posts[3].expert).toBe('energy'); expect(posts[3].document_id).toBeNull(); expect(posts[3].entity_ids).toEqual([])
  const inputs = Array.from(container.querySelectorAll<HTMLInputElement>('label input')).filter(input => input.type === 'text')
  const setInput = async (input: HTMLInputElement, value: string) => act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await setInput(inputs[0], 'manual-existing'); await setInput(inputs[1], 'entity-x')
  fail = true
  await dropFile('revision.md', '改版资料')
  await waitFor(() => container.textContent!.includes('上传失败'))
  expect(inputs[0].value).toBe('manual-existing'); expect(inputs[1].value).toBe('entity-x')
  await dropFile('revision.md', '改版资料')
  await waitFor(() => container.textContent!.includes('资料编号：manual-existing'))
  expect(posts[5].document_id).toBe('manual-existing'); expect(posts[5].entity_ids).toEqual(['entity-x'])
  expect(posts[5].idempotency_key).toBe(posts[4].idempotency_key)
  expect(inputs[0].value).toBe(''); expect(inputs[1].value).toBe('')
  const url = new URL(window.location.href)
  expect(url.searchParams.get('knowledge_document')).toBe('manual-existing')
  expect(url.searchParams.get('knowledge_asset')).toBe('asset-6')
})

it('切换离线模式清空真实回执、图谱和恢复引用', async () => {
  const receipt = { asset_version_id: 'asset://manufacturing/12345678/process_doc/source', document_id: 'doc-a',
    available_at: '2026-11-15T00:00:00Z', nature: 'synthetic', expert: 'equipment', entity_ids: [], ontology_candidate_ids: [], status: 'needs_review', candidate_ids: [], published: false, note: '' }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, json: async () => receipt })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ items: [{ text: '真实原文' }] }) }))
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await dropFile('manual.md', '资料文本')
  await waitFor(() => (container.textContent ?? '').includes('真实原文'))
  expect(window.location.search).toContain('knowledge_asset')
  await act(async () => (container.querySelector('input[type="checkbox"]') as HTMLInputElement).click())
  expect(container.textContent).not.toContain(receipt.asset_version_id)
  expect(container.textContent).not.toContain('真实原文')
  expect(window.location.search).toBe('')
  expect(container.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('0')
})

it('共享资产的不同资料通过双身份链接刷新和更新各自回执', async () => {
  const asset = 'asset://manufacturing/12345678/process_doc/source'
  const receipts = {
    'manual-hvac': { document_id: 'manual-hvac', expert: 'hvac', source_name: 'hvac.md' },
    'manual-energy': { document_id: 'manual-energy', expert: 'energy', source_name: 'energy.md' },
  } as const
  const fetchMock = vi.fn(async (input: string | URL | Request) => {
    const url = new URL(String(input), window.location.origin)
    if (url.pathname.endsWith('/ingestion')) {
      const id = url.searchParams.get('document_id') as keyof typeof receipts
      if (!(id in receipts)) throw new Error('必须带上正确资料编号')
      return { ok: true, json: async () => ({ ...receipts[id], asset_version_id: asset,
        available_at: '2026-11-15T00:00:00Z', nature: 'synthetic', entity_ids: [], ontology_candidate_ids: [],
        candidate_ids: [], status: 'needs_review', published: false, note: '' }) }
    }
    return { ok: true, json: async () => ({ items: [{ text: '相同的服务端原文' }] }) }
  })
  vi.stubGlobal('fetch', fetchMock)
  for (const [id, expected] of Object.entries(receipts)) {
    window.history.replaceState(null, '', `/?knowledge_asset=${encodeURIComponent(asset)}&knowledge_document=${id}`)
    await act(async () => root.render(<KnowledgeDropScreen key={id}/>))
    await waitFor(() => (container.textContent ?? '').includes(expected.source_name))
    expect(container.textContent).toContain(id)
    expect(Array.from(container.querySelectorAll<HTMLInputElement>('label input')).filter(input => input.type === 'text').slice(0, 2).map(input => input.value)).toEqual(['', ''])
    expect((container.querySelector('select') as HTMLSelectElement).value).toBe(expected.expert)
    const refresh = Array.from(container.querySelectorAll('button')).find(button => button.textContent === '刷新处理状态与图谱')!
    await act(async () => { refresh.click(); await wait(30) })
    expect(new URL(String(fetchMock.mock.calls.filter(call => String(call[0]).includes('/ingestion')).at(-1)![0]), window.location.origin).searchParams.get('document_id')).toBe(id)
    expect(new URL(window.location.href).searchParams.get('knowledge_document')).toBe(id)
  }
})

it('旧链接有多个匹配回执时展示冲突，不显示其他资料', async () => {
  const asset = 'asset://manufacturing/12345678/process_doc/source'
  window.history.replaceState(null, '', `/?knowledge_asset=${encodeURIComponent(asset)}`)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 409,
    json: async () => ({ error: { message: '该资产对应多个资料，请提供 document_id' } }) }))
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await waitFor(() => (container.textContent ?? '').includes('该资产对应多个资料'))
  expect(container.textContent).toContain('knowledge_document=资料编号')
  expect(container.textContent).not.toContain('资料编号：')
  expect(container.querySelector('.kd-nodes')?.textContent).not.toContain(asset)
})

it('旧后端忽略资料编号并返回其他回执时不展示错档', async () => {
  const asset = 'asset://manufacturing/12345678/process_doc/source'
  window.history.replaceState(null, '', `/?knowledge_asset=${encodeURIComponent(asset)}&knowledge_document=manual-hvac`)
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    asset_version_id: asset, document_id: 'manual-energy', expert: 'energy', source_name: 'energy.md',
  }) })
  vi.stubGlobal('fetch', fetchMock)
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await waitFor(() => (container.textContent ?? '').includes('回执资料编号与链接不一致'))
  expect(container.textContent).not.toContain('资料编号：manual-energy')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('刷新页面恢复服务端发布回执并从三元组绘图', async () => {
  const asset = 'asset://manufacturing/12345678/process_doc/source'
  window.history.replaceState(null, '', `/?knowledge_asset=${encodeURIComponent(asset)}`)
  const receipt = { asset_version_id: asset, document_id: 'doc-a', available_at: '2026-11-15T00:00:00Z',
    nature: 'synthetic', expert: 'equipment', entity_ids: [], ontology_candidate_ids: [], status: 'published', candidate_ids: ['candidate'], published: true, note: '已发布', manifest_ids: ['manifest-a'] }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce({ ok: true, json: async () => receipt })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ items: [{ text: '原文' }] }) })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true, data: { items: [
      { subject: '<entity://factory/air_compressor/AC-2>', predicate: '<https://plantkg.dev/ns/ontology#sourceAsset>', object: `<${asset}>` },
    ] }, page: { next_cursor: null } }) }))
  await act(async () => root.render(<KnowledgeDropScreen/>))
  await waitFor(() => (container.textContent ?? '').includes('已发布知识图谱'))
  expect(container.querySelector('.kd-nodes')?.textContent).toContain('AC-2')
  expect(container.querySelector('.kd-edges')?.textContent).toContain('sourceAsset')
  expect(container.textContent).toContain('已确认发布')
  expect((container.querySelector('select') as HTMLSelectElement).value).toBe('equipment')
  expect(new URL(window.location.href).searchParams.get('knowledge_document')).toBe('doc-a')
})

it('growGraph 只产出 synthetic 节点，生长顺序从资料节点开始', () => {
  const day: Day = structuredClone(sample)
  const graph = growGraph(day, 'T1 变压器越限，储能放电削峰。', 'manual.md')
  expect(graph.order[0]).toBe('doc')
  expect(graph.nodes.every(node => node.nature === 'synthetic')).toBe(true)
  expect(graph.nodes.some(node => node.label === 'T1 变压器')).toBe(true)
  expect(graph.edges.length).toBeGreaterThan(0)
  expect(graph.order.length).toBe(graph.nodes.length)
  expect(graph.matches).toBe(graph.nodes.length - 1)
})
