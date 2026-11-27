import type { PageProps } from './types'
import type { Day } from '../data/types'
import { deviceLabel, expertShort, refLabel } from '../data/labels'
import { latestKnowledge } from '../data/selectors'
import { resultVisibleAt, time } from '../data/time'
import './knowledge.css'

interface DocEntry {
  title: string
  note: string
  at: number | null
  active: boolean
  cited: number
  /** doc 是真的入库记录，ref 只是提案里的引用编号 */
  kind: 'doc' | 'ref'
  /** 引用原文编号；点开抽屉时用 */
  ref?: string
}

const RULE_LIKE = /^(rule|document|doc):\/\//
const titleOf = (text: string) => {
  const match = text.match(/《([^》]+)》/)
  return match ? match[1] : ''
}

/** 入库时刻；不是当天的补上日期，避免只看到 18:00 分不清是哪天。 */
function stamp(at: number, timeZone: string, localDate: string) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(at)
  const get = (type: string) => parts.find(part => part.type === type)?.value ?? ''
  const time = `${get('hour')}:${get('minute')}`
  return `${get('month')}-${get('day')}` === localDate.slice(5) ? time : `${get('month')}-${get('day')} ${time}`
}

/** 资料清单：知识版本摘要里的书名号、资料入库事件、提案引用里的 document:// 三处合一，按标题去重。 */
function collectDocs({ day, cursor }: Pick<PageProps, 'day' | 'cursor'>) {
  const docs = new Map<string, DocEntry>()
  // 引用计数按资料标题记：document://maintenance-manual 和《空压机运行维护手册》是同一份资料。
  const cited = new Map<string, { count: number; ref: string }>()
  for (const proposal of day.proposals) {
    // 引用属于提案结果，须等结果在仿真时间中生效。
    if (resultVisibleAt(proposal) > cursor) continue
    for (const ref of proposal.evidence_refs ?? []) {
      if (!ref.startsWith('document://')) continue
      // 未知资料不并成同一张“资料原文”卡，按路径末段区分。
      const label = refLabel(ref)
      const title = label === '资料原文' ? ref.split('/').filter(Boolean).pop() ?? label : label
      const hit = cited.get(title)
      if (hit) hit.count += 1
      else cited.set(title, { count: 1, ref })
    }
  }
  for (const version of day.knowledge_versions) {
    const title = titleOf(version.summary ?? '')
    if (!title) continue
    docs.set(title, {
      title,
      note: version.summary ?? '',
      at: time(version.activated_at),
      active: time(version.activated_at) <= cursor,
      cited: cited.get(title)?.count ?? 0,
      kind: 'doc',
    })
  }
  for (const event of day.events) {
    if (!/document|ingest|资料/i.test(event.event_type)) continue
    const title = titleOf(event.summary) || String(event.payload?.document ?? '')
    if (!title) continue
    if (!docs.has(title)) {
      docs.set(title, {
        title,
        note: event.summary,
        at: time(event.at),
        active: time(event.at) <= cursor,
        cited: cited.get(title)?.count ?? 0,
        kind: 'doc',
      })
    }
  }
  for (const [title, hit] of cited) {
    if (docs.has(title)) continue
    docs.set(title, { title, note: '只在提案引用里出现，没有入库记录', at: null, active: true, cited: hit.count, kind: 'ref', ref: hit.ref })
  }
  return [...docs.values()].sort((a, b) => (a.at ?? 0) - (b.at ?? 0))
}

/**
 * 资产四态，顺序与 plantkg/knowledge/assets/catalog.py 的 STAGES 一致：
 * dormant（沉睡）→ identified（已标识）→ in_graph（已入图）→ referenced（被引用）。
 * 被引用不等于产生业务收益，激活率只作辅助指标。
 */
type Stage = 'dormant' | 'identified' | 'in_graph' | 'referenced'
const STAGES: { key: Stage; label: string; hint: string }[] = [
  { key: 'dormant', label: '沉睡', hint: '入库了，还没有被检索' },
  { key: 'identified', label: '已标识', hint: '被检索到，关联了实体' },
  { key: 'in_graph', label: '已入图', hint: '进了知识图谱' },
  { key: 'referenced', label: '被引用', hint: '被规则或决策引用' },
]
const STAGE_INDEX: Record<Stage, number> = { dormant: 0, identified: 1, in_graph: 2, referenced: 3 }
const STAGE_LABEL: Record<Stage, string> = { dormant: '沉睡', identified: '已标识', in_graph: '已入图', referenced: '被引用' }
/** 证据事件 → 资产阶段；没有资产就退回到按资料状态近似，页面上会写明。 */
const EVIDENCE_STAGE: Record<string, Stage> = {
  retrieval: 'identified',
  graph_query: 'in_graph',
  rule_lookup: 'referenced',
  rule_applied: 'referenced',
  plan_created: 'referenced',
  version_activated: 'referenced',
}

/** 资料阶段：未生效算沉睡，被提案引用算被引用，生效的资料已经进了图谱。 */
function docStage(doc: DocEntry): Stage {
  if (!doc.active) return 'dormant'
  if (doc.cited > 0) return 'referenced'
  return doc.kind === 'ref' ? 'identified' : 'in_graph'
}

/** 真实导出的 assets + evidence：阶段由证据事件推出，不靠页面猜。 */
function assetStages(day: Day, cursor: number) {
  const assets = (day.assets ?? []).filter(asset => (asset.available_at ? time(asset.available_at) <= cursor : false))
  const events = (day.evidence ?? []).filter(event => time(event.occurred_at) <= cursor)
  return assets.map(asset => {
    const stages: Stage[] = ['dormant']
    for (const event of events) {
      const hits = (event.sources ?? []).some(source => source.asset_version_id === asset.asset_version_id)
      if (hits) stages.push(EVIDENCE_STAGE[event.event_type] ?? 'identified')
    }
    const stage = stages.sort((a, b) => STAGE_INDEX[a] - STAGE_INDEX[b])[stages.length - 1]
    return { asset, stage }
  })
}

/**
 * 图谱生长的三个阶段：开场（第一张事件单进图）、插曲资料入库、纠错附资料。
 * 时刻全部来自当天数据，缺哪一段就少显示一段，不补造节点。
 */
function growthPhases(day: Day) {
  const firstProposal = day.proposals
    .map(proposal => (proposal.binding?.trigger_at ? time(proposal.binding.trigger_at) : null))
    .filter((at): at is number => at !== null)
    .sort((a, b) => a - b)[0]
  const firstIngest = day.events
    .filter(event => /document|ingest|资料/i.test(event.event_type))
    .map(event => time(event.at))
    .sort((a, b) => a - b)[0]
  const correction = [...day.knowledge_versions]
    .sort((a, b) => time(a.activated_at) - time(b.activated_at))
    .map(version => time(version.activated_at))
    .pop()
  const phases: { key: string; label: string; at: number; detail: string }[] = []
  const add = (key: string, label: string, at: number | undefined, detail: string) => {
    if (at === undefined || phases.some(phase => phase.at === at)) return
    phases.push({ key, label, at, detail })
  }
  add('open', '开场', firstProposal, '第一张事件单的依据进图')
  add('insert', '插曲资料入库', firstIngest, '新资料抽成本体与规则')
  add('correct', '纠错附资料', correction, '纠错结果沉淀成规则')
  return phases.sort((a, b) => a.at - b.at)
}

interface GraphNode { id: string; label: string; kind: 'knowledge' | 'ref' | 'proposal' | 'device'; column: number; row: number; hot: boolean }
interface GraphEdge { from: string; to: string; hot: boolean }

/** 依据 → 提案 → 设备，加上知识版本 → 提案：全部由 evidence_refs、actions.entity_id 与 binding.knowledge_version 推出。 */
function buildGraph({ day, cursor }: Pick<PageProps, 'day' | 'cursor'>) {
  const nodes = new Map<string, GraphNode>()
  const edges: GraphEdge[] = []
  const seen = new Set<string>()
  const current = latestKnowledge(day, cursor)?.version ?? 0
  const push = (node: Omit<GraphNode, 'row' | 'hot'> & { hot?: boolean }) => {
    if (!nodes.has(node.id)) nodes.set(node.id, { ...node, row: 0, hot: node.hot ?? false })
    return node.id
  }
  const link = (from: string, to: string, hot: boolean) => {
    const key = from + '→' + to
    if (seen.has(key)) return
    seen.add(key)
    edges.push({ from, to, hot })
  }
  const versions = new Map(day.knowledge_versions.map(version => [version.version, version]))
  for (const proposal of day.proposals) {
    if (!proposal.proposal_id) continue
    const binding = proposal.binding
    if (resultVisibleAt(proposal) > cursor) continue
    const knowledgeVersion = binding?.knowledge_version ?? null
    const hotVersion = knowledgeVersion === current
    const pid = push({ id: `p:${proposal.proposal_id}`, label: expertShort(proposal.expert), kind: 'proposal', column: 2, hot: hotVersion })
    if (knowledgeVersion !== null && versions.has(knowledgeVersion)) {
      const kid = push({ id: `k:${knowledgeVersion}`, label: `知识 v${knowledgeVersion}`, kind: 'knowledge', column: 0, hot: hotVersion })
      link(kid, pid, hotVersion)
    }
    for (const entityId of (proposal.actions ?? []).map(action => action.entity_id)) {
      const did = push({ id: `d:${entityId}`, label: deviceLabel(entityId), kind: 'device', column: 3, hot: hotVersion })
      link(pid, did, hotVersion)
    }
    for (const ref of proposal.evidence_refs ?? []) {
      if (!RULE_LIKE.test(ref) && !ref.startsWith('entity://')) continue
      if (ref.startsWith('entity://')) {
        const did = push({ id: `d:${ref}`, label: deviceLabel(ref), kind: 'device', column: 3, hot: hotVersion })
        link(pid, did, hotVersion)
        continue
      }
      const rid = push({ id: `r:${ref}`, label: refLabel(ref), kind: 'ref', column: 1, hot: hotVersion })
      link(rid, pid, hotVersion)
    }
  }
  const columns = [0, 1, 2, 3]
  const rowsUsed = new Map<number, number>()
  for (const node of [...nodes.values()].sort((a, b) => a.column - b.column || a.label.localeCompare(b.label))) {
    const row = rowsUsed.get(node.column) ?? 0
    node.row = row
    rowsUsed.set(node.column, row + 1)
  }
  const width = 960
  const height = 560
  const colX = (column: number) => 60 + column * ((width - 120) / 3)
  const maxRows = Math.max(1, ...[...rowsUsed.values()])
  const position = (node: GraphNode) => ({ x: colX(node.column), y: 50 + (node.row + 0.5) * ((height - 100) / maxRows) })
  void columns
  return { nodes: [...nodes.values()], edges, position, width, height }
}

export function KnowledgePage({ day, cursor, openEvidence }: PageProps) {
  const docs = collectDocs({ day, cursor })
  const graph = buildGraph({ day, cursor })
  const current = latestKnowledge(day, cursor)
  const versions = [...day.knowledge_versions].sort((a, b) => time(a.activated_at) - time(b.activated_at))
  const documents = docs.filter(doc => doc.kind === 'doc')
  const citedCount = documents.filter(doc => doc.cited > 0).length
  const ruleRefs = Array.from(new Set(day.proposals
    .filter(proposal => resultVisibleAt(proposal) <= cursor)
    .flatMap(proposal => proposal.evidence_refs ?? []).filter(ref => ref.startsWith('rule://'))))
  const nodeById = new Map(graph.nodes.map(node => [node.id, node]))

  // 四态：真实导出优先用 assets + evidence 推，没有 assets 时按资料状态近似并写明。
  const assetRows = assetStages(day, cursor)
  const docStages = docs.map(doc => docStage(doc))
  const stageCounts = STAGES.map(stage => ({ ...stage, count: docStages.filter(item => item === stage.key).length }))
  // 全量目录：B36 在 metrics 的 knowledge 组给出官方汇总（#238 第 9 项）：asset_total、asset_activated。
  // 四态计数兼容两种在研形状：asset_state_counts.<stage> 一项一条，或单条 asset_state_counts 带 counts 对象（PR268）；
  // 缺其中任何一项就退回证据覆盖的子集，避免激活率按全量、四态按子集同屏打架。
  const knowledgeMetric = (name: string) => {
    const value = day.metrics.find(item => item.metric === name && item.group === 'knowledge')?.value
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }
  const officialCount = (stage: string) => {
    const split = knowledgeMetric(`asset_state_counts.${stage}`)
    if (split !== null) return split
    const metric = day.metrics.find(item => item.metric === 'asset_state_counts' && item.group === 'knowledge')
    const value = (metric as { counts?: Record<string, unknown> | null } | undefined)?.counts?.[stage]
    return typeof value === 'number' && Number.isFinite(value) ? value : null
  }
  const assetTotal = knowledgeMetric('asset_total')
  const assetActivated = knowledgeMetric('asset_activated')
  const officialCounts = STAGES.map(stage => officialCount(stage.key))
  const hasOfficialCounts = officialCounts.every(count => count !== null)
  const assetStageCounts = STAGES.map((stage, index) => ({
    ...stage,
    count: hasOfficialCounts ? officialCounts[index] as number : assetRows.filter(row => row.stage === stage.key).length,
  }))
  const referencedAssets = assetRows.filter(row => row.stage === 'referenced').length
  const fullCatalog = assetTotal !== null && assetActivated !== null && hasOfficialCounts
  const activation = fullCatalog
    ? `${assetActivated} / ${assetTotal}`
    : assetRows.length
      ? `${referencedAssets} / ${assetRows.length}`
      : documents.length ? `${citedCount} / ${documents.length}` : '—'
  const activationUnit = fullCatalog ? '份资产已激活' : assetRows.length ? '份资产被引用' : '份资料被决策引用'
  const alignment = day.metrics.find(item => item.metric === 'standard_alignment_rate' && item.value !== null)
  const alignmentText = alignment && alignment.value !== null
    ? `${(alignment.unit === '%' ? alignment.value : alignment.value * 100).toFixed(0)}%`
    : '—'
  const phaseSnapshots = growthPhases(day).map(phase => ({ ...phase, snapshot: buildGraph({ day, cursor: phase.at }) }))

  return (
    <div className="knowledge-page">
      <section className="panel kn-docs">
        <div className="sec">
          <div className="sec-title">资料入库<span className="dm">
            {documents.length} 份{docs.length > documents.length ? ` · 引用 ${docs.length - documents.length} 条` : ''}
          </span></div>
        </div>
        <div className="kn-stages" aria-label="资料激活状态">
          {stageCounts.map(stage => (
            <span key={stage.key} title={stage.hint}>
              <i className={'kn-dot st-' + stage.key} aria-hidden="true" />{stage.label}
              <b className="n">{stage.count}</b>
            </span>
          ))}
        </div>
        <div className="kn-doclist">
          {docs.length === 0 && <p className="dm kn-empty">这一天没有资料入库记录。</p>}
          {docs.map((doc, index) => {
            const stage = docStages[index]
            return (
              <button
                type="button"
                key={doc.title}
                className={'kn-doc' + (doc.active ? ' on' : '')}
                title={doc.note}
                onClick={() => openEvidence(doc.ref ?? (doc.cited ? `document://${doc.title}` : null), doc.title)}
              >
                <b>
                  <i className={'kn-dot st-' + stage} aria-hidden="true" />
                  {doc.kind === 'doc' ? `《${doc.title}》` : doc.title}
                </b>
                <span className="dm">
                  {doc.at === null ? doc.note : `${stamp(doc.at, day.timezone, day.local_date)} · ${doc.active ? STAGE_LABEL[stage] : '未生效'}`}
                  {doc.cited > 0 ? ` · 被 ${doc.cited} 条提案引用` : ''}
                </span>
              </button>
            )
          })}
        </div>
        {(assetRows.length > 0 || hasOfficialCounts) && (
          <div className="kn-realassets">
            <div className="sec-title kn-sub">资产状态<span className="dm">来自 day.json assets + evidence</span>
              {!(fullCatalog && hasOfficialCounts) && <span className="chip">证据覆盖子集 · 推断值</span>}
            </div>
            <div className="kn-assetlist">
              {assetRows.map(row => (
                <button type="button" className={'kn-asset st-' + row.stage} key={row.asset.asset_version_id}
                  onClick={() => openEvidence(row.asset.asset_version_id, row.asset.title ?? row.asset.asset_version_id)}>
                  <b><i className={'kn-dot st-' + row.stage} aria-hidden="true" />{row.asset.title ?? row.asset.asset_version_id}</b>
                  <span className="dm">
                    {row.asset.available_at ? stamp(time(row.asset.available_at), day.timezone, day.local_date) : '入库时刻未导出'} · {STAGE_LABEL[row.stage]}（推断）
                  </span>
                </button>
              ))}
            </div>
            <div className="kn-stages kn-stages-inline">
              {assetStageCounts.map(stage => (
                <span key={stage.key} title={stage.hint}>
                  <i className={'kn-dot st-' + stage.key} aria-hidden="true" />{stage.label}
                  <b className="n">{stage.count}</b>
                </span>
              ))}
              {hasOfficialCounts && <span className="chip">全天汇总 · 不随回放头</span>}
            </div>
          </div>
        )}
        <div className="sec kn-assets">
          <span className="lb">{fullCatalog ? '全厂资产激活率' : '资产激活率'}</span>
          <b className="n">{activation}</b>
          <span className="dm">{activationUnit}</span>
          {fullCatalog ? <span className="chip">全天汇总 · 不随回放头</span> : <span className="chip">证据覆盖子集 · 推断值</span>}
          <span className="lb kn-sp">标准对齐率</span>
          <b className="n kn-align">{alignmentText}</b>
          <span className="dm">{alignment ? '规则条款已定位到标准原文' : '当前快照暂未包含对齐率指标'}</span>
        </div>
      </section>

      <div className="kn-middle">
        <section className="panel kn-graph">
          <div className="sec">
            <div className="sec-title">全局图谱<span className="dm">知识版本 → 依据 → 提案 → 设备</span>
              <span className="dm kn-legend"><i className="kn-dot hot" />当前知识 v{current?.version ?? '—'}</span>
            </div>
          </div>
          <div className="kn-graphbody">
            {graph.nodes.length === 0 ? <p className="dm kn-empty">这一天还没有可画的依据链。</p> : (
              <svg viewBox={`0 0 ${graph.width} ${graph.height}`} width="100%" height="100%" role="img"
                aria-label={`依据图谱：${graph.nodes.length} 个节点、${graph.edges.length} 条边`}>
                {graph.edges.map(edge => {
                  const from = nodeById.get(edge.from)
                  const to = nodeById.get(edge.to)
                  if (!from || !to) return null
                  const a = graph.position(from)
                  const b = graph.position(to)
                  return <line key={edge.from + edge.to} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                    stroke={edge.hot ? 'var(--accent)' : 'var(--line-strong)'} strokeWidth={edge.hot ? 1.6 : 1} />
                })}
                {graph.nodes.map(node => {
                  const point = graph.position(node)
                  const radius = node.kind === 'proposal' ? 16 : node.kind === 'knowledge' ? 14 : 9
                  return (
                    <g key={node.id} className="kn-nodewrap"
                      onClick={() => openEvidence(node.kind === 'device' ? node.id.slice(2) : null, node.label)}>
                      <circle cx={point.x} cy={point.y} r={radius}
                        fill={node.hot ? 'var(--text)' : node.kind === 'ref' ? 'var(--panel-3)' : 'var(--line-strong)'}
                        stroke={node.hot ? 'var(--text)' : 'var(--line-hover)'} strokeWidth="1.2" />
                      <text x={point.x} y={point.y + radius + 15} textAnchor="middle" fontSize="13"
                        fill={node.hot ? 'var(--text)' : 'var(--text-2)'}>{node.label}</text>
                    </g>
                  )
                })}
              </svg>
            )}
          </div>
        </section>

        <section className="panel kn-growth">
          <div className="sec">
            <div className="sec-title">图谱生长<span className="dm">开场 → 插曲资料入库 → 纠错附资料</span></div>
          </div>
          <div className="kn-growthbody">
            {phaseSnapshots.length === 0 && <p className="dm kn-empty">这一天没有可对齐的图谱生长节点。</p>}
            {phaseSnapshots.map((phase, index) => {
              const active = phase.at <= cursor && (index === phaseSnapshots.length - 1 || cursor < phaseSnapshots[index + 1].at)
              return (
                <div className={'kn-phase' + (active ? ' on' : phase.at > cursor ? ' future' : '')} key={phase.key}>
                  <span className="dm n">{stamp(phase.at, day.timezone, day.local_date)}</span>
                  <b>{phase.label}</b>
                  <span className="n">{phase.at > cursor
                    ? '待生成'
                    : `${phase.snapshot.nodes.length} 节点 · ${phase.snapshot.edges.length} 条边`}</span>
                  <span className="dm">{phase.detail}</span>
                </div>
              )
            })}
          </div>
          <p className="dm kn-growthnote">阶段按当天实际进图、入库与纠错时刻切分；未到的阶段先置灰，不预画未来节点。</p>
        </section>

        <section className="panel kn-rule">
          <div className="kn-rulebody">
            <div>
              <span className="lb">知识 v{current?.version ?? '—'} 的原文</span>
              <p>{current?.summary ?? '回放到这一刻还没有生效的知识版本。'}</p>
            </div>
            <span className="kn-arrow" aria-hidden="true">→</span>
            <div>
              <span className="lb">抽出的规则引用</span>
              {ruleRefs.length === 0
                ? <p className="dm">当日没有引用到规则原文。</p>
                : <p className="n">{ruleRefs.map(ref => refLabel(ref)).join('、')}</p>}
            </div>
          </div>
        </section>
      </div>

      <section className="panel kn-evolution">
        <div className="sec">
          <div className="sec-title">知识进化<span className="dm">按激活时刻排</span></div>
        </div>
        <div className="kn-timeline">
          {versions.length === 0 && <p className="dm kn-empty">这一天没有知识版本记录。</p>}
          {versions.map(version => {
            const at = time(version.activated_at)
            const active = at <= cursor
            const isCurrent = current?.version === version.version
            return (
              <div className={'kn-vrow' + (isCurrent ? ' on' : '') + (active ? '' : ' future')} key={version.version}
                title={`知识版本 ${version.version_manifest_id}`}>
                <b className="n kn-vtag">知识 v{version.version}</b>
                <div>
                  <span className="n dm">{stamp(at, day.timezone, day.local_date)}</span>
                  <p>{version.summary ?? '没有说明'}</p>
                  {!active && <span className="dm">未生效</span>}
                </div>
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}


