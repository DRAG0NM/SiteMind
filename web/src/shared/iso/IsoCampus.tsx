import {
  forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState,
  type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent,
} from 'react'
import type { CampusBuilding, CampusEntity, CampusSpot, CampusTone } from '../campus'
import { LoadColorIcon, ResetViewIcon, RoofCutIcon, SectionIcon, ZoomBuildingIcon } from '../icons'
import { BuildingSection, BUILDING_TITLE } from './BuildingSection'
import {
  ANCHORS, buildingBounds, buildScene, inkColor, project, spotBounds,
  type Bounds, type IsoPart, type IsoShape, type PaletteKey, type Pt, type Tone,
} from './geometry'
import { prefersReducedMotion } from '../motion'
import './iso.css'

export interface IsoCampusHandle {
  /** 回到全景 */
  reset: () => void
  /** 推到某个建筑（不给就用选中项所在的建筑，再没有就车间） */
  zoomToBuilding: (building?: CampusBuilding | null) => void
  /** 巡演镜头：推到某个实体 */
  focusSpot: (spot: CampusSpot) => void
  /** 拉回全景，不带选中变化 */
  clearFocus: () => void
  /**
   * 把几处位置一起框进画面（舞台态：事件和它派出的动作同框）。
   * area 是这几处在画布宽度里该占的左右比例，给两侧有浮层的舞台留出中间窗口。
   */
  focusSpots: (spots: CampusSpot[], area?: [number, number]) => void
}

export interface IsoCampusProps {
  entities: CampusEntity[]
  selected?: CampusSpot | null
  onSelect?: (spot: CampusSpot | null) => void
  /** 图形四周留白，展示页与控制台不同 */
  margin?: { l: number; r: number; t: number; b: number }
  toolbarStyle?: CSSProperties
  legend?: boolean
  ariaLabel?: string
  /** 剖面图里的轿厢楼层，来自回放数据；缺数据时停在一层 */
  carFloor?: number | null
  className?: string
  /**
   * 状态变化时的动效。calm（控制台）：事件处扩散一圈、动作到达处闪一下；
   * show（展示页）：再加从事件画到动作目标的派发线，目标设备等线到了才转蓝。
   */
  motion?: 'calm' | 'show'
  /** 压暗与事件、动作无关的建筑和设备 */
  spotlight?: boolean
  /** 首次挂载时把当前已有的事件和动作当作刚发生，重放一遍（舞台态用） */
  replayOnMount?: boolean
}

/** 一次性动效：事件扩散、派发线、动作到达。start 是 performance.now() 时刻。 */
interface Cue { id: number; kind: 'event' | 'dispatch' | 'arrive'; spot: CampusSpot; from?: CampusSpot; start: number }
const CUE_LIFE = 1900
const LINE_MS = 720

const VB_EPS = 0.001
const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value))
const useReducedMotion = () => {
  // 首帧就读系统设置，免得第一批动效在设置生效前就排上了
  const [reduced, setReduced] = useState(prefersReducedMotion)
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    setReduced(prefersReducedMotion())
    const onChange = () => setReduced(prefersReducedMotion())
    query.addEventListener?.('change', onChange)
    return () => query.removeEventListener?.('change', onChange)
  }, [])
  return reduced
}

const shapePointString = (points: Pt[]) => points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')

export const IsoCampus = forwardRef<IsoCampusHandle, IsoCampusProps>(function IsoCampus(props, ref) {
  const {
    entities: rawEntities, selected = null, onSelect, margin, toolbarStyle, legend = true, carFloor, className,
    motion = 'calm', spotlight = false, replayOnMount = false,
  } = props
  const reducedMotion = useReducedMotion()

  // —— 状态变化 → 一次性动效。只在这里比对色调，计时由下面的 clock 统一推进（StrictMode 下也只触发一次）。
  const [cues, setCues] = useState<Cue[]>([])
  const [clock, setClock] = useState(() => performance.now())
  const prevTonesRef = useRef<Map<CampusSpot, CampusTone> | null>(replayOnMount ? new Map() : null)
  const cueIdRef = useRef(0)
  useEffect(() => {
    const prev = prevTonesRef.current
    prevTonesRef.current = new Map(rawEntities.map(entity => [entity.spot, entity.tone]))
    if (!prev || reducedMotion) return
    const now = performance.now()
    const placed = (entity: CampusEntity) => Boolean(ANCHORS[entity.spot])
    const events = rawEntities.filter(entity => entity.tone === 'event' && placed(entity) && prev.get(entity.spot) !== 'event')
    const actions = rawEntities.filter(entity => entity.tone === 'action' && placed(entity) && prev.get(entity.spot) !== 'action')
    if (!events.length && !actions.length) return
    // 派发线从出事的设备出发：优先刚出的事件、有独立形体的设备（T1 而不是整栋车间）。
    const byDevice = (list: CampusEntity[]) => list.find(entity => entity.paint === 'fill') ?? list[0]
    const source = (byDevice(events) ?? byDevice(rawEntities.filter(entity => entity.tone === 'event' && placed(entity))))?.spot
    const lead = replayOnMount && prev.size === 0 ? 260 : 0
    const fresh: Cue[] = events.map(entity => ({ id: ++cueIdRef.current, kind: 'event', spot: entity.spot, start: now + lead }))
    const actLead = lead + (events.length ? 760 : 0)
    actions.forEach((entity, index) => {
      const start = now + actLead + index * 240
      const line = motion === 'show' && source && source !== entity.spot
      if (line) fresh.push({ id: ++cueIdRef.current, kind: 'dispatch', spot: entity.spot, from: source, start })
      fresh.push({ id: ++cueIdRef.current, kind: 'arrive', spot: entity.spot, start: start + (line ? LINE_MS : 0) })
    })
    setCues(current => [...current, ...fresh])
    setClock(now)
  }, [rawEntities, motion, replayOnMount, reducedMotion])

  // 推进到下一个动效起点或终点。
  useEffect(() => {
    if (!cues.length) return
    const now = performance.now()
    const marks = cues.flatMap(cue => [cue.start, cue.start + CUE_LIFE]).filter(mark => mark > now)
    const next = marks.length ? Math.min(...marks) : now
    const id = window.setTimeout(() => {
      const at = performance.now()
      setClock(at)
      setCues(current => {
        const alive = current.filter(cue => cue.start + CUE_LIFE > at)
        return alive.length === current.length ? current : alive
      })
    }, Math.max(0, next - now) + 16)
    return () => window.clearTimeout(id)
  }, [cues, clock])

  const liveCues = cues.filter(cue => cue.start <= clock && cue.start + CUE_LIFE > clock)
  const arriving = new Set(liveCues.filter(cue => cue.kind === 'arrive').map(cue => cue.spot))
  // 展示页：派发线还没到的目标先保持原色，到了才转蓝。
  const held = useMemo(() => new Set(
    motion === 'show' ? cues.filter(cue => cue.kind === 'arrive' && cue.start > clock).map(cue => cue.spot) : [],
  ), [cues, clock, motion])
  const entities = useMemo(
    () => (held.size ? rawEntities.map(entity => (held.has(entity.spot) ? { ...entity, tone: 'normal' as const } : entity)) : rawEntities),
    [rawEntities, held],
  )
  const [roofCut, setRoofCut] = useState(true)
  const [loadColor, setLoadColor] = useState(false)
  const [section, setSection] = useState<CampusBuilding | null>(null)
  const svgRef = useRef<SVGSVGElement>(null)
  const dragRef = useRef<{ x: number; y: number; viewBox: Bounds; moved: boolean } | null>(null)
  const movedRef = useRef(false)

  const loads = useMemo(() => {
    const map: Partial<Record<CampusSpot, number | null>> = {}
    for (const entity of entities) map[entity.spot] = entity.load
    return map
  }, [entities])

  const scene = useMemo(() => buildScene({ roofCut, margin }), [roofCut, margin])
  const base = scene.viewBox
  const [viewBox, setViewBox] = useState<Bounds>(base)
  const viewBoxRef = useRef<Bounds>(base)
  const rafRef = useRef<number | null>(null)
  const baseRef = useRef<Bounds>(base)
  baseRef.current = base
  useEffect(() => { viewBoxRef.current = viewBox }, [viewBox])

  const bounds = useMemo(() => ({ buildings: buildingBounds(scene.parts), spots: spotBounds(scene.parts) }), [scene])
  const entityOf = useCallback((spot: CampusSpot | null) => entities.find(entity => entity.spot === spot), [entities])
  const selectionBounds: Bounds | undefined = selected ? bounds.spots[selected] : undefined
  const selectedBuilding = selected ? entityOf(selected)?.building ?? null : null

  const setBox = useCallback((next: Bounds) => { setViewBox(next) }, [])

  const animateTo = useCallback((target: Bounds) => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    const from = viewBoxRef.current
    const same = Math.abs(from.w - target.w) < VB_EPS && Math.abs(from.x - target.x) < VB_EPS && Math.abs(from.y - target.y) < VB_EPS
    if (reducedMotion || same) { setBox(target); return }
    const started = performance.now()
    // 平移和推拉都走过渡；展示页镜头更慢、更有分量。
    const duration = motion === 'show' ? 760 : 450
    const step = (now: number) => {
      const t = clamp((now - started) / duration, 0, 1)
      const ease = motion === 'show' ? (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2) : 1 - Math.pow(1 - t, 3)
      setBox({
        x: from.x + (target.x - from.x) * ease,
        y: from.y + (target.y - from.y) * ease,
        w: from.w + (target.w - from.w) * ease,
        h: from.h + (target.h - from.h) * ease,
      })
      if (t < 1) rafRef.current = requestAnimationFrame(step)
      else rafRef.current = null
    }
    rafRef.current = requestAnimationFrame(step)
  }, [reducedMotion, setBox, motion])

  useEffect(() => () => { if (rafRef.current !== null) cancelAnimationFrame(rafRef.current) }, [])

  /** 把包围盒加上留白，并保持与全图一致的宽高比。 */
  const framed = useCallback((box: Bounds | undefined, pad = 0.22): Bounds => {
    const view = baseRef.current
    if (!box) return view
    const cx = box.x + box.w / 2
    const cy = box.y + box.h / 2
    const scale = view.h / view.w
    let w = Math.max(box.w * (1 + pad), view.w * 0.3)
    let h = w * scale
    if (h < box.h * (1 + pad)) { h = box.h * (1 + pad); w = h / scale }
    return { x: cx - w / 2, y: cy - h / 2, w, h }
  }, [])

  const reset = useCallback(() => animateTo(baseRef.current), [animateTo])
  const zoomToBuilding = useCallback((building?: CampusBuilding | null) => {
    const target = building ?? selectedBuilding ?? 'factory'
    animateTo(framed(bounds.buildings[target]))
  }, [animateTo, bounds, framed, selectedBuilding])
  const focusSpot = useCallback((spot: CampusSpot) => {
    animateTo(framed(bounds.spots[spot], 0.35))
  }, [animateTo, bounds, framed])
  const clearFocus = useCallback(() => animateTo(baseRef.current), [animateTo])
  const focusSpots = useCallback((spots: CampusSpot[], area: [number, number] = [0, 1]) => {
    const boxes = spots.map(spot => bounds.spots[spot]).filter((box): box is Bounds => Boolean(box))
    if (!boxes.length) { animateTo(baseRef.current); return }
    const x = Math.min(...boxes.map(box => box.x))
    const y = Math.min(...boxes.map(box => box.y))
    const w = Math.max(...boxes.map(box => box.x + box.w)) - x
    const h = Math.max(...boxes.map(box => box.y + box.h)) - y
    const view = baseRef.current
    const scale = view.h / view.w
    const [left, right] = area
    // area 反向或越界时退回整幅，避免算出负宽度。
    const [l, r] = left >= 0 && right <= 1 && left < right ? [left, right] : [0, 1]
    // 镜头最多放大到全图的 1.6 倍，再大就只剩局部色块、看不出是哪栋楼。
    const maxWidth = view.w * 1.6
    let width = Math.min((w * 1.04) / (r - l), maxWidth)
    if (width * scale < h * 1.3) width = Math.min((h * 1.3) / scale, maxWidth)
    const height = width * scale
    animateTo({ x: x + w / 2 - width * (l + r) / 2, y: y + h / 2 - height / 2, w: width, h: height })
  }, [animateTo, bounds])

  useImperativeHandle(ref, () => ({ reset, zoomToBuilding, focusSpot, clearFocus, focusSpots }),
    [reset, zoomToBuilding, focusSpot, clearFocus, focusSpots])

  /** 客户端坐标 → viewBox 坐标（preserveAspectRatio=xMidYMid meet）。 */
  const toSvg = (clientX: number, clientY: number, view: Bounds) => {
    const svg = svgRef.current
    if (!svg) return { x: view.x + view.w / 2, y: view.y + view.h / 2 }
    const rect = svg.getBoundingClientRect()
    if (!rect.width || !rect.height) return { x: view.x + view.w / 2, y: view.y + view.h / 2 }
    const k = Math.min(rect.width / view.w, rect.height / view.h)
    const offsetX = rect.left + (rect.width - view.w * k) / 2
    const offsetY = rect.top + (rect.height - view.h * k) / 2
    return { x: view.x + (clientX - offsetX) / k, y: view.y + (clientY - offsetY) / k }
  }

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const view = viewBoxRef.current
      const at = toSvg(event.clientX, event.clientY, view)
      const factor = Math.pow(1.0015, event.deltaY)
      const baseView = baseRef.current
      const nextWidth = clamp(view.w * factor, baseView.w * 0.28, baseView.w * 1.6)
      const nextHeight = nextWidth * (view.h / view.w)
      const ratioX = (at.x - view.x) / view.w
      const ratioY = (at.y - view.y) / view.h
      setBox({
        x: at.x - nextWidth * ratioX,
        y: at.y - nextHeight * ratioY,
        w: nextWidth,
        h: nextHeight,
      })
    }
    svg.addEventListener('wheel', onWheel, { passive: false })
    return () => svg.removeEventListener('wheel', onWheel)
    // toSvg 只读 ref 与当前 viewBox，用 ref 保持最新
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setBox])

  const onPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (event.button !== 0) return
    dragRef.current = { x: event.clientX, y: event.clientY, viewBox: viewBoxRef.current, moved: false }
    movedRef.current = false
    event.currentTarget.setPointerCapture?.(event.pointerId)
    event.currentTarget.classList.add('panning')
  }
  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current
    const svg = svgRef.current
    if (!drag || !svg) return
    const dx = event.clientX - drag.x
    const dy = event.clientY - drag.y
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return
    drag.moved = true
    movedRef.current = true
    const rect = svg.getBoundingClientRect()
    if (!rect.width || !rect.height) return
    const k = Math.min(rect.width / drag.viewBox.w, rect.height / drag.viewBox.h)
    setBox({
      ...drag.viewBox,
      x: drag.viewBox.x - dx / k,
      y: drag.viewBox.y - dy / k,
    })
  }
  const onPointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    dragRef.current = null
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    event.currentTarget.classList.remove('panning')
    // 拖拽结束时落下的 click 由各构件里的 movedRef 判断
    window.setTimeout(() => { movedRef.current = false }, 0)
  }

  const select = (spot: CampusSpot | null) => {
    onSelect?.(spot)
    if (spot) focusSpot(spot)
    else clearFocus()
  }
  const onPartKey = (event: ReactKeyboardEvent<SVGGElement>, spot: CampusSpot | null) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    select(spot)
  }

  const toneOf = (part: IsoPart): Tone => {
    if (!part.spot) return 'normal'
    const entity = entityOf(part.spot)
    const tone = (entity?.tone ?? 'normal') as CampusTone
    if (tone === 'normal') return 'normal'
    // 有独立形体的设备整块上色；建筑本体只给外轮廓一圈实线，内部产线、机组、空调箱保持灰。
    if (entity?.paint !== 'outline') return tone
    if (!part.edge) return 'normal'
    return tone === 'event' ? 'event-outline' : tone
  }
  const loadOf = (part: IsoPart): number | null => (part.spot ? loads[part.spot] ?? null : null)

  const renderShape = (part: IsoPart, shape: IsoShape, index: number, palette: PaletteKey, tone: Tone, load: number | null) => {
    const key = `${part.key}-${shape.key}-${index}`
    // 虚线只属于 Agent 动作；事件橙色一律实线。
    const dashed = tone === 'action'
    const baseWidth = shape.el === 'ellipse' ? 0.8 : shape.sw
    const outlineWidth = tone === 'event-outline' ? Math.max(baseWidth * 2.4, 1.6) : baseWidth
    if (shape.el === 'polygon') {
      const fill = loadColor && load != null ? 'var(--accent)' : inkColor(shape.fill, palette, tone)
      return <polygon key={key} points={shapePointString(shape.points)}
        fill={fill} fillOpacity={loadColor && load != null ? 0.15 + 0.7 * load : undefined}
        stroke={inkColor(shape.stroke, palette, tone)} strokeWidth={shape.sw}
        strokeDasharray={dashed && shape.stroke === 's' ? '4 3' : undefined}
        strokeLinejoin="round" className={shape.cls} />
    }
    if (shape.el === 'polyline') {
      const ink = inkColor(shape.stroke, palette, tone)
      return <polyline key={key} points={shapePointString(shape.points)} fill="none" stroke={ink} strokeWidth={outlineWidth}
        strokeDasharray={tone === 'event-outline' ? undefined : (shape.dash ?? (dashed && shape.stroke === 'line' ? '4 3' : undefined))}
        strokeLinejoin="round" strokeLinecap="round" className={shape.cls} opacity={shape.opacity} />
    }
    if (shape.el === 'path') {
      return <path key={key} d={shape.d} fill={inkColor(shape.fill, palette, tone)} stroke={inkColor(shape.stroke, palette, tone)}
        strokeWidth={shape.sw} className={shape.cls} />
    }
    return <ellipse key={key} cx={shape.cx} cy={shape.cy} rx={shape.rx} ry={shape.ry}
      fill={inkColor(shape.fill, palette, tone)} stroke={inkColor('s', palette, tone)} strokeWidth={0.8} />
  }

  return (
    <div className={'iso-campus' + (spotlight ? ' is-spotlight' : '') + (className ? ' ' + className : '')}>
      <svg
        ref={svgRef}
        className="iso-canvas"
        width="100%"
        height="100%"
        viewBox={`${viewBox.x.toFixed(1)} ${viewBox.y.toFixed(1)} ${viewBox.w.toFixed(1)} ${viewBox.h.toFixed(1)}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={props.ariaLabel ?? '厂区轴测图：生产车间、行政楼与电梯井、能源站、空压站、冷却塔和光伏车棚'}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onClick={() => { if (!movedRef.current) onSelect?.(null) }}
      >
        <g className="iso-ground">{scene.ground.map((shape, index) => renderShape(
          { key: 'ground', spot: null, building: null, layer: 'ground', depth: 0, palette: 'n', shapes: [] },
          shape, index, 'n', 'normal', null))}</g>
        {scene.parts.map(part => {
          const tone = toneOf(part)
          const load = loadOf(part)
          const isSelected = part.spot != null && part.spot === selected
          return (
            <g
              key={part.key}
              className={[
                'iso-part',
                // 建筑外轮廓只描实线，不再加脉冲光晕；光晕留给有独立形体的设备。
                tone === 'event' ? 'is-event' : '',
                tone === 'action' ? 'is-action' : '',
                tone !== 'normal' ? 'is-lit' : '',
                part.spot && arriving.has(part.spot) ? 'is-arrive' : '',
                isSelected ? 'is-selected' : '',
              ].filter(Boolean).join(' ')}
              data-spot={part.spot ?? undefined}
              data-hit={part.spot ? 'true' : undefined}
              role={part.spot ? 'button' : undefined}
              tabIndex={part.spot ? 0 : undefined}
              aria-label={part.spot ? `厂区设备：${entityOf(part.spot)?.label ?? part.spot}` : undefined}
              aria-pressed={part.spot ? isSelected : undefined}
              onClick={event => {
                if (!part.spot) return
                event.stopPropagation()
                if (!movedRef.current) select(part.spot)
              }}
              onKeyDown={event => onPartKey(event, part.spot)}
            >
              {part.shapes.map((shape, index) => renderShape(part, shape, index, part.palette, tone, load))}
            </g>
          )
        })}
        {selected && selectionBounds && (
          <rect
            x={selectionBounds.x - 10} y={selectionBounds.y - 10}
            width={selectionBounds.w + 20} height={selectionBounds.h + 20}
            fill="none" stroke="var(--text)" strokeWidth="1.5" strokeDasharray="6 4" rx="4"
            pointerEvents="none"
          />
        )}
        {entities.map(entity => {
          const anchor = ANCHORS[entity.spot]
          if (!anchor) return null
          return (
            <Annotation
              // 色调变了就重新挂载，让标注牌弹一下
              key={`${entity.spot}-${entity.tone}`}
              entity={entity}
              anchor={anchor.at}
              dx={anchor.dx}
              dy={anchor.dy}
              selected={selected === entity.spot}
              onSelect={() => select(entity.spot)}
            />
          )
        })}
        <g className="iso-cues" pointerEvents="none">
          {liveCues.map(cue => <CueMark key={cue.id} cue={cue} />)}
        </g>
      </svg>

      <div className="iso-toolbar" style={toolbarStyle}>
        <button type="button" className="ic" data-tip="回到全景" aria-label="回到全景" onClick={reset}><ResetViewIcon /></button>
        <button type="button" className="ic" data-tip="缩放到建筑" aria-label="缩放到建筑" onClick={() => zoomToBuilding()}>
          <ZoomBuildingIcon />
        </button>
        <button type="button" className={'ic' + (loadColor ? ' on' : '')} data-tip="负载着色" aria-label="负载着色"
          aria-pressed={loadColor} onClick={() => setLoadColor(value => !value)}><LoadColorIcon /></button>
        <button type="button" className={'ic' + (roofCut ? ' on' : '')} data-tip="剖开屋面" aria-label="剖开屋面"
          aria-pressed={roofCut} onClick={() => setRoofCut(value => !value)}><RoofCutIcon /></button>
        <button type="button" className="ic" data-tip="看建筑剖面" aria-label="看建筑剖面"
          onClick={() => setSection(selectedBuilding ?? 'factory')}><SectionIcon /></button>
      </div>

      {legend && (
        <div className="iso-legend" aria-hidden="true">
          <span><i />正常</span>
          <span><i className="event" />事件</span>
          <span><i className="am" />Agent 动作</span>
        </div>
      )}

      {section && (
        <BuildingSection
          building={section}
          entities={entities}
          title={BUILDING_TITLE[section]}
          carFloor={carFloor}
          onClose={() => setSection(null)}
        />
      )}
    </div>
  )
})

/** 标注牌：标题与数值来自数据；事件用实心橙面加计数，Agent 动作用橙色虚线。 */
function Annotation({ entity, anchor, dx, dy, selected, onSelect }: {
  entity: CampusEntity
  anchor: [number, number, number]
  dx: number
  dy: number
  selected: boolean
  onSelect: () => void
}) {
  const [ax, ay] = project(anchor[0], anchor[1], anchor[2])
  const fs = 13
  const title = entity.label
  const value = entity.value.length > 16 ? `${entity.value.slice(0, 15)}…` : entity.value
  const textWidth = (text: string) => [...text].reduce((sum, char) => sum + (char.charCodeAt(0) > 255 ? 1 : 0.56), 0) * fs
  let w = Math.max(textWidth(title), textWidth(value)) + 20
  if (entity.tone === 'event') w += 20
  const h = fs * 2 + 16
  const lx = dx < 0 ? ax + dx - w : ax + dx
  const ly = ay + dy - h / 2
  const ex = dx < 0 ? lx + w : lx
  const ey = ly + h / 2
  const color = entity.tone === 'event' ? 'var(--accent)' : entity.tone === 'action' ? 'var(--agent)' : 'var(--text-3)'
  const border = entity.tone === 'event' ? 'var(--accent)' : entity.tone === 'action' ? 'var(--agent)' : '#3A4551'
  return (
    <g
      className={'iso-annotation' + (entity.tone !== 'normal' ? ' is-lit' : '')}
      role="button"
      tabIndex={0}
      aria-label={`${title}：${entity.value}`}
      aria-pressed={selected}
      onClick={event => { event.stopPropagation(); onSelect() }}
      onKeyDown={event => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        event.preventDefault()
        onSelect()
      }}
    >
      {entity.title && <title>{entity.title}</title>}
      <line x1={ax} y1={ay} x2={ex} y2={ey} stroke="#6B7A87" strokeWidth="1" pointerEvents="none" />
      <circle cx={ax} cy={ay} r="3" fill="#12171D" stroke={color} strokeWidth="1.2" pointerEvents="none" />
      <rect x={lx} y={ly} width={w} height={h} fill="#12171D" stroke={border} rx="2"
        strokeDasharray={entity.tone === 'action' ? '5 3' : undefined} strokeWidth={entity.tone === 'action' ? 1.5 : 1} />
      {entity.tone === 'event' && (
        <>
          <rect x={lx + w - 22} y={ly + 6} width="16" height="16" fill="var(--accent)" />
          <text x={lx + w - 14} y={ly + 18.5} fontSize="12" fontWeight={700} textAnchor="middle" fill="#0A0D11">
            {Math.max(1, entity.alerts)}
          </text>
        </>
      )}
      <text x={lx + 9} y={ly + fs + 5} fontSize={fs} fontWeight={700} fill="var(--text)">{title}</text>
      <text x={lx + 9} y={ly + fs * 2 + 9} fontSize={fs} fill={entity.tone === 'event' ? 'var(--accent)' : entity.tone === 'action' ? 'var(--agent)' : 'var(--text-2)'}>{value}</text>
    </g>
  )
}


const anchorPoint = (spot: CampusSpot) => {
  const anchor = ANCHORS[spot]!.at
  return project(anchor[0], anchor[1], anchor[2])
}

/** 一次性动效图形：橙色扩散两圈 / 蓝色派发线（拖尾 + 光点）/ 蓝色到达一圈。 */
function CueMark({ cue }: { cue: Cue }) {
  const [x, y] = anchorPoint(cue.spot)
  if (cue.kind === 'dispatch' && cue.from) {
    const [fx, fy] = anchorPoint(cue.from)
    const lift = Math.min(140, Math.hypot(x - fx, y - fy) * 0.38)
    const d = `M${fx.toFixed(1)},${fy.toFixed(1)} Q${((fx + x) / 2).toFixed(1)},${(Math.min(fy, y) - lift).toFixed(1)} ${x.toFixed(1)},${y.toFixed(1)}`
    return (
      <g className="iso-dispatch">
        <path d={d} pathLength={1} className="iso-dispatch-trail" />
        <path d={d} pathLength={1} className="iso-dispatch-comet" />
      </g>
    )
  }
  const event = cue.kind === 'event'
  return (
    <g transform={`translate(${x.toFixed(1)} ${y.toFixed(1)})`} className={'iso-ripple ' + (event ? 'is-event' : 'is-arrive')}>
      <circle r={event ? 46 : 30} />
      <circle r={event ? 46 : 30} className="late" />
    </g>
  )
}
