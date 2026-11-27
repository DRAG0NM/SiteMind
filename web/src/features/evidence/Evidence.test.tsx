// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EvidenceButton } from './EvidenceConsole'
import type { EvidenceSubject } from './evidenceData'

let container: HTMLDivElement
let root: Root

const ENTITY = 'entity://demo-electronics/battery/BESS1'

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

async function open(subject: EvidenceSubject) {
  await act(async () => root.render(<EvidenceButton subject={subject} />))
  const button = container.querySelector<HTMLButtonElement>('.evidence-open')
  expect(button).toBeTruthy()
  await act(async () => { button!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  await settle()
}

const respond = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), {
  status: 200,
  headers: { 'Content-Type': 'application/json' },
}))

it('所有真实接口都读不到时退回当天记录，四段齐全并标出合成数据', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
  await open({ title: '储能放电削峰', entityId: ENTITY, kind: 'queue' })

  const panel = document.querySelector('.evidence-panel')
  expect(panel).toBeTruthy()
  expect(panel!.querySelectorAll('.evidence-section')).toHaveLength(4)
  const heads = [...panel!.querySelectorAll('.evidence-sechead b')].map(element => element.textContent)
  expect(heads).toEqual(['模型填值', '系统绑定值', '图谱路径', '原文片段'])

  expect(panel!.textContent).toContain('储能放电削峰')
  expect(panel!.querySelector('.nature-badge')?.textContent).toContain('合成')
  expect(panel!.querySelector('.remote-note')?.textContent).toContain('当前展示标准演示场景数据')
  // 当天记录里储能提案的型号参数与知识摘要，都进了四段。
  expect(panel!.textContent).toContain('390')
  expect(panel!.textContent).toContain('知识 v')
  expect(panel!.querySelectorAll('.evidence-kvrow').length).toBeGreaterThan(0)
})

it('方案解释返回契约形状的数据时显示真实模型填值、绑定值、路径与原文', async () => {
  const event = {
    event_id: 'event://ev-1',
    seq: 1,
    occurred_at: '2026-07-01T01:31:05Z',
    agent_run_id: 'ar-main',
    run_id: 'run-live',
    action_id: 'act-123',
    event_type: 'tool_called',
    tool: 'asset_search',
    path: 'retrieval',
    rule_ids: ['rule://synthetic-leak#pressure-drop'],
    sources: [{
      asset_version_id: 'asset://factory/1234abcd/document/maintenance-manual',
      locator: '3.1',
      evidence_id: 'document://maintenance-manual',
      excerpt: '资料摘录：空压机压力下降速率超阈值先查管网。',
    }],
    params: { entity_id: 'entity://demo-electronics/compressor/AIR-1', command: 'open_work_order' },
    model_params: { kw: 390, reason: '压力下降速率超阈值' },
    result_status: 'ok',
    summary: '查询空压机维护手册',
    nature: 'measured',
  }
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/plans?')) return respond({ items: [{ plan_id: 'plan://p1' }], next_cursor: null })
    if (url.includes('/explanation')) return respond({ items: [event], next_cursor: null })
    if (url.includes('/api/v1/assets/')) {
      return respond({
        items: [{ fragment_id: 'frag-1', locator: '3.2', kind: 'text', text: '原文片段：停机前先关闭进气阀。' }],
        next_cursor: null,
      })
    }
    return respond({ items: [], next_cursor: null })
  })
  vi.stubGlobal('fetch', fetcher)

  await open({ title: '空压机停机检修', entityId: 'entity://demo-electronics/compressor/AIR-1', actionId: 'act-123', runId: 'run-live', kind: 'work_order' })

  const panel = document.querySelector('.evidence-panel')
  expect(panel).toBeTruthy()
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/explanation'))).toBe(true)
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('action_id=act-123'))).toBe(true)
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/api/v1/assets/'))).toBe(true)

  expect(panel!.classList.contains('path-retrieval')).toBe(true)
  expect(panel!.querySelector('.evidence-path')?.textContent).toBe('检索')
  expect(panel!.querySelector('.nature-badge')?.textContent).toContain('实测')
  expect(panel!.querySelector('.evidence-fallback')).toBeNull()

  // 模型填值与系统绑定值分开显示，原文摘录和资料片段都在第四段。
  expect(panel!.textContent).toContain('390')
  expect(panel!.textContent).toContain('压力下降速率超阈值')
  expect(panel!.textContent).toContain('entity://demo-electronics/compressor/AIR-1')
  expect(panel!.textContent).toContain('资料摘录：空压机压力下降速率超阈值先查管网。')
  expect(panel!.textContent).toContain('原文片段：停机前先关闭进气阀。')
  expect(panel!.textContent).not.toContain('后端没有记录模型填值')
})

it('真实证据缺 nature 时不按实测兜底，显示“来源未标注”', async () => {
  const event = {
    event_id: 'event://ev-no-nature',
    seq: 1,
    occurred_at: '2026-07-01T01:31:05Z',
    agent_run_id: 'ar-main',
    run_id: 'run-live',
    event_type: 'tool_called',
    tool: 'asset_search',
    path: 'reasoning',
    rule_ids: ['rule://synthetic-leak#pressure-drop'],
    sources: [],
    params: { entity_id: 'entity://demo-electronics/compressor/AIR-1' },
    result_status: 'ok',
    summary: '查询空压机维护手册',
  }
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/plans?')) return respond({ items: [{ plan_id: 'plan://p1' }], next_cursor: null })
    if (url.includes('/explanation')) return respond({ items: [event], next_cursor: null })
    return respond({ items: [], next_cursor: null })
  })
  vi.stubGlobal('fetch', fetcher)

  await open({ title: '空压机停机检修', entityId: 'entity://demo-electronics/compressor/AIR-1', runId: 'run-live', kind: 'work_order' })

  const panel = document.querySelector('.evidence-panel')
  expect(panel).toBeTruthy()
  const badge = panel!.querySelector('.nature-badge')
  expect(badge?.textContent).toContain('来源未标注')
  expect(badge?.textContent).not.toContain('实测')
  expect(panel!.querySelector('.nature-badge.live')).toBeNull()
})

it('在线模式（?run_id=）时证据请求优先用页面的 run_id，不用 day.run_id', async () => {
  const fetcher = vi.fn(() => respond({ items: [], next_cursor: null }))
  vi.stubGlobal('fetch', fetcher)
  window.history.pushState({}, '', '/?run_id=run-online')
  try {
    await open({ title: '空压机停机检修', entityId: 'entity://demo-electronics/compressor/AIR-1', kind: 'work_order' })
  } finally {
    window.history.pushState({}, '', '/')
  }
  const urls = fetcher.mock.calls.map(call => String((call as unknown[])[0]))
  expect(urls.some(url => url.includes('run_id=run-online'))).toBe(true)
})
