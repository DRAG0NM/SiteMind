import { expect, it, vi } from 'vitest'
import { getDay, getEvolutionTimeline, getWatchdog, runAgent, watchdogBackoff } from './api'

const events = [
  {type:'run_started',agent_run_id:'ar-1',seq:1,ts:'2026-07-01T00:00:00Z',payload:{agent_name:'plantkg_main'}},
  {type:'answer_delta',agent_run_id:'ar-1',seq:2,ts:'2026-07-01T00:00:01Z',payload:{text:'已处理'}},
  {type:'run_finished',agent_run_id:'ar-1',seq:3,ts:'2026-07-01T00:00:02Z',payload:{terminal_reason:'completed'}}
]
const sse = events.map(e=>`event: ${e.type}\r\ndata: ${JSON.stringify(e)}\r\n\r\n`).join('')

it('接收网关拆包的事件并识别运行终态', async () => {
  const encoder = new TextEncoder()
  const stream=new ReadableStream({start(c){c.enqueue(encoder.encode(sse.slice(0,31)));c.enqueue(encoder.encode(sse.slice(31)));c.close()}})
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(stream,{status:200})))
  const received:string[]=[]
  try { await runAgent('检查情况','run-1',Date.UTC(2026,6,1),e=>received.push(e.type)); expect(received).toEqual(events.map(e=>e.type)) } finally {vi.unstubAllGlobals()}
})

it('CRLF 在 \\r 与 \\n 之间分块时仍接收最后的终态事件', async () => {
  const encoder = new TextEncoder()
  const bytes = encoder.encode(sse)
  const stream=new ReadableStream({start(c){for(const byte of bytes)c.enqueue(Uint8Array.of(byte));c.close()}})
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(stream,{status:200})))
  const received:string[]=[]
  try { await runAgent('检查情况','run-1',Date.UTC(2026,6,1),e=>received.push(e.type)); expect(received).toEqual(events.map(e=>e.type)) } finally {vi.unstubAllGlobals()}
})

it('请求体按播放头带上 as_of，并透出后端错误详情', async () => {
  const fetchMock=vi.fn().mockImplementation(()=>Promise.resolve(new Response(JSON.stringify({error:{code:'INVALID_REQUEST',message:'文件与运行编号不一致：文件中的 run_id 为 run-9'}}),{status:400})))
  vi.stubGlobal('fetch',fetchMock)
  try {
    await expect(getDay('run-1')).rejects.toThrow('文件中的 run_id 为 run-9')
    await expect(runAgent('检查情况','run-1',Date.UTC(2026,6,1,2,30),()=>{})).rejects.toThrow('文件中的 run_id 为 run-9')
    const [url,init]=fetchMock.mock.calls[1]
    expect(url).toBe('/api/v1/agent/run')
    expect(JSON.parse(init.body)).toMatchObject({agent_name:'plantkg_main',run_id:'run-1',as_of:'2026-07-01T02:30:00.000Z'})
  } finally {vi.unstubAllGlobals()}
})

const watchdogFailure = async (status?: number) => {
  vi.stubGlobal('fetch', status
    ? vi.fn().mockResolvedValue(new Response('', { status }))
    : vi.fn().mockRejectedValue(new TypeError('fetch failed')))
  try { return await getWatchdog().catch((reason: unknown) => reason) } finally { vi.unstubAllGlobals() }
}

it('Watchdog 502/404 判定为需要退避，其它失败保持原轮询节奏', async () => {
  expect(watchdogBackoff(await watchdogFailure(502))).toBe(true)
  expect(watchdogBackoff(await watchdogFailure(404))).toBe(true)
  expect(watchdogBackoff(await watchdogFailure(500))).toBe(false)
  expect(watchdogBackoff(await watchdogFailure())).toBe(false)
})

it('进化时间线带 cursor 一路翻到最后一页，最新记录在末尾', async () => {
  const entry = (seq: number) => ({ seq, event: 'feedback_recorded', occurred_at: '2026-07-01T00:00:00Z', details: {} })
  const fetcher = vi.fn()
    .mockImplementationOnce(() => Promise.resolve(new Response(JSON.stringify({ items: [entry(1), entry(2)], next_cursor: '2' }))))
    .mockImplementationOnce(() => Promise.resolve(new Response(JSON.stringify({ items: [entry(3)], next_cursor: null }))))
  vi.stubGlobal('fetch', fetcher)
  const entries = await getEvolutionTimeline()
  expect(entries.map(item => item.seq)).toEqual([1, 2, 3])
  expect(String(fetcher.mock.calls[1][0])).toContain('cursor=2')
})

it('进化时间线可以从指定 seq 之后开始读', async () => {
  const fetcher = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ items: [], next_cursor: null }))))
  vi.stubGlobal('fetch', fetcher)
  await getEvolutionTimeline(41)
  expect(String((fetcher.mock.calls[0] as unknown[])[0])).toContain('cursor=41')
})
