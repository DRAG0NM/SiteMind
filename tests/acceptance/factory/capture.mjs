// node capture.mjs <showcase.html> <day1.json> <day2.json> <new-output-dir> [chrome.exe]
// Captures offline screenshots with Chrome DevTools; no model or backend is contacted.
import { spawn } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const [html, day1, day2, out, exe = 'C:/Program Files/Google/Chrome/Application/chrome.exe'] = process.argv.slice(2)
if (!out || !day2 || !day1 || !html) throw new Error('Expected html, day1, day2 and new output directory')
const firstDay = JSON.parse(await readFile(day1, 'utf8'))
const lastVersion = firstDay.knowledge_versions.at(-1).version
const comboSummary = firstDay.events.find(event => event.event_id === 'act4-combo').summary
await mkdir(out, { recursive: false })
const profile = resolve(out, 'chrome-profile')
const browser = spawn(exe, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, '--window-size=1600,1000', pathToFileURL(resolve(html)).href], { stdio: 'ignore' })
const delay = ms => new Promise(r => setTimeout(r, ms))
let ws
try {
  let port
  for (let i = 0; i < 100; i++) {
    try { port = (await readFile(resolve(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break } catch { await delay(100) }
  }
  if (!port) throw new Error('Chrome did not start')
  let page
  for (let i = 0; i < 100; i++) {
    const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
    page = targets.find(t => t.type === 'page' && t.url.startsWith('file:'))
    if (page) break
    await delay(100)
  }
  if (!page) throw Error('Chrome did not open the showcase file')
  ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((yes, no) => { ws.onopen = yes; ws.onerror = no })
  let id = 0
  const pending = new Map()
  ws.onmessage = e => { const m = JSON.parse(e.data); if (pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result) } }
  const send = (method, params = {}) => new Promise((resolve, reject) => { const n = ++id; pending.set(n, { resolve, reject }); ws.send(JSON.stringify({ id: n, method, params })) })
  const js = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true })).result.value
  const waitFor = async expression => { for (let i = 0; i < 150; i++) { if (await js(expression)) return; await delay(100) } throw Error(`Timed out: ${expression}; page=${await js('location.href')} body=${await js('document.body?.innerText.slice(0,300)')}`) }
  const click = async selector => { await waitFor(`!!document.querySelector(${JSON.stringify(selector)})`); await js(`document.querySelector(${JSON.stringify(selector)}).click()`) }
  const load = async file => {
    await waitFor('!!document.querySelector("input[type=file]")')
    const root = await send('DOM.getDocument')
    const node = await send('DOM.querySelector', { nodeId: root.root.nodeId, selector: 'input[type=file]' })
    await send('DOM.setFileInputFiles', { nodeId: node.nodeId, files: [resolve(file)] })
    await waitFor(`document.querySelector('.source')?.textContent.includes(${JSON.stringify(file.split(/[\\/]/).pop())})`)
    await delay(500)
  }
  const screenshot = async name => {
    // Wait for animated numeric readouts to settle after seeking.
    await delay(1200)
    const png = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    await writeFile(resolve(out, name + '.png'), Buffer.from(png.data, 'base64'))
  }
  await send('Page.enable'); await send('DOM.enable'); await send('Runtime.enable')
  await load(day1)
  await click('[aria-label="跳到 08:00"]')
  await click('[aria-label="播放"]')
  await waitFor("document.querySelector('.now b')?.textContent === '08:05'")
  await click('[aria-label="暂停"]'); await screenshot('day1-0805')
  await click('[aria-label="跳到 16:00"]'); await screenshot('day1-1600')
  await click('[data-tab="agent"]')
  await waitFor(`document.body.innerText.includes(${JSON.stringify(comboSummary)})`)
  await screenshot('day1-1600-agent')
  await click('[data-tab="overview"]')
  await click(`[aria-label="跳到知识 v${lastVersion} 激活时刻"]`)
  await waitFor("document.querySelector('.now b')?.textContent === '16:10'")
  await screenshot('day1-fork')
  await click('[data-tab="knowledge"]'); await screenshot('day1-knowledge')
  await click('[data-tab="hvac"]'); await screenshot('day1-hvac')
  await click('[data-tab="agent"]'); await screenshot('day1-agent')
  await click('.ex-push'); await screenshot('day1-agent-runs')
  await click('[aria-label="关闭证据抽屉"]'); await delay(500)
  await click('[data-tab="compare"]'); await screenshot('day1-comparison')
  const metric = group => firstDay.metrics.find(m => m.metric === 'electricity_cost' && m.group === group)?.value
  const saved = metric('baseline') === null || metric('coordinated') === null ? null : metric('baseline') - metric('coordinated')
  const savedText = saved === null ? '—' : `¥${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(saved)}`
  await waitFor(`document.querySelector('.cmp-saved')?.textContent === ${JSON.stringify(savedText)}`)
  await load(day2); await click('[aria-label="跳到 16:00"]'); await screenshot('day2-1600')
  await click('[data-tab="agent"]'); await screenshot('day2-agent')
  const secondDay = JSON.parse(await readFile(day2, 'utf8'))
  const b47 = secondDay.qa.find(entry => entry.question === 'B47 如何判断和执行？')
  if (!b47) throw new Error('The B47 record-backed answer is missing')
  const secondTicket = secondDay.tickets.find(ticket => ticket.event_ids.includes('day2-combo'))
  const equipment = secondDay.proposals.find(p => p.ticket_id === secondTicket.ticket_id && p.expert === 'equipment')
  if (await js(`document.body.textContent.includes(${JSON.stringify(equipment.proposal_id)})`)) throw new Error('The decision appeared before its proposal was effective')
  if (await js("document.body.innerText.includes('B47 如何判断和执行？')")) throw new Error('B47 result appeared before it was effective')
  await click('[aria-label="播放"]')
  await waitFor("document.querySelector('.now b')?.textContent === '16:01'")
  await click('[aria-label="暂停"]')
  await waitFor("[...document.querySelectorAll('button')].some(b => b.textContent === 'B47 如何判断和执行？')")
  await js("[...document.querySelectorAll('button')].find(b => b.textContent === 'B47 如何判断和执行？').click()")
  const displayedAnswer = b47.answer
    .replace(/proposal:\/\/[\w.-]+/g, '方案')
    .replace(/置信度 ?(0?\.\d+|1(?:\.0+)?)/g, (_, value) => `把握 ${Math.round(Number(value) * 100)}%`)
  await waitFor(`document.body.textContent.includes(${JSON.stringify(displayedAnswer)})`)
  await screenshot('day2-b47')
  await writeFile(resolve(out, 'capture.json'), JSON.stringify({ html: resolve(html), days: [resolve(day1), resolve(day2)],
    offline: true, forkVersion: lastVersion, comboSummary, comparisonSaved: savedText }, null, 2))
  await send('Browser.close')
} finally {
  if (ws) ws.close()
  browser.kill()
}
