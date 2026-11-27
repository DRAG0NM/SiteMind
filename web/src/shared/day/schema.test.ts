import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { expect, it } from 'vitest'
import { generateTypes } from '../../../scripts/gen-types.mjs'

/** 提交的 schema.gen.ts 必须就是 contracts/day.schema.json 当前生成的结果：改了契约忘了重跑就红。 */
it('schema.gen.ts 与契约重新生成的结果一致', async () => {
  const committed = await readFile(fileURLToPath(new URL('./schema.gen.ts', import.meta.url)), 'utf8')
  // 本机是 core.autocrlf 的 CRLF 检出，生成结果是 LF，比较前统一换行。
  expect(committed.replace(/\r\n/g, '\n')).toBe(await generateTypes())
})
