/**
 * 轴测园区几何：从 .design/v2/iso.js 的 scene 移植，投影公式、建筑坐标、画家排序、o.js 的配色原样保留。
 * 这里只产出数据（多边形、线、路径），渲染成 React SVG 元素由 IsoCampus 做，不拼字符串、不用 innerHTML。
 */
import type { CampusBuilding, CampusSpot } from '../campus'

export const C = 0.866
export const S = 12

export type Pt = [number, number]

/** 轴测投影：x 向右前、y 向左前、z 向上。 */
export const project = (x: number, y: number, z = 0): Pt => [(x - y) * C * S, (x + y) * 0.5 * S - z * S]

/** 调色板沿用 o.js 的 st.tones。 */
export const PALETTE = {
  n: { t: '#3A4551', l: '#1E252D', r: '#28313B', k: '#56636F' },
  inner: { t: '#161C23', l: '#11161C', r: '#141A20', k: '#2A323C' },
  eq: { t: '#465260', l: '#232B34', r: '#2E3843', k: '#56636F' },
  eq2: { t: '#56636F', l: '#2A333D', r: '#36414C', k: '#56636F' },
  alarm: { t: '#F2913B', l: '#A85A17', r: '#CC7426', k: '#6B3A10' },
  pv: { t: '#243446', l: '#161D25', r: '#1B242E', k: '#45617D' },
} as const

export const INK = {
  line: '#4A5663',
  env: '#56636F',
  ground: '#10151B',
  groundLine: '#2A323C',
  grid: '#161D25',
  road: '#171E26',
  shadow: '#000000',
  'pipe.power': '#6B7A87',
  'pipe.act': '#5CB3F2',
  'pipe.air': '#96A1AD',
  'pipe.water': '#5C7088',
  accent: '#F2913B',
  agent: '#5CB3F2',
  agentPale: '#93CBF8',
  accentPale: '#FFC08A',
  none: 'none',
} as const

export type PaletteKey = keyof typeof PALETTE
export type InkKey = keyof typeof INK
/** 由所在构件的色调决定的墨色；其余是固定墨色。 */
export type FaceInk = 't' | 'l' | 'r' | 'k' | 's' | 'line'
export type Ink = FaceInk | Exclude<InkKey, 'line'>

export type IsoShape =
  | { el: 'polygon'; key: string; points: Pt[]; fill: Ink; stroke: Ink; sw: number; cls?: string; opacity?: number }
  | { el: 'polyline'; key: string; points: Pt[]; stroke: Ink; sw: number; dash?: string; cls?: string; opacity?: number }
  | { el: 'path'; key: string; d: string; fill: Ink; stroke: Ink; sw: number; cls?: string }
  | { el: 'ellipse'; key: string; cx: number; cy: number; rx: number; ry: number; fill: Ink; cls?: string }

export type SceneLayer = 'ground' | 'factory' | 'item'

export interface IsoPart {
  key: string
  /** 对应哪个可着色/可点击的位置；null 表示只画不选 */
  spot: CampusSpot | null
  building: CampusBuilding | null
  layer: SceneLayer
  depth: number
  palette: PaletteKey
  /** 建筑本体：建筑级事件/动作只落在这一圈外轮廓上 */
  edge?: boolean
  shapes: IsoShape[]
}

export interface IsoScene {
  ground: IsoShape[]
  parts: IsoPart[]
  viewBox: { x: number; y: number; w: number; h: number }
}

export type Tone = 'normal' | 'event' | 'action' | 'event-outline'

export interface SceneOptions {
  /** 剖开屋面：关掉时车间画成完整厂房 */
  roofCut: boolean
  /** 各位置的状态色 */
  tones?: Partial<Record<CampusSpot, Tone>>
  /** 各位置的负载（负载着色用） */
  loads?: Partial<Record<CampusSpot, number | null>>
  margin?: { l: number; r: number; t: number; b: number }
}

const SW = 0.8
const PW = 1.5

/** 构件里一种墨色的实际颜色：色调覆盖 + 固定墨色。 */
export function inkColor(ink: Ink, palette: PaletteKey, tone: Tone): string {
  if (tone === 'event' && (ink === 't' || ink === 'l' || ink === 'r' || ink === 's' || ink === 'k')) {
    if (ink === 'k') return PALETTE.alarm.k
    return PALETTE.alarm[ink as 't' | 'l' | 'r']
  }
  if (ink === 'line') return tone === 'action' ? INK.agent : tone === 'event-outline' ? INK.accent : INK.line
  if (ink === 's') return tone === 'action' ? INK.agent : tone === 'event-outline' ? INK.accent : INK.line
  if (ink === 'env') return tone === 'action' ? INK.agent : tone === 'event-outline' ? INK.accent : INK.env
  if (ink === 't' || ink === 'l' || ink === 'r' || ink === 'k') return PALETTE[palette][ink]
  return INK[ink]
}

const points = (raw: [number, number, number][]): Pt[] => raw.map(([x, y, z]) => project(x, y, z))

function box(rect: [number, number, number, number, number, number], palette: PaletteKey, opts: {
  lines?: number; mx?: number; my?: number; gx?: number; nosh?: boolean; shadow?: IsoShape[]
} = {}): { depth: number; shapes: IsoShape[] } {
  const [x0, y0, z0, x1, y1, z1] = rect
  const shapes: IsoShape[] = []
  if (z0 === 0 && !opts.nosh && opts.shadow) {
    const h = Math.min(z1, 9), dx = h * 0.5, dy = h * 0.2
    opts.shadow.push({
      el: 'polygon', key: 'sh',
      points: points([[x0, y1, 0], [x0 + dx, y1 + dy, 0], [x1 + dx, y1 + dy, 0], [x1 + dx, y0 + dy, 0], [x1, y0, 0]]),
      fill: 'shadow', stroke: 'none', sw: 0,
    })
  }
  shapes.push({ el: 'polygon', key: 'l', points: points([[x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]]), fill: 'l', stroke: 's', sw: SW })
  shapes.push({ el: 'polygon', key: 'r', points: points([[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]]), fill: 'r', stroke: 's', sw: SW })
  shapes.push({ el: 'polygon', key: 't', points: points([[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]), fill: 't', stroke: 's', sw: SW })
  for (let i = 1; i <= (opts.lines ?? 0); i++) {
    const z = z0 + (z1 - z0) * i / ((opts.lines ?? 0) + 1)
    shapes.push({ el: 'polyline', key: `ln${i}`, points: points([[x0, y1, z], [x1, y1, z], [x1, y0, z]]), stroke: 'k', sw: SW * 0.7 })
  }
  for (let i = 1; i <= (opts.mx ?? 0); i++) {
    const x = x0 + (x1 - x0) * i / ((opts.mx ?? 0) + 1)
    shapes.push({ el: 'polyline', key: `mx${i}`, points: points([[x, y1, z0], [x, y1, z1]]), stroke: 'k', sw: SW * 0.5 })
  }
  for (let i = 1; i <= (opts.my ?? 0); i++) {
    const y = y0 + (y1 - y0) * i / ((opts.my ?? 0) + 1)
    shapes.push({ el: 'polyline', key: `my${i}`, points: points([[x1, y, z0], [x1, y, z1]]), stroke: 'k', sw: SW * 0.5 })
  }
  for (let i = 1; i <= (opts.gx ?? 0); i++) {
    const x = x0 + (x1 - x0) * i / ((opts.gx ?? 0) + 1)
    shapes.push({ el: 'polyline', key: `gx${i}`, points: points([[x, y0, z1], [x, y1, z1]]), stroke: 'k', sw: SW * 0.5 })
  }
  return { depth: (x0 + x1 + y0 + y1) / 2 + z0 * 0.01, shapes }
}

function cyl(cx: number, cy: number, z0: number, rad: number, h: number, palette: PaletteKey): { depth: number; shapes: IsoShape[] } {
  const [X, Y0] = project(cx, cy, z0)
  const Y1 = Y0 - h * S, rx = rad * 1.2247 * S, ry = rad * 0.7071 * S
  return {
    depth: cx + cy + z0 * 0.01,
    shapes: [
      {
        el: 'path', key: 'body',
        d: `M${X - rx} ${Y1}V${Y0}A${rx} ${ry} 0 0 0 ${X + rx} ${Y0}V${Y1}Z`,
        fill: 'l', stroke: 's', sw: SW,
      },
      { el: 'ellipse', key: 'cap', cx: X, cy: Y1, rx, ry, fill: 't' },
    ],
  }
}

const poly = (raw: [number, number, number][], fill: Ink, stroke: Ink = 's', sw = SW, cls?: string): IsoShape =>
  ({ el: 'polygon', key: 'p', points: points(raw), fill, stroke, sw, cls })

const pl = (raw: [number, number, number][], stroke: Ink, sw: number, dash?: string, cls?: string): IsoShape =>
  ({ el: 'polyline', key: 'l', points: points(raw), stroke, sw, dash, cls })

const ENV_DASH = '3 4'
const ACT_DASH = '7 5'

/** 生产车间：剖开时露出二层夹层空调机组和一层设备；不剖开时画成完整厂房。 */
function factory(roofCut: boolean, shadow: IsoShape[]): IsoPart[] {
  const [x0, y0, x1, y1, h] = [4, 6, 24, 22, 8]
  const key = (name: string) => `factory-${name}`

  if (!roofCut) {
    const shell = box([x0, y0, 0, x1, y1, h], 'n', { lines: 2, mx: 5, gx: 5, my: 3, shadow })
    return [{
      key: key('shell'), spot: 'factory', building: 'factory', layer: 'factory', depth: 28, palette: 'n', edge: true,
      shapes: [
        ...shell.shapes,
        pl([[x0, y0, h], [x1, y0, h], [x1, y1, h], [x0, y1, h], [x0, y0, h]], 'env', SW, ENV_DASH),
      ],
    }]
  }

  const shapes: IsoShape[] = []
  shapes.push(poly([[x0, y0, 0], [x1, y0, 0], [x1, y1, 0], [x0, y1, 0]], 't'))
  shapes.push(poly([[x0, y0, 0], [x1, y0, 0], [x1, y0, h], [x0, y0, h]], 'l'))
  shapes.push(poly([[x0, y0, 0], [x0, y1, 0], [x0, y1, h], [x0, y0, h]], 'r'))
  shapes.push(pl([[x0, y1, 4], [x0, y0, 4], [x1, y0, 4]], 'k', SW * 0.7))
  for (let i = 1; i < 5; i++) shapes.push(pl([[x0 + i * 4, y0, 4.4], [x0 + i * 4, y0, h]], 'k', SW * 0.5))
  for (let i = 1; i < 4; i++) shapes.push(pl([[x0, y0 + i * 4, 0], [x0, y0 + i * 4, h]], 'k', SW * 0.5))

  const inner: IsoPart[] = []
  const nosh = { nosh: true }
  const mezz = box([x0, y0, 4, x1, y0 + 6, 4.35], 'n', nosh)
  shapes.push(...mezz.shapes)
  const ahu = box([5.5, 7.4, 4.35, 9.5, 10.6, 6.3], 'eq2', { ...nosh, lines: 1 })
  const eqA = box([11.5, 7.4, 4.35, 15.5, 10.6, 6.3], 'eq', { ...nosh, lines: 1 })
  const eqB = box([17.5, 7.4, 4.35, 21.5, 10.6, 6.3], 'eq', { ...nosh, lines: 1 })
  inner.push({ key: key('hvac'), spot: 'hvac', building: 'factory', layer: 'factory', depth: ahu.depth, palette: 'eq2', shapes: ahu.shapes })
  inner.push({ key: key('eq-a'), spot: 'equipment', building: 'factory', layer: 'factory', depth: eqA.depth, palette: 'eq', shapes: eqA.shapes })
  inner.push({ key: key('eq-b'), spot: 'equipment', building: 'factory', layer: 'factory', depth: eqB.depth, palette: 'eq', shapes: eqB.shapes })
  shapes.push(pl([[7.5, 9, 6.3], [7.5, 9, 7.3], [22.5, 9, 7.3], [22.5, 9, 6]], 'pipe.water', PW * 1.6))
  shapes.push(pl([[13.5, 9, 6.3], [13.5, 9, 7.3]], 'pipe.water', PW * 1.6))
  shapes.push(pl([[19.5, 9, 6.3], [19.5, 9, 7.3]], 'pipe.water', PW * 1.6))
  for (let x = x0; x <= x1; x += 5) shapes.push(pl([[x, y0 + 6, 0], [x, y0 + 6, 4]], 'line', SW * 1.4))

  const eq: { depth: number; shapes: IsoShape[] }[] = []
  for (const y of [14.2, 18.2]) {
    eq.push(box([6, y + 0.3, 0, 21.5, y + 1.5, 0.9], 'eq', nosh))
    for (const x of [7, 12, 17]) eq.push(box([x + (y > 15 ? 1.5 : 0), y, 0, x + 2.6 + (y > 15 ? 1.5 : 0), y + 1.8, 2.3], 'eq2', { ...nosh, lines: 1 }))
  }
  eq.push(box([21.8, 12.5, 0, 23.2, 14, 3], 'eq', nosh))
  eq.sort((a, b) => a.depth - b.depth)
  inner.push({
    key: key('floor'), spot: 'equipment', building: 'factory', layer: 'factory', depth: 0, palette: 'eq',
    shapes: eq.map((item, index) => item.shapes.map(shape => ({ ...shape, key: `eq${index}-${shape.key}` }))).flat(),
  })

  shapes.push(...box([x0, y1 - 0.35, 0, x1, y1, 1.1], 'n', nosh).shapes)
  shapes.push(...box([x1 - 0.35, y0, 0, x1, y1 - 0.35, 1.1], 'n', nosh).shapes)

  const outline: IsoShape[] = [
    pl([[x0, y0, h], [x1, y0, h], [x1, y1, h], [x0, y1, h], [x0, y0, h]], 'env', SW, ENV_DASH),
    pl([[x1, y1, 1.1], [x1, y1, h]], 'env', SW, ENV_DASH),
    pl([[x1, y0, 1.1], [x1, y0, h]], 'env', SW, ENV_DASH),
    pl([[x0, y1, 1.1], [x0, y1, h]], 'env', SW, ENV_DASH),
  ]

  return [
    { key: key('shell'), spot: 'factory', building: 'factory', layer: 'factory', depth: 28, palette: 'inner', shapes },
    ...inner,
    // 车间外轮廓：建筑级事件/动作只描这一圈（剖开时的墙顶与四个转角）。
    { key: key('outline'), spot: 'factory', building: 'factory', layer: 'factory', depth: 29, palette: 'inner', edge: true, shapes: outline },
  ]
}

/** 完整场景。 */
export function buildScene(options: SceneOptions): IsoScene {
  const ground: IsoShape[] = []
  const shadow: IsoShape[] = []
  const items: IsoPart[] = []
  const add = (part: Omit<IsoPart, 'layer'> & { layer?: SceneLayer }) =>
    items.push({ ...part, layer: part.layer ?? 'item' })

  ground.push(poly([[0, 0, 0], [46, 0, 0], [46, 34, 0], [0, 34, 0]], 'ground', 'groundLine', SW))
  for (let x = 2; x < 46; x += 2) ground.push(pl([[x, 0, 0], [x, 34, 0]], 'grid', 0.5))
  for (let y = 2; y < 34; y += 2) ground.push(pl([[0, y, 0], [46, y, 0]], 'grid', 0.5))
  ground.push(poly([[28.4, 0, 0], [29.8, 0, 0], [29.8, 34, 0], [28.4, 34, 0]], 'road', 'none', 0))
  ground.push(poly([[0, 23.9, 0], [28.4, 23.9, 0], [28.4, 25.1, 0], [0, 25.1, 0]], 'road', 'none', 0))

  const factoryParts = factory(options.roofCut, shadow)

  // 行政楼 3 层 + 电梯井
  const admin = box([30, 6, 0, 40, 14, 9], 'n', { lines: 2, mx: 7, my: 5, shadow })
  add({ key: 'admin', spot: 'admin', building: 'admin', depth: 45, palette: 'n', edge: true, shapes: admin.shapes })
  const shaft = box([40, 8, 0, 42, 11, 10.5], 'n', { shadow })
  const stripe = (z0: number, z1: number): IsoShape => ({
    el: 'polygon', key: `stripe${z0}`, points: points([[42, 8, z0], [42, 11, z0], [42, 11, z1], [42, 8, z1]]),
    fill: 'agent', stroke: 'agentPale', sw: SW,
  })
  add({
    key: 'elevator', spot: 'elevator', building: 'admin', depth: 50.5, palette: 'n',
    shapes: [...shaft.shapes, stripe(3.2, 4.6), stripe(6.4, 7.8)],
  })

  // 能源站：T1、T2、配电室、储能
  add({ key: 't1', spot: 'transformer', building: 'energy', depth: 54.75, palette: 'n', shapes: box([31, 21, 0, 34, 23.5, 2.4], 'n', { lines: 3, shadow }).shapes })
  add({ key: 't2', spot: null, building: 'energy', depth: 59.25, palette: 'n', shapes: box([35.5, 21, 0, 38.5, 23.5, 2.4], 'n', { lines: 3, shadow }).shapes })
  add({ key: 'power', spot: null, building: 'energy', depth: 62.25, palette: 'n', shapes: box([31, 25.5, 0, 38.5, 29.5, 3.2], 'n', { mx: 3 }).shapes })
  for (const y of [20.5, 23.2, 25.9]) {
    const battery = box([40, y, 0, 45, y + 1.8, 1.9], 'n', { mx: 4 })
    add({ key: `bess-${y}`, spot: 'battery', building: 'energy', depth: battery.depth, palette: 'n', shapes: battery.shapes })
  }

  // 空压站 + 储气罐
  add({ key: 'air', spot: 'compressor', building: 'air', depth: 37, palette: 'n', shapes: box([6, 26, 0, 12, 30, 3.2], 'n', { mx: 2 }).shapes })
  add({ key: 'air-tank-1', spot: null, building: 'air', depth: 40.8, palette: 'n', shapes: cyl(13.6, 27.2, 0, 0.85, 3.6, 'n').shapes })
  add({ key: 'air-tank-2', spot: null, building: 'air', depth: 43, palette: 'n', shapes: cyl(13.6, 29.4, 0, 0.85, 3.6, 'n').shapes })

  // 冷却塔
  add({ key: 'cooling', spot: 'cooling', building: 'cooling', depth: 36.3, palette: 'n', shapes: box([25.2, 6.9, 0, 28, 12.5, 0.5], 'n').shapes })
  add({ key: 'cooling-1', spot: 'cooling', building: 'cooling', depth: 35, palette: 'n', shapes: cyl(26.6, 8.4, 0.5, 1.05, 2.4, 'n').shapes })
  add({ key: 'cooling-2', spot: 'cooling', building: 'cooling', depth: 37.5, palette: 'n', shapes: cyl(26.6, 10.9, 0.5, 1.05, 2.4, 'n').shapes })

  // 光伏车棚
  for (let j = 0; j < 3; j++) {
    const y = 26.2 + j * 2.1
    const canopy = box([17, y, 1.5, 27, y + 1.6, 1.68], 'pv', { gx: 7 })
    add({
      key: `pv-${j}`, spot: 'pv', building: 'pv', depth: canopy.depth, palette: 'pv',
      shapes: [
        pl([[17.3, y + 1.4, 0], [17.3, y + 1.4, 1.5]], 'line', SW),
        pl([[26.7, y + 1.4, 0], [26.7, y + 1.4, 1.5]], 'line', SW),
        ...canopy.shapes,
      ],
    })
  }

  // 压缩空气管廊、冷冻水管
  add({
    key: 'pipes', spot: null, building: null, depth: 41, palette: 'n',
    shapes: [
      pl([[13.8, 28, 0], [13.8, 28, 2.6]], 'line', SW),
      pl([[15.5, 25.6, 0], [15.5, 25.6, 2.6]], 'line', SW),
      pl([[12, 28, 2.6], [15.5, 28, 2.6], [15.5, 22, 2.6], [15.5, 22, 1.1]], 'pipe.air', PW * 1.8),
    ],
  })
  add({ key: 'chilled', spot: null, building: null, depth: 36.3, palette: 'n', shapes: [pl([[26.6, 9.6, 1.7], [24, 9.6, 1.7]], 'pipe.water', PW * 1.8)] })

  // 地面：母线、放电路径
  ground.push(...shadow)
  ground.push(pl([[31, 22.2, 0], [27.6, 22.2, 0], [27.6, 16, 0], [24, 16, 0]], 'pipe.power', PW))
  ground.push(pl([[27.6, 16, 0], [27.6, 10, 0], [30, 10, 0]], 'pipe.power', PW))
  ground.push(pl([[40, 24.6, 0], [27.6, 24.6, 0], [27.6, 22.2, 0]], 'pipe.act', PW * 1.5, ACT_DASH, 'flow'))

  const sorted = [...items].sort((a, b) => {
    const order = (part: IsoPart) => part.layer === 'factory' ? 0 : 1
    return order(a) - order(b) || a.depth - b.depth
  })

  const m = options.margin ?? { l: 70, r: 80, t: 78, b: 14 }
  const viewBox = {
    x: -34 * C * S - m.l,
    y: -3 * S - m.t,
    w: 80 * C * S + m.l + m.r,
    h: 43 * S + m.t + m.b,
  }

  return { ground, parts: [...factoryParts, ...sorted], viewBox }
}

export interface Bounds { x: number; y: number; w: number; h: number }

function boundsBy(parts: IsoPart[], pick: (part: IsoPart) => string | null): Record<string, Bounds> {
  const raw: Record<string, { x: number; y: number; x1: number; y1: number }> = {}
  for (const part of parts) {
    const key = pick(part)
    if (!key) continue
    const box = raw[key] ?? { x: Infinity, y: Infinity, x1: -Infinity, y1: -Infinity }
    for (const shape of part.shapes) {
      const pts: Pt[] = shape.el === 'polygon' || shape.el === 'polyline' ? shape.points
        : shape.el === 'ellipse' ? [[shape.cx - shape.rx, shape.cy - shape.ry], [shape.cx + shape.rx, shape.cy + shape.ry]] : []
      for (const [x, y] of pts) {
        box.x = Math.min(box.x, x)
        box.y = Math.min(box.y, y)
        box.x1 = Math.max(box.x1, x)
        box.y1 = Math.max(box.y1, y)
      }
    }
    raw[key] = box
  }
  const out: Record<string, Bounds> = {}
  for (const [key, box] of Object.entries(raw)) out[key] = { x: box.x, y: box.y, w: box.x1 - box.x, h: box.y1 - box.y }
  return out
}

/** 每个建筑在屏幕坐标里的包围盒，给“缩放到建筑”用。 */
export const buildingBounds = (parts: IsoPart[]) => boundsBy(parts, part => part.building)

/** 每个可选中位置在屏幕坐标里的包围盒，给镜头推近用。 */
export const spotBounds = (parts: IsoPart[]) => boundsBy(parts, part => part.spot)

/** 实体标注的位置与偏移，照 iso.js 的 co() 参数。 */
export const ANCHORS: Record<string, { at: [number, number, number]; dx: number; dy: number }> = {
  factory: { at: [14, 6, 8], dx: -60, dy: -34 },
  hvac: { at: [7.5, 9, 7.3], dx: -150, dy: 30 },
  elevator: { at: [41, 9.5, 10.5], dx: 40, dy: -28 },
  transformer: { at: [32.5, 21, 2.4], dx: 190, dy: 26 },
  battery: { at: [42.5, 26.8, 1.9], dx: 60, dy: 62 },
  compressor: { at: [9, 28, 3.2], dx: -46, dy: 62 },
  pv: { at: [22, 32, 1.68], dx: -50, dy: 70 },
}


