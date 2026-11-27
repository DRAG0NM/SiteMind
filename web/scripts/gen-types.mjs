// 从 contracts/day.schema.json 生成 web/src/shared/day/schema.gen.ts。
// 改了契约（contracts/build.py 重新生成 schema）就重跑 `npm run gen:types`；
// schema.test.ts 会检查提交的文件与重新生成的结果一致。
import { compile } from 'json-schema-to-typescript'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const contracts = fileURLToPath(new URL('../../contracts/', import.meta.url))
const schemaPath = `${contracts}day.schema.json`
const outputPath = fileURLToPath(new URL('../src/shared/day/schema.gen.ts', import.meta.url))
const selfPath = fileURLToPath(import.meta.url)

// banner 里不能带时间戳或本机路径：生成结果提交到仓库，换个人重跑必须逐字一致。
const banner = `/**
 * 本文件由 contracts/day.schema.json 生成，不要手改。
 * 重新生成：cd web && npm run gen:types（契约改了没重跑时 schema.test.ts 会失败）。
 */`

/**
 * 契约由 pydantic 导出，每个属性都带 title；json-schema-to-typescript 会把它们提升成独立别名
 * （TicketId1、Source2 之类），生成文件难读，属性顺序一换编号还会整体漂移。
 * 生成前去掉内联 schema 的 title，只保留 $defs 定义自己的 title（类型名仍取自契约）。
 */
function stripInlineTitles(node) {
  if (Array.isArray(node)) return node.map(stripInlineTitles)
  if (node === null || typeof node !== 'object') return node
  const copy = {}
  for (const [key, value] of Object.entries(node)) {
    if (key === 'title') continue
    if (key === 'properties') {
      // properties 的键是字段名，其中一个字段就叫 title，不能当 schema 关键字删掉。
      copy[key] = Object.fromEntries(Object.entries(value).map(([name, property]) => [name, stripInlineTitles(property)]))
    } else if (key === '$defs' || key === 'definitions') {
      copy[key] = Object.fromEntries(Object.entries(value).map(([name, definition]) => [name, keepOwnTitle(definition)]))
    } else {
      copy[key] = stripInlineTitles(value)
    }
  }
  return copy
}

function keepOwnTitle(definition) {
  if (definition === null || typeof definition !== 'object') return definition
  const copy = stripInlineTitles(definition)
  if (typeof definition.title === 'string') copy.title = definition.title
  return copy
}

export async function generateTypes() {
  const schema = JSON.parse(await readFile(schemaPath, 'utf8'))
  return compile(keepOwnTitle(schema), 'DayRecord', { bannerComment: banner, cwd: contracts })
}

if (process.argv[1] && resolve(process.argv[1]) === selfPath) {
  await writeFile(outputPath, await generateTypes())
  console.log(`已生成 ${outputPath}`)
}
