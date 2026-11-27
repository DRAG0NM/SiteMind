/**
 * 资料拖入与图谱生长：上传失败时显示错误；仅在显式选择离线演示时生成合成节点。
 */
import { useEffect, useRef, useState } from 'react'
import { NatureBadge, type DataNature } from '../common/nature'
import { prefersReducedMotion } from '../../shared/motion'
import { useDay } from '../../store'
import { GROW_KIND_LABEL, growGraph, publishedGraph, visibleEdges, type GrowGraph } from './graph'
import { KnowledgeGraph } from './KnowledgeGraph'
import { getFragments, getReceipt, getPublishedGraph, searchDocuments, uploadDocument, type IngestReceipt, type SearchHit } from './upload'
import './knowledge-drop.css'

const GROW_STEP_MS = 180
const ACCEPT = '.txt,.md,.markdown,.json,text/plain,text/markdown,application/json'
const SUPPORTED = /\.(txt|md|markdown|json)$/i

type Phase = 'idle' | 'reading' | 'matching' | 'growing' | 'done'

const PHASE_LABEL: Record<Phase, string> = {
  idle: '等待资料',
  reading: '读取中',
  matching: '匹配中',
  growing: '生长中',
  done: '完成',
}

/** 读文件文本；浏览器会给 onprogress，量不到时保持起始进度。 */
function readText(file: File, onProgress: (ratio: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onprogress = event => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total)
    }
    reader.onload = () => {
      try { resolve(new TextDecoder('utf-8', { fatal: true }).decode(reader.result as ArrayBuffer)) }
      catch { reject(new Error('资料必须是有效的 UTF-8 文本')) }
    }
    reader.onerror = () => reject(reader.error ?? new Error('文件读取失败'))
    reader.readAsArrayBuffer(file)
  })
}

export function KnowledgeDropScreen() {
  const day = useDay(state => state.day)
  const [phase, setPhase] = useState<Phase>('idle')
  const [progress, setProgress] = useState(0)
  const [dragOver, setDragOver] = useState(false)
  const [fileName, setFileName] = useState('')
  const [model, setModel] = useState<GrowGraph | null>(null)
  const [reveal, setReveal] = useState(0)
  const [note, setNote] = useState('')
  const [offline, setOffline] = useState(false)
  const [expert, setExpert] = useState('hvac')
  const [nature, setNature] = useState<DataNature>('synthetic')
  const [documentId, setDocumentId] = useState('')
  const [entityIds, setEntityIds] = useState('')
  const [receipt, setReceipt] = useState<IngestReceipt | null>(null)
  const [manifest, setManifest] = useState('')
  const [triples, setTriples] = useState<{ subject: string; predicate: string; object: string }[] | null>(null)
  const [graphNote, setGraphNote] = useState('')
  const retry = useRef<{ signature: string; key: string } | null>(null)
  const generation = useRef(0)
  const searchRequest = useRef(0)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // 本机默认强制播放动效；?motion=reduce 或系统减弱动效时直接显示最终图。
  const reduced = prefersReducedMotion()
  const busy = phase === 'reading' || phase === 'matching' || phase === 'growing'

  const readGraph = async (manifestId: string, request = generation.current) => {
    setGraphNote('读取中')
    try {
      const items = await getPublishedGraph(manifestId)
      if (request !== generation.current) return
      setTriples(items)
      const graph = publishedGraph(items, manifestId)
      setModel(graph)
      setReveal(graph.nodes.length)
      setGraphNote(items.length ? `正式图谱：${manifestId}；节点来源为后端三元组` : '此清单没有图谱记录')
    } catch (error) {
      if (request === generation.current) setGraphNote(`已发布图谱读取失败：${error instanceof Error ? error.message : '请重试'}`)
    }
  }

  const showReceipt = async (saved: IngestReceipt, name: string, request = generation.current) => {
    const fragments = await getFragments(saved)
    if (request !== generation.current) return
    setReceipt(saved)
    if (saved.expert) setExpert(saved.expert)
    setNature(saved.nature)
    const first = fragments[0]
    const node = { id: saved.asset_version_id, kind: 'document' as const, label: name,
      detail: saved.asset_version_id, nature: saved.nature, reason: '持久化原文；关联内容以正式发布清单为准',
      terms: [], excerpts: first?.text ? [`第 ${first.locator?.line_start ?? 1} 行：${first.text}`] : [] }
    setModel({ nodes: [node], edges: [], order: [node.id], matches: 0,
      stats: { document: 1, entity: 0, event: 0, ticket: 0, rule: 0, decision: 0, knowledge: 0 } })
    setReveal(1)
    setPhase('done')
    setProgress(100)
    setNote(saved.note)
    const url = new URL(window.location.href)
    url.searchParams.set('knowledge_asset', saved.asset_version_id)
    url.searchParams.set('knowledge_document', saved.document_id)
    window.history.replaceState(null, '', url)
    if (saved.manifest_ids?.length) {
      const manifestId = saved.manifest_ids.at(-1)!
      setManifest(manifestId)
      await readGraph(manifestId, request)
    }
  }

  useEffect(() => {
    const params = new URL(window.location.href).searchParams
    const assetId = params.get('knowledge_asset')
    const documentId = params.get('knowledge_document')
    if (assetId) {
      const request = generation.current
      void getReceipt(assetId, documentId ?? undefined).then(saved => {
        if (request === generation.current) return showReceipt(saved, saved.source_name || saved.document_id, request)
      }).catch(error => { if (request === generation.current) setNote(String(error)) })
    }
    return () => { generation.current += 1 }
  // 初次装载只恢复 URL 指定的持久化回执。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleFile = async (file: File) => {
    if (busy) {
      setNote('上一个资料还在处理，等它长完再拖新的')
      return
    }
    if (file.size > 1048576) { setNote('资料不能超过 1 MiB'); return }
    if (!SUPPORTED.test(file.name)) {
      setNote(`只支持 .txt / .md / .json 文本资料，「${file.name}」没有读取`)
      return
    }
    setNote('')
    setFileName(file.name)
    setProgress(8)
    setPhase('reading')
    setModel(null)
    setReceipt(null)
    setReveal(0)
    generation.current += 1
    setTriples(null)
    setManifest('')
    setGraphNote('')
    setDragOver(false)

    let text = ''
    try {
      text = await readText(file, ratio => setProgress(Math.max(8, Math.round(8 + 32 * ratio))))
    } catch (error) {
      setPhase('idle')
      setProgress(0)
      setNote(`文件读取失败：${error instanceof Error ? error.message : '请重试'}`)
      return
    }
    if (!text.trim()) {
      setPhase('idle')
      setProgress(0)
      setNote('文件是空的，没有可匹配的文字')
      return
    }

    if (!offline) {
      let saved: IngestReceipt
      try {
        const entities = entityIds.split(/\s+/).filter(Boolean)
        const signature = JSON.stringify([file.name, text, expert, nature, documentId, entities])
        if (retry.current?.signature !== signature) retry.current = { signature, key: crypto.randomUUID() }
        saved = await uploadDocument(file, text, expert, nature, documentId, entities, retry.current.key)
      } catch (error) {
        setPhase('idle')
        setProgress(0)
        setNote(`上传失败：${error instanceof Error ? error.message : '请重试'}`)
        return
      }
      setDocumentId('')
      setEntityIds('')
      setReceipt(saved)
      try {
        await showReceipt(saved, file.name)
      } catch (error) {
        setPhase('idle')
        setNote(`资料已入库，但原文读取失败：${error instanceof Error ? error.message : '请重试'}`)
      }
      return
    }

    setProgress(44)
    setPhase('matching')
    const graph = growGraph(day, text, file.name)
    setModel(graph)
    setProgress(reduced ? 100 : 45)
    setPhase('growing')
    setReveal(reduced ? graph.nodes.length : Math.min(1, graph.nodes.length))
    if (!graph.matches) setNote('当前资料未命中已有实体、事件或规则，已生成独立资料节点')
  }

  // 每 240ms 长出一个节点；减弱动效时直接落到最终图。
  useEffect(() => {
    if (!model) return
    const total = model.nodes.length
    if (reveal >= total) {
      setPhase('done')
      setProgress(100)
      return
    }
    if (reduced) {
      setReveal(total)
      return
    }
    const timer = window.setTimeout(() => {
      setReveal(count => Math.min(total, count + 1))
      setProgress(Math.min(99, 45 + Math.round(55 * ((reveal + 1) / Math.max(1, total)))))
    }, GROW_STEP_MS)
    return () => window.clearTimeout(timer)
  }, [model, reveal, reduced])

  const nodesShown = model ? model.nodes.slice(0, reveal) : []
  const edgesShown = model ? visibleEdges(model, reveal) : []
  const labels = new Map((model?.nodes ?? []).map(node => [node.id, node.label]))
  const graphHint = !model
    ? (offline ? '离线演示：按 day 记录匹配。' : '入库后显示原文；审核发布后读取正式清单图谱。')
    : phase === 'growing'
      ? `生长中 ${reveal} / ${model.nodes.length}`
      : `${model.nodes.length} 个节点 · ${model.edges.length} 条边`

  return (
    <div className="kd">
      <div className="kd-main">
        <section className="panel kd-drop">
          <div className="sec">
            <div className="sec-title">
              资料拖入与图谱生长
              {offline && <NatureBadge nature="synthetic" source="本地匹配" />}
            </div>
            <p className="dm kd-intro">
              拖入设备资料或操作手册。离线演示按 day 记录做本地关键词匹配。
            </p>
            <label><input type="checkbox" checked={offline} disabled={busy} onChange={event => {
              generation.current += 1
              searchRequest.current += 1
              setOffline(event.target.checked); setModel(null); setReceipt(null); setNote(''); setTriples(null)
              setGraphNote(''); setManifest(''); setProgress(0); setPhase('idle'); setFileName(''); retry.current = null
              setHits(null); setQuery('')
              const url = new URL(window.location.href); url.searchParams.delete('knowledge_asset'); url.searchParams.delete('knowledge_document'); window.history.replaceState(null, '', url)
            }} /> 离线演示（synthetic，不上传）</label>
            {!offline && <label>所属专家 <select value={expert} disabled={busy} onChange={event => { searchRequest.current += 1; setHits(null); setExpert(event.target.value) }}><option value="hvac">暖通</option><option value="elevator">电梯</option><option value="energy">能源</option><option value="equipment">设备监测</option></select></label>}
            {!offline && <label>资料性质 <select value={nature} disabled={busy} onChange={event => setNature(event.target.value as DataNature)}><option value="synthetic">合成</option><option value="simulated">仿真</option><option value="configured">配置</option><option value="derived">推算</option><option value="measured">实测</option></select></label>}
            {!offline && <label>已有资料编号（改版时填写） <input value={documentId} disabled={busy} onChange={event => setDocumentId(event.target.value)} /></label>}
            {!offline && <label>已登记实体 ID（可选，空格分隔） <input value={entityIds} disabled={busy} onChange={event => setEntityIds(event.target.value)} /></label>}

            <div
              className={'kd-zone' + (dragOver ? ' over' : '')}
              data-testid="kd-dropzone"
              role="button"
              tabIndex={0}
              aria-label="拖入或选择资料文件"
              onClick={() => inputRef.current?.click()}
              onKeyDown={event => {
                if (event.key !== 'Enter' && event.key !== ' ') return
                event.preventDefault()
                inputRef.current?.click()
              }}
              onDragEnter={event => {
                event.preventDefault()
                setDragOver(true)
              }}
              onDragOver={event => {
                event.preventDefault()
                if (!dragOver) setDragOver(true)
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={event => {
                event.preventDefault()
                setDragOver(false)
                const file = event.dataTransfer?.files?.[0]
                if (file) void handleFile(file)
              }}
            >
              <b>{busy ? `正在处理「${fileName}」` : '把资料拖到这里'}</b>
              <p className="dm">支持 .txt / .md / .json · 也可以点这里选文件</p>
              <input
                ref={inputRef}
                type="file"
                accept={ACCEPT}
                className="sr-only"
                data-testid="kd-file-input"
                onClick={event => event.stopPropagation()}
                onChange={event => {
                  const file = event.currentTarget.files?.[0]
                  event.currentTarget.value = ''
                  if (file) void handleFile(file)
                }}
              />
            </div>

            <div className="kd-progress">
              <span className="lb kd-phase">{PHASE_LABEL[phase]}</span>
              <div
                className="g kd-bar"
                role="progressbar"
                aria-label="读取与生长进度"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={progress}
              >
                <i className="x" style={{ width: `${progress}%` }} />
              </div>
              <b className="n">{progress}%</b>
            </div>

            <p className="dm kd-upload">{offline ? '离线演示：本地匹配，资料未上传到后端。' : '资料经受控接口入库；上传不等于发布。'}</p>
            {receipt && <div className="dm">
              <p>资料编号：{receipt.document_id} · 资产版本：{receipt.asset_version_id} · 状态：{receipt.status} · {receipt.published ? '已确认发布' : '未发布'}</p>
              <p>规则候选：{receipt.candidate_ids.join('、') || '无'} · 本体候选：{receipt.ontology_candidate_ids?.join('、') || '无'} · 无候选或无明确实体时不生成关联。</p>
              <p>审核：{JSON.stringify(receipt.reviews ?? {})} · 回测：{JSON.stringify(receipt.backtests ?? {})} · 待办：{receipt.action_ids?.join('、') || '无'}</p>
              <p>通过现有知识复核工具提交候选处理意见，再到<a href="#desk">确认卡</a>确认；发布仍需回测和人工确认。</p>
              <button type="button" disabled={busy} onClick={() => {
                const request = generation.current
                void getReceipt(receipt.asset_version_id, receipt.document_id).then(saved => {
                  if (request === generation.current) return showReceipt(saved, saved.source_name || receipt.document_id, request)
                }).catch(error => { if (request === generation.current) setNote(String(error)) })
              }}>刷新处理状态与图谱</button>
            </div>}
            {note && <p className="dm kd-note" role="status">{note}</p>}
            {!offline && <div>
              <label>专家资料检索 <input value={query} onChange={event => { searchRequest.current += 1; setQuery(event.target.value); setHits(null) }} /></label>
              <button type="button" disabled={!query.trim() || busy} onClick={() => {
                const request = generation.current
                const search = ++searchRequest.current
                void searchDocuments(expert, query).then(items => { if (request === generation.current && search === searchRequest.current) setHits(items) })
                  .catch(error => { if (request === generation.current && search === searchRequest.current) setNote(String(error)) })
              }}>检索已入库原文</button>
              <p className="dm">本地确定性检索，按专家与可见时间隔离；未同步至 Nexent。</p>
              {hits && (hits.length ? <ul>{hits.map((hit, index) => <li key={index}>{hit.asset_version_id} · 第 {hit.locator.line_start} 行 · {hit.text}</li>)}</ul> : <p>没有相关原文。</p>)}
            </div>}
            {model && model.matches > 0 && (
              <p className="dm kd-stats">
                命中 实体 {model.stats.entity} · 事件 {model.stats.event} · 事件单 {model.stats.ticket} · 裁定 {model.stats.decision} · 规则 {model.stats.rule} · 知识版本 {model.stats.knowledge}
              </p>
            )}
          </div>
        </section>

        <section className="panel kd-graph-panel">
          <div className="sec kd-graph-head">
            <div className="sec-title">{offline ? '知识图谱构建' : triples ? '已发布知识图谱' : '已入库资料'}</div>
            <span className="dm">{graphHint}</span>
          </div>
          <div className="kd-graph-body">
            {model
              ? <KnowledgeGraph model={model} reveal={reveal} animate={!reduced} />
              : <div className="kd-graph-empty dm">请拖入 .md / .txt / .json 资料文件，系统将实时解析并构建知识图谱。</div>}
          </div>
          {!offline && <div className="sec">
            <label>已发布清单 <input value={manifest} onChange={event => { generation.current += 1; setManifest(event.target.value); setTriples(null); setGraphNote(''); setModel(null) }} /></label>
            <button type="button" disabled={!manifest.trim()} onClick={() => {
              setTriples(null)
              setGraphNote('读取中')
              void readGraph(manifest.trim())
            }}>读取已发布图谱</button>
            {graphNote && <p role="status">{graphNote}</p>}
            {triples && <ul>{triples.map((triple, index) => <li key={index}>{triple.subject} · {triple.predicate} · {triple.object}</li>)}</ul>}
          </div>}
        </section>
      </div>

      <aside className="kd-side">
        <section className="panel kd-nodes-panel">
          <div className="sec kd-listhead">
            <div className="sec-title">新增节点<span className="dm">{model ? `${nodesShown.length} 个` : '—'}</span></div>
          </div>
          <div className="kd-list kd-nodes">
            {nodesShown.length
              ? nodesShown.map((node, index) => (
                <div className="kd-node" key={node.id}>
                  <div className="kd-node-top">
                    <span className="n kd-index">{index + 1}</span>
                    <b>{node.label}</b>
                    <span className="chip kd-kind">{GROW_KIND_LABEL[node.kind]}</span>
                    <NatureBadge nature={node.nature} />
                  </div>
                  <div className="dm kd-node-reason">{node.reason}</div>
                  {node.detail && node.detail !== node.label && <div className="dm kd-node-detail">{node.detail}</div>}
                  {node.excerpts[0] && <div className="kd-quote">原文：{node.excerpts[0]}</div>}
                </div>
              ))
              : <div className="kd-empty dm">还没有新增节点。</div>}
          </div>
        </section>

        <section className="panel kd-edges-panel">
          <div className="sec kd-listhead">
            <div className="sec-title">新增边<span className="dm">{model ? `${edgesShown.length} 条` : '—'}</span></div>
          </div>
          <div className="kd-list kd-edges">
            {edgesShown.length
              ? edgesShown.map(edge => (
                <div className="kd-edge" key={edge.id}>
                  <span className="chip kd-kind">{edge.label}</span>
                  <span className="lb kd-edge-path">
                    {labels.get(edge.source) ?? edge.source} → {labels.get(edge.target) ?? edge.target}
                  </span>
                </div>
              ))
              : <div className="kd-empty dm">关联节点解析完成后，图谱关系将在此显示。</div>}
          </div>
        </section>
      </aside>
    </div>
  )
}
