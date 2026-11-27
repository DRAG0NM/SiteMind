/**
 * 图谱视图：G6 v5 负责画布与逐节点生长动画，匹配数据来自 graph.ts（纯函数）。
 * 画布容器量不到尺寸（jsdom、面板被折叠成 0 宽）或 Canvas 不可用时，退回成清单，不抛错。
 */
import { useEffect, useRef, useState } from 'react'
import type { EdgeData, Graph as G6Graph, NodeData } from '@antv/g6'
import { NatureBadge } from '../common/nature'
import { GROW_KIND_LABEL, visibleEdges, type GrowEdge, type GrowGraph, type GrowNode } from './graph'

interface KnowledgeGraphProps {
  model: GrowGraph
  /** 已经长到第几个节点（含资料节点） */
  reveal: number
  /** 减弱动效时一次显示最终图 */
  animate: boolean
}

/** 颜色全部取自 shared/theme.css：accent 橙、panel/line/text 系列，不另起一套。 */
const KIND_STYLE: Record<GrowNode['kind'], { fill: string; stroke: string; text: string; size: number }> = {
  document: { fill: '#f2913b', stroke: '#ffa654', text: '#0a0d11', size: 46 },
  entity: { fill: '#1b2229', stroke: '#3a4551', text: '#e6eaee', size: 34 },
  event: { fill: '#2a1c10', stroke: '#f2913b', text: '#ffc08a', size: 28 },
  rule: { fill: '#171d24', stroke: '#5e3518', text: '#e6eaee', size: 26 },
  ticket: { fill: '#171d24', stroke: '#2a323c', text: '#aeb8c3', size: 24 },
  decision: { fill: '#171d24', stroke: '#5e3518', text: '#e6eaee', size: 24 },
  knowledge: { fill: '#12171d', stroke: '#2a323c', text: '#96a1ad', size: 22 },
}

function toNode(node: GrowNode): NodeData {
  const tone = KIND_STYLE[node.kind]
  return {
    id: node.id,
    data: { kind: node.kind, label: node.label },
    style: {
      size: tone.size,
      fill: tone.fill,
      stroke: tone.stroke,
      lineWidth: node.kind === 'document' ? 2 : 1,
      // 圆形布局下相邻节点的标签会互相压住：只给中心（资料）节点显示标签，其余节点在右侧列表里看（#248-10）。
      labelText: node.kind === 'document' ? node.label : '',
      labelFill: tone.text,
      labelFontSize: 11,
      labelPlacement: 'bottom',
      labelOffsetY: 4,
      labelMaxWidth: 150,
    },
  }
}

function toEdge(edge: GrowEdge): EdgeData {
  return { id: edge.id, source: edge.source, target: edge.target }
}

type ViewMode = 'pending' | 'g6' | 'list'

export function KnowledgeGraph({ model, reveal, animate }: KnowledgeGraphProps) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const graphRef = useRef<G6Graph | null>(null)
  const revealedRef = useRef(0)
  const edgeIdsRef = useRef(new Set<string>())
  const revealRef = useRef(reveal)
  const [mode, setModeState] = useState<ViewMode>('pending')
  revealRef.current = reveal

  const setMode = (next: ViewMode) => setModeState(next)

  // 尺寸不可用时不动态加载 G6（jsdom 里也不去碰 Canvas）。
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const width = host.clientWidth
    const height = host.clientHeight
    if (width < 80 || height < 80) {
      setMode('list')
      return
    }
    let cancelled = false
    let graph: G6Graph | null = null
    setMode('pending')
    void (async () => {
      try {
        const { Graph } = await import('@antv/g6')
        if (cancelled) return
        const initial = Math.max(1, Math.min(revealRef.current, model.nodes.length))
        const nodes = model.nodes.slice(0, initial).map(toNode)
        const edges = visibleEdges(model, initial).map(toEdge)
        graph = new Graph({
          container: host,
          width,
          height,
          animation: animate,
          data: { nodes, edges },
          node: {
            type: 'circle',
            style: {
              size: 30,
              fill: '#1b2229',
              stroke: '#3a4551',
              lineWidth: 1,
              labelFill: '#e6eaee',
              labelFontSize: 11,
              labelPlacement: 'bottom',
              labelOffsetY: 4,
              labelMaxWidth: 150,
            },
            animation: animate ? { enter: 'fade', update: 'translate' } : false,
          },
          edge: {
            type: 'line',
            style: { stroke: '#2a323c', lineWidth: 1, endArrow: true, endArrowFill: '#3a4551', endArrowSize: 6 },
            animation: animate ? { enter: 'fade' } : false,
          },
          layout: { type: 'circular', radius: Math.max(70, Math.min(width, height) / 2 - 70) },
          behaviors: ['drag-canvas'],
        })
        await graph.render()
        if (cancelled) {
          graph.destroy()
          return
        }
        graphRef.current = graph
        revealedRef.current = initial
        edgeIdsRef.current = new Set(edges.map(edge => String(edge.id)))
        setMode('g6')
      } catch {
        if (!cancelled) setMode('list')
      }
    })()
    return () => {
      cancelled = true
      graph?.destroy()
      graphRef.current = null
      revealedRef.current = 0
      edgeIdsRef.current = new Set()
    }
  }, [model, animate])

  // 每长出一个节点就 addNodeData + draw；边在两端节点都出现后补上，由 G6 播放入场动画。
  useEffect(() => {
    const graph = graphRef.current
    if (!graph || mode !== 'g6') return
    const target = Math.min(reveal, model.nodes.length)
    if (target <= revealedRef.current) return
    const addedNodes = model.nodes.slice(revealedRef.current, target)
    const addedEdges = visibleEdges(model, target).filter(edge => !edgeIdsRef.current.has(edge.id))
    graph.addNodeData(addedNodes.map(toNode))
    if (addedEdges.length) {
      graph.addEdgeData(addedEdges.map(toEdge))
      for (const edge of addedEdges) edgeIdsRef.current.add(edge.id)
    }
    revealedRef.current = target
    // render = 重新布局 + 重绘：新节点按环形布局落位，旧节点用 update 动画平移过去。
    void graph.render().catch(() => setMode('list'))
  }, [model, reveal, mode])

  // 面板尺寸变了就跟着变，避免画布与容器错位。
  useEffect(() => {
    if (mode !== 'g6') return
    const onResize = () => {
      const host = hostRef.current
      const graph = graphRef.current
      if (!host || !graph) return
      const width = host.clientWidth
      const height = host.clientHeight
      if (width < 80 || height < 80) return
      try {
        graph.resize(width, height)
      } catch {
        // 容器已被移除时忽略，下一次挂载会重建。
      }
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [mode])

  const shown = model.nodes.slice(0, reveal)

  return (
    <div className="kd-graph">
      <div ref={hostRef} className="kd-canvas" aria-hidden={mode !== 'g6'} />
      {mode !== 'g6' && (
        <div className="kd-graph-flat" data-testid="kd-graph-fallback">
          <p className="dm kd-flat-note">
            {mode === 'pending'
              ? '正在准备图谱画布…'
              : '图谱画布不可用（容器尺寸为 0 或当前环境不支持 Canvas），已降级为清单。'}
          </p>
          <ul className="kd-flat-list">
            {shown.map(node => (
              <li key={node.id} className="kd-flat-item">
                <span className="chip">{GROW_KIND_LABEL[node.kind]}</span>
                <b>{node.label}</b>
                <NatureBadge nature={node.nature} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
