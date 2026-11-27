import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/**
 * B35 登录接入后删除：本机开发时由开发代理替页面带上运维令牌，
 * 否则在普通浏览器里点批准/驳回会被确认端点按 403 拒绝。
 * 令牌只在开发服务器进程里读，不写进前端源码，也不会进构建产物。
 * 每个请求都重新读一次文件：后端每次启动会重写 runtime/operator.token，
 * 先起 dev 后起后端、或后端重启换了令牌，下一次请求就能用上，不用重启 dev。
 */
const DEFAULT_TOKEN_FILE = fileURLToPath(new URL('../runtime/operator.token', import.meta.url))
let warnedMissing = false

export function operatorHeaders(tokenFile = DEFAULT_TOKEN_FILE): Record<string, string> | null {
  let token = ''
  try {
    token = readFileSync(tokenFile, 'utf8').trim()
  } catch {
    token = ''
  }
  if (!token) {
    if (!warnedMissing) {
      warnedMissing = true
      console.debug('[dev] 没找到 runtime/operator.token，批准/驳回仍会被后端拒绝（403）；写好令牌文件后下一次请求自动带上，不用重启 dev。')
    }
    return null
  }
  warnedMissing = false
  return {
    'X-Plantkg-Operator-Token': token,
    'X-Plantkg-Actor': process.env.PLANTKG_ACTOR?.trim() || 'u-reviewer',
  }
}
