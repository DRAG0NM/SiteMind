import { expect, it, vi } from 'vitest'
import { buildEvidenceRuns, fetchEvidence } from './online'

it('在线证据超过一页时按 next_cursor 取完', async () => {
  const calls: string[] = []
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    calls.push(url)
    const second = url.includes('cursor=200')
    return Promise.resolve(new Response(JSON.stringify({
      items: second
        ? [{ seq: 201, occurred_at: '2026-07-01T01:32:00Z', event_type: 'retrieval', result_status: 'ok' }]
        : [{ seq: 1, occurred_at: '2026-07-01T01:31:00Z', event_type: 'tool_called', result_status: 'ok' }],
      next_cursor: second ? null : '200',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  })

  const items = await fetchEvidence('run-1', 'ar-1', fetcher)

  expect(items.map(item => item.seq)).toEqual([1, 201])
  expect(calls).toHaveLength(2)
  expect(calls[0]).toContain('page_size=200')
  expect(calls[0]).toContain('agent_run_id=ar-1')
  expect(calls[1]).toContain('cursor=200')
  expect(calls[1]).toContain('agent_run_id=ar-1')
})

it('空 agent_run_id 归到“这次运行”，不占第 N 次运行的序号', () => {
  const groups = buildEvidenceRuns([
    { seq: 1, occurred_at: '2026-07-01T01:31:00Z', event_type: 'tool_called', result_status: 'ok' },
    { seq: 2, occurred_at: '2026-07-01T01:32:00Z', event_type: 'tool_called', result_status: 'ok', agent_run_id: 'ar-1' },
    { seq: 3, occurred_at: '2026-07-01T01:33:00Z', event_type: 'tool_called', result_status: 'ok', agent_run_id: 'ar-2' },
  ], 'Asia/Shanghai')

  expect(groups.map(group => [group.runId, group.ordinal])).toEqual([['', 0], ['ar-1', 1], ['ar-2', 2]])
})
