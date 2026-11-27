import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, expect, it, vi } from 'vitest'
import { operatorHeaders } from './vite.operator-token'

const dirs: string[] = []

afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true })
})

it('开发代理每次请求重读运维令牌，后端重启重写令牌后不用重启 dev', () => {
  const dir = mkdtempSync(join(tmpdir(), 'operator-token-'))
  dirs.push(dir)
  const file = join(dir, 'operator.token')

  writeFileSync(file, 'first-token\n')
  expect(operatorHeaders(file)?.['X-Plantkg-Operator-Token']).toBe('first-token')

  writeFileSync(file, 'second-token\n')
  expect(operatorHeaders(file)?.['X-Plantkg-Operator-Token']).toBe('second-token')
})

it('缺令牌时只在 debug 级别提示一次，不污染测试与构建输出', () => {
  const dir = mkdtempSync(join(tmpdir(), 'operator-token-'))
  dirs.push(dir)
  const file = join(dir, 'missing.token')
  const debug = vi.spyOn(console, 'debug').mockImplementation(() => { /* 不打印 */ })
  const log = vi.spyOn(console, 'log').mockImplementation(() => { /* 不打印 */ })

  expect(operatorHeaders(file)).toBeNull()
  expect(operatorHeaders(file)).toBeNull()
  expect(debug).toHaveBeenCalledTimes(1)
  expect(log).not.toHaveBeenCalled()
  debug.mockRestore()
  log.mockRestore()
})
