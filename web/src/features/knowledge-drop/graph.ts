/**
 * 资料拖入后的图谱生长：只做数据，不碰 DOM 与 G6。
 * 从 day 记录（实体、事件、事件单、规则、裁定、知识版本）里取名称与关键词，
 * 和拖入的文本做包含匹配，得到新增节点、边与生长顺序。
 * growGraph 仅供显式离线演示；publishedGraph 只转换后端已发布三元组。
 */
import { areaOf, SPOT_LABEL, spotOf, type CampusBuilding } from '../../shared/campus'
import { outcomeLabel } from '../../shared/labels'
import type { DataNature } from '../common/nature'
import type { Day } from '../../store'

export type GrowNodeKind = 'document' | 'entity' | 'event' | 'ticket' | 'rule' | 'decision' | 'knowledge'

export type GrowEdgeKind = 'mentions' | 'occurred_at' | 'applies_to' | 'merged_into' | 'decided_by' | 'based_on'

export interface GrowNode {
  id: string
  kind: GrowNodeKind
  /** 画布与清单共用的一行短名 */
  label: string
  /** 节点的完整说明（事件摘要、规则原文、实体编号等） */
  detail: string
  /** 数据来源性质；离线匹配固定 synthetic */
  nature: DataNature
  /** 命中的依据，直接显示给人看 */
  reason: string
  /** 命中的关键词（资料节点为空） */
  terms: string[]
  /** 原始文本里命中的片段 */
  excerpts: string[]
}

export interface GrowEdge {
  id: string
  source: string
  target: string
  kind: GrowEdgeKind
  /** 边上的关系名（提及 / 发生在 / 适用于 / 并入 / 裁定 / 对照版本） */
  label: string
}

export interface GrowGraph {
  nodes: GrowNode[]
  edges: GrowEdge[]
  /** 生长顺序：节点 id，资料节点固定第一，其余按关键词在原文里首次出现的位置排 */
  order: string[]
  /** 命中的节点数（不含资料节点） */
  matches: number
  stats: Record<GrowNodeKind, number>
}

export const GROW_KIND_LABEL: Record<GrowNodeKind, string> = {
  document: '资料',
  entity: '实体',
  event: '事件',
  ticket: '事件单',
  rule: '规则',
  decision: '裁定',
  knowledge: '知识版本',
}

const EDGE_LABEL: Record<GrowEdgeKind, string> = {
  mentions: '提及',
  occurred_at: '发生在',
  applies_to: '适用于',
  merged_into: '并入',
  decided_by: '裁定',
  based_on: '对照版本',
}

/** 事件类型的中文名。合成数据里没收录的类型直接保留原值。 */
const EVENT_LABEL: Record<string, string> = {
  elevator_peak: '电梯早高峰',
  transformer_overload: '变压器越限',
  operator_instruction: '值班口头指示',
  document_ingest: '资料入库',
  air_pressure_abnormal: '空压泄漏',
  lighting_fault: '照明故障',
  equipment_vibration: '设备振动',
  elevator_fault: '电梯困人',
  compound_anomaly: '复合异常',
  valley_charge: '储能谷充',
  hvac_overload: '暖通过载',
  pv_surge: '光伏出力骤升',
}

const AREA_TERM: Record<CampusBuilding, string> = {
  factory: '生产车间',
  admin: '行政楼',
  energy: '能源站',
  air: '空压站',
  pv: '光伏车棚',
  cooling: '冷却塔',
}

const trimLabel = (text: string, max = 12) => (text.length > max ? `${text.slice(0, max)}…` : text)

/** 生长到第 target 个节点时可见的边：两端都已经长出来。 */
export function visibleEdges(model: GrowGraph, target: number): GrowEdge[] {
  const ids = new Set(model.nodes.slice(0, target).map(node => node.id))
  return model.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target))
}

/** 三元组保留完整来源 ID；这里只进行画布转换，不添加业务关系。 */
export function publishedGraph(rows: { subject: string; predicate: string; object: string }[], manifestId: string): GrowGraph {
  const nodes = new Map<string, GrowNode>()
  const edges: GrowEdge[] = []
  for (const row of rows) {
    for (const id of [row.subject, row.object]) {
      if (!nodes.has(id)) nodes.set(id, { id, kind: id.includes('asset://') ? 'document' : 'entity',
        label: id.replace(/[<>]/g, '').split(/[/#]/).pop() || id, detail: id, nature: 'derived',
        reason: `已发布清单 ${manifestId} 的图谱记录`, terms: [], excerpts: [] })
    }
    edges.push({ id: JSON.stringify([row.subject, row.predicate, row.object]), source: row.subject, target: row.object,
      kind: 'based_on', label: row.predicate.replace(/[<>]/g, '').split(/[/#]/).pop() || row.predicate })
  }
  const list = [...nodes.values()]
  return { nodes: list, edges, order: list.map(node => node.id), matches: 0,
    stats: { document: list.filter(node => node.kind === 'document').length, entity: list.filter(node => node.kind === 'entity').length,
      event: 0, ticket: 0, rule: 0, decision: 0, knowledge: 0 } }
}

const snippet = (text: string, index: number, length: number) => {
  const start = Math.max(0, index - 14)
  const end = Math.min(text.length, index + length + 14)
  const body = text.slice(start, end).replace(/\s+/g, ' ')
  return `${start > 0 ? '…' : ''}${body}${end < text.length ? '…' : ''}`
}

/**
 * 中文关键词：短词（2–4 字）整段使用；更长的句子切 4 字片段。
 * 不切二字词，“运行”“设备”这类泛词会把不相关的记录全拉进图谱。纯数字与泛词丢掉。
 */
const STOP_TERMS = new Set([
  '设备', '运行', '记录', '状态', '系统', '数据', '处理', '进行', '相关',
  '情况', '时间', '工作', '人员', '事件', '规则', '知识', '版本', '资料', '文档',
])

function keywords(text: string, limit = 30): string[] {
  const terms = new Set<string>()
  for (const run of text.match(/[\u4e00-\u9fa5]{2,}/g) ?? []) {
    if (run.length <= 4) {
      terms.add(run)
      continue
    }
    for (let i = 0; i + 4 <= run.length && terms.size < limit; i += 1) terms.add(run.slice(i, i + 4))
  }
  return [...terms].filter(term => !/^[\d.\s]+$/.test(term) && !STOP_TERMS.has(term)).slice(0, limit)
}

/** 实体名：优先用 campus 的落位名（T1 变压器、储能），认不出时退回实体编号末段。 */
function entityInfo(entityId: string) {
  const spot = spotOf(entityId)
  const tail = entityId.split('/').filter(Boolean).pop() ?? entityId
  const area = areaOf(entityId)
  const label = spot ? SPOT_LABEL[spot] : tail
  const terms = new Set<string>()
  terms.add(label)
  if (tail.length >= 2) terms.add(tail)
  if (area) terms.add(AREA_TERM[area])
  return { label, tail, terms: [...terms] }
}

interface Hit {
  index: number
  terms: string[]
  excerpts: string[]
}

/** 在原文里找关键词。取最早出现的那处作为生长位置。 */
function findHit(terms: string[], text: string, lower: string): Hit | null {
  const hits: { term: string; index: number }[] = []
  for (const term of terms) {
    if (term.length < 2) continue
    const index = lower.indexOf(term.toLowerCase())
    if (index >= 0) hits.push({ term, index })
  }
  if (!hits.length) return null
  hits.sort((a, b) => a.index - b.index)
  const seen = new Set<string>()
  const excerpts: string[] = []
  for (const hit of hits.slice(0, 2)) {
    const excerpt = snippet(text, hit.index, hit.term.length)
    if (seen.has(excerpt)) continue
    seen.add(excerpt)
    excerpts.push(excerpt)
  }
  return { index: hits[0].index, terms: hits.map(hit => hit.term), excerpts }
}

/** 一次最多长这么多个节点（含资料节点）：清单不淹没，动画时长也压得住。 */
const MAX_NODES = 24

/** 把一篇资料文本和一天记录做关键词匹配，生成带生长顺序的图谱数据（纯函数）。 */
export function growGraph(day: Day, text: string, fileName = '拖入资料'): GrowGraph {
  const lower = text.toLowerCase()
  const documentNode: GrowNode = {
    id: 'doc',
    kind: 'document',
    label: trimLabel(fileName, 24),
    detail: `拖入的 ${fileName}，本地读取后与 day 记录匹配`,
    nature: 'synthetic',
    reason: '本次拖入的资料',
    terms: [],
    excerpts: text.trim() ? [snippet(text, 0, Math.min(24, text.length))] : [],
  }

  const nodes: GrowNode[] = [documentNode]
  const matched = new Set<string>(['doc'])
  const position = new Map<string, number>([['doc', -1]])
  /** 超过 MAX_NODES 时先砍派生节点：0=直接命中，1=事件单，2=裁定 */
  const rank = new Map<string, number>([['doc', 0]])

  const add = (node: GrowNode, hit: Hit, nodeRank = 0) => {
    nodes.push(node)
    matched.add(node.id)
    position.set(node.id, hit.index)
    rank.set(node.id, nodeRank)
  }

  const baseNode = (id: string, kind: GrowNodeKind, label: string, detail: string, reason: string, hit: Hit): GrowNode => ({
    id,
    kind,
    label,
    detail,
    nature: 'synthetic',
    reason,
    terms: hit.terms,
    excerpts: hit.excerpts,
  })

  // 1. 实体：按显示名归类（同一楼层的照明、暖通在图上都叫“行政楼”，合成一个节点）。
  const entityIds = new Set<string>()
  for (const event of day.events) if (event.entity_id) entityIds.add(event.entity_id)
  for (const ticket of day.tickets) for (const subscription of ticket.subscriptions ?? []) entityIds.add(subscription.entity_id)
  for (const rule of day.temp_rules) for (const entityId of rule.entity_ids) entityIds.add(entityId)

  const entityGroups = new Map<string, { tails: string[]; terms: Set<string>; ids: string[] }>()
  for (const entityId of entityIds) {
    const info = entityInfo(entityId)
    const group = entityGroups.get(info.label) ?? { tails: [], terms: new Set<string>(), ids: [] }
    group.tails.push(info.tail)
    group.ids.push(entityId)
    for (const term of info.terms) group.terms.add(term)
    entityGroups.set(info.label, group)
  }

  const entityNodeByEntityId = new Map<string, string>()
  for (const [label, group] of entityGroups) {
    const hit = findHit([...group.terms], text, lower)
    if (!hit) continue
    const id = `entity:${label}`
    for (const entityId of group.ids) entityNodeByEntityId.set(entityId, id)
    add(baseNode(id, 'entity', label, `实体 ${group.tails.join(' / ')}`, `原文命中「${hit.terms[0]}」`, hit), hit)
  }
  const entityHit = (entityId: string | null | undefined) => {
    if (!entityId) return null
    const nodeId = entityNodeByEntityId.get(entityId)
    return nodeId ? position.get(nodeId) ?? null : null
  }

  // 2. 事件：类型名 + 摘要关键词；没直接命中时跟着命中的实体长出来。
  const eventNodeByEventId = new Map<string, string>()
  const eventEntityId = new Map<string, string | null>()
  for (const event of day.events) {
    const typeLabel = EVENT_LABEL[event.event_type] ?? event.event_type
    const direct = findHit([typeLabel, ...keywords(event.summary)], text, lower)
    const entityId = event.entity_id ?? null
    const entityAt = entityHit(entityId)
    if (!direct && entityAt == null) continue
    const id = `event:${event.event_id}`
    const hit = direct ?? {
      index: entityAt! + 0.5,
      terms: [entityId ? entityInfo(entityId).label : typeLabel],
      excerpts: [],
    }
    const reason = direct
      ? `原文命中「${direct.terms[0]}」`
      : `关联了已命中的实体「${entityId ? entityInfo(entityId).label : typeLabel}」`
    eventNodeByEventId.set(event.event_id, id)
    eventEntityId.set(event.event_id, entityId)
    add(baseNode(id, 'event', typeLabel, event.summary, reason, hit), hit)
  }

  // 3. 临时规则：规则原文关键词；没直接命中时跟着规则适用的实体长出来。
  const ruleEntityIds = new Map<string, string[]>()
  for (const rule of day.temp_rules) {
    const direct = findHit(keywords(rule.text), text, lower)
    const linked = rule.entity_ids.find(entityId => entityNodeByEntityId.has(entityId))
    if (!direct && !linked) continue
    const id = `rule:${rule.rule_id}`
    const hit = direct ?? {
      index: (entityHit(linked) ?? 0) + 0.5,
      terms: linked ? [entityInfo(linked).label] : [],
      excerpts: [],
    }
    const reason = direct
      ? `原文命中「${direct.terms[0]}」`
      : `适用于已命中的实体「${linked ? entityInfo(linked).label : ''}」`
    ruleEntityIds.set(id, rule.entity_ids)
    add(baseNode(id, 'rule', '临时规则', rule.text, reason, hit), hit)
  }

  // 4. 事件单：摘要关键词；没直接命中时跟着并入的事件长出来。
  const ticketEventIds = new Map<string, string[]>()
  for (const ticket of day.tickets) {
    const direct = findHit(keywords(ticket.summary), text, lower)
    const linkedEvents = ticket.event_ids.filter(eventId => eventNodeByEventId.has(eventId))
    if (!direct && !linkedEvents.length) continue
    const id = `ticket:${ticket.ticket_id}`
    const lead = linkedEvents.length
      ? nodes.find(node => node.id === eventNodeByEventId.get(linkedEvents[0]))
      : undefined
    const hit = direct ?? {
      index: (lead ? position.get(lead.id) ?? 0 : 0) + 0.5,
      terms: lead ? [lead.terms[0] ?? lead.label] : [],
      excerpts: lead?.excerpts ?? [],
    }
    const reason = direct
      ? `原文命中「${direct.terms[0]}」`
      : `并入了已命中事件「${lead?.label ?? '事件'}」`
    ticketEventIds.set(id, ticket.event_ids)
    add(baseNode(id, 'ticket', `事件单 · ${trimLabel(ticket.summary, 10)}`, ticket.summary, reason, hit), hit, 1)
  }

  // 5. 裁定：冲突与结果摘要；没直接命中时跟着对应事件单长出来。
  const decisionTicket = new Map<string, string>()
  for (const decision of day.decisions) {
    const direct = findHit(keywords(`${decision.conflict.summary} ${decision.result.summary}`), text, lower)
    const ticketNodeId = `ticket:${decision.ticket_id}`
    const linked = matched.has(ticketNodeId)
    if (!direct && !linked) continue
    const id = `decision:${decision.decision_id}`
    const hit = direct ?? {
      index: (position.get(ticketNodeId) ?? 0) + 0.5,
      terms: [],
      excerpts: [],
    }
    const reason = direct
      ? `原文命中「${direct.terms[0]}」`
      : '对应的事件单已命中'
    decisionTicket.set(id, decision.ticket_id)
    add(baseNode(id, 'decision', `裁定 · ${outcomeLabel(decision.result.outcome)}`, decision.result.summary, reason, hit), hit, 2)
  }

  // 6. 知识版本：版本的摘要关键词（电价、峰段这类变更说明）。
  for (const version of day.knowledge_versions) {
    const direct = findHit(keywords(version.summary), text, lower)
    if (!direct) continue
    const id = `knowledge:${version.version_manifest_id}`
    add(baseNode(id, 'knowledge', `知识 v${version.version}`, version.summary, `原文命中「${direct.terms[0]}」`, direct), direct)
  }

  // 7. 生长顺序：资料固定第一，其余按原文里首次命中的位置；超量时先砍事件单和裁定。
  const byPosition = [documentNode, ...nodes.slice(1).sort((a, b) => (position.get(a.id) ?? 0) - (position.get(b.id) ?? 0))]
  const ordered = byPosition.length <= MAX_NODES
    ? byPosition
    : byPosition
      .map((node, index) => ({ node, index }))
      .sort((a, b) => (rank.get(a.node.id) ?? 0) - (rank.get(b.node.id) ?? 0) || a.index - b.index)
      .slice(0, MAX_NODES)
      .sort((a, b) => a.index - b.index)
      .map(item => item.node)
  const shownIds = new Set(ordered.map(node => node.id))

  // 8. 边：结构关系优先；孤立的节点头到资料节点，保证图谱连通。
  const edges: GrowEdge[] = []
  const edgeIds = new Set<string>()
  const edge = (source: string, target: string, kind: GrowEdgeKind) => {
    if (!shownIds.has(source) || !shownIds.has(target)) return
    const id = `${source}->${target}:${kind}`
    if (edgeIds.has(id)) return
    edgeIds.add(id)
    edges.push({ id, source, target, kind, label: EDGE_LABEL[kind] })
  }
  for (const node of ordered) {
    if (node.kind === 'entity') { edge('doc', node.id, 'mentions'); continue }
    if (node.kind === 'event') {
      const eventId = node.id.slice('event:'.length)
      const entityId = eventEntityId.get(eventId)
      const entityNodeId = entityId ? entityNodeByEntityId.get(entityId) : undefined
      if (entityNodeId) edge(node.id, entityNodeId, 'occurred_at')
      else edge('doc', node.id, 'mentions')
      continue
    }
    if (node.kind === 'rule') {
      const entityId = (ruleEntityIds.get(node.id) ?? []).find(id => entityNodeByEntityId.has(id))
      const entityNodeId = entityId ? entityNodeByEntityId.get(entityId) : undefined
      if (entityNodeId) edge(node.id, entityNodeId, 'applies_to')
      else edge('doc', node.id, 'mentions')
      continue
    }
    if (node.kind === 'ticket') {
      const ticketId = node.id.slice('ticket:'.length)
      const eventNodeIds = (ticketEventIds.get(ticketId) ?? []).map(id => eventNodeByEventId.get(id)).filter(Boolean) as string[]
      if (eventNodeIds.length) for (const eventNodeId of eventNodeIds.slice(0, 3)) edge(eventNodeId, node.id, 'merged_into')
      else edge('doc', node.id, 'mentions')
      continue
    }
    if (node.kind === 'decision') {
      const ticketId = decisionTicket.get(node.id)
      const ticketNodeId = ticketId ? `ticket:${ticketId}` : ''
      if (ticketNodeId && shownIds.has(ticketNodeId)) edge(node.id, ticketNodeId, 'decided_by')
      else edge('doc', node.id, 'mentions')
      continue
    }
    if (node.kind === 'knowledge') edge('doc', node.id, 'based_on')
  }
  edges.sort((a, b) => Math.max(position.get(a.source) ?? 0, position.get(a.target) ?? 0)
    - Math.max(position.get(b.source) ?? 0, position.get(b.target) ?? 0))

  const stats: Record<GrowNodeKind, number> = { document: 0, entity: 0, event: 0, ticket: 0, rule: 0, decision: 0, knowledge: 0 }
  for (const node of ordered) stats[node.kind] += 1

  return {
    nodes: ordered,
    edges,
    order: ordered.map(node => node.id),
    matches: ordered.length - 1,
    stats,
  }
}
