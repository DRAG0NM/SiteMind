// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { loadDay } from './loadDay'

afterEach(() => vi.unstubAllGlobals())

it('file:// 打开时直接读内置样例，不再请求 day.json', async () => {
  vi.stubGlobal('location', { protocol: 'file:' })
  const fetcher = vi.fn()
  const loaded = await loadDay(fetcher as unknown as typeof fetch)
  expect(fetcher).not.toHaveBeenCalled()
  expect(loaded.source.kind).toBe('sample')
  expect(loaded.source.label).toContain('标准演示场景')
})

it('静态托管 SPA 回退把 index.html 当 day.json 返回时仍用内置样例', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('<!doctype html><title>SiteMind</title>', {
    status: 200, headers: { 'Content-Type': 'text/html' },
  }))
  const loaded = await loadDay(fetcher as unknown as typeof fetch)
  expect(loaded.source.kind).toBe('sample')
  expect(loaded.source.label).toContain('标准演示场景')
})

it('day.json 返回 404 时仍回退内置样例', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('', { status: 404 }))
  const loaded = await loadDay(fetcher as unknown as typeof fetch)
  expect(loaded.source.kind).toBe('sample')
})

it('后端 5xx 不回退样例，抛出带 HTTP 状态的错误', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('', { status: 502 }))
  await expect(loadDay(fetcher as unknown as typeof fetch, { search: '?run_id=run-5xx', protocol: 'http:' }))
    .rejects.toThrow('后端出错（HTTP 502）')
  // 5xx 直接报错，不再去读 day.json 或替换成样例。
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('day.json 返回 5xx 时不回退内置样例', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response('', { status: 500 }))
  await expect(loadDay(fetcher as unknown as typeof fetch)).rejects.toThrow('后端出错（HTTP 500）')
})
