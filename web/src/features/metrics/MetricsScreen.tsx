import { useEffect, useMemo, useRef } from 'react'
import type * as echarts from 'echarts'
import { getBaselines, getReviewMetrics, type EvaluationReportRaw, type MetricSeriesRaw } from '../../api'
import { prefersReducedMotion } from '../../shared/motion'
import { useDay } from '../../store'
import { NatureBadge } from '../common/nature'
import { useRemote } from '../common/remote'
import './metrics.css'

/** 三组对照：基线组无 Agent，独立组各自优化，协同组由主 Agent 统一调度。 */
const GROUPS = [
  { key: 'baseline', label: '基线组', sub: '无 Agent' },
  { key: 'independent', label: '独立组', sub: '各自优化' },
  { key: 'coordinated', label: '协同组', sub: '主 Agent' },
] as const
type GroupKey = (typeof GROUPS)[number]['key']
const GROUP_KEYS: GroupKey[] = GROUPS.map(group => group.key)

/**
 * 《愿景》数字清单的指标。alias 里同时列了演示日记录的名字（B35 数据）和 B33 报告里的名字，
 * 单位取数据自带的值，报告没有的指标行直接不显示。
 */
type Alias = { metric: string; unit: string }
type VisionRow = { metric: string; label: string; aliases: Alias[] }
const VISION_ROWS: VisionRow[] = [
  {
    metric: 'cost_cny',
    label: '当天电费',
    aliases: [{ metric: 'cost_cny', unit: 'CNY' }, { metric: 'electricity_cost', unit: 'CNY' }],
  },
  {
    metric: 'energy_kwh',
    label: '用电量',
    aliases: [{ metric: 'energy_kwh', unit: 'kWh' }, { metric: 'load', unit: 'kWh' }],
  },
  {
    metric: 'transformer_peak_pct',
    label: '变压器峰值',
    aliases: [{ metric: 'transformer_peak_pct', unit: '%' }, { metric: 'peak_import', unit: 'kW' }],
  },
  {
    metric: 'avg_wait_min',
    label: '平均候梯',
    aliases: [{ metric: 'avg_wait_min', unit: 'min' }, { metric: 'congestion_duration', unit: 'min' }],
  },
  {
    metric: 'comfort_violation_min',
    label: '舒适度越限',
    aliases: [
      { metric: 'comfort_violation_min', unit: 'min' },
      { metric: 'ordinary_temperature_violation', unit: 'zone-min' },
      { metric: 'hard_temperature_violation', unit: 'zone-min' },
    ],
  },
  {
    metric: 'manual_interventions',
    label: '人工干预',
    aliases: [{ metric: 'manual_interventions', unit: '次' }, { metric: 'human_interruptions', unit: '次' }],
  },
]

type MetricValues = Record<GroupKey, number | null>
type MetricRow = { metric: string; label: string; unit: string; values: MetricValues; note?: string }
/** origin：三组数字来自哪一层，徽章与来源句按它写；live 由 useRemote 另外给出。 */
type Comparison = { origin: 'baselines' | 'day' | 'mock'; rows: MetricRow[]; comparable?: boolean; notes?: string[] }
type SeriesPoint = { metric: string; group: string; value: number | null; unit?: string | null; note?: string | null }

/** 接口与本目录都没有数据时的兜底；数字是演示补编，界面标 synthetic。 */
const MOCK_METRICS: SeriesPoint[] = [
  { metric: 'cost_cny', group: 'baseline', value: 19040, unit: 'CNY', note: '仿真演示场景' },
  { metric: 'cost_cny', group: 'independent', value: 18410, unit: 'CNY', note: '仿真演示场景' },
  { metric: 'cost_cny', group: 'coordinated', value: 17520, unit: 'CNY', note: '仿真演示场景' },
  { metric: 'energy_kwh', group: 'baseline', value: 23560, unit: 'kWh', note: '仿真演示场景' },
  { metric: 'energy_kwh', group: 'independent', value: 22840, unit: 'kWh', note: '仿真演示场景' },
  { metric: 'energy_kwh', group: 'coordinated', value: 21950, unit: 'kWh', note: '仿真演示场景' },
  { metric: 'transformer_peak_pct', group: 'baseline', value: 113, unit: '%', note: '仿真演示场景' },
  { metric: 'transformer_peak_pct', group: 'independent', value: 106, unit: '%', note: '仿真演示场景' },
  { metric: 'transformer_peak_pct', group: 'coordinated', value: 94, unit: '%', note: '仿真演示场景' },
  { metric: 'avg_wait_min', group: 'baseline', value: 6.8, unit: 'min', note: '仿真演示场景' },
  { metric: 'avg_wait_min', group: 'independent', value: 5.4, unit: 'min', note: '仿真演示场景' },
  { metric: 'avg_wait_min', group: 'coordinated', value: 3.8, unit: 'min', note: '仿真演示场景' },
  { metric: 'comfort_violation_min', group: 'baseline', value: 228, unit: 'min', note: '仿真演示场景' },
  { metric: 'comfort_violation_min', group: 'independent', value: 139, unit: 'min', note: '仿真演示场景' },
  { metric: 'comfort_violation_min', group: 'coordinated', value: 64, unit: 'min', note: '仿真演示场景' },
  { metric: 'manual_interventions', group: 'baseline', value: 19, unit: '次', note: '仿真演示场景' },
  { metric: 'manual_interventions', group: 'independent', value: 14, unit: '次', note: '仿真演示场景' },
  { metric: 'manual_interventions', group: 'coordinated', value: 4, unit: '次', note: '仿真演示场景' },
]

const MOCK_REVIEW: MetricSeriesRaw = {
  metric: 'review_f1',
  dataset: 'demo',
  split_id: 'b35-metrics-mock',
  points: [[0, 0.55], [20, 0.64], [50, 0.72], [100, 0.77], [200, 0.8], [400, 0.79]],
  note: '基准评测曲线',
}

/** 与 theme.css 的 --line-hover / --text-2 / --agent 同值：echarts 不解析 CSS 变量，这里按 token 抄一份。 */
const GROUP_COLOR: Record<GroupKey, string> = {
  baseline: '#6b7a87',
  independent: '#aeb8c3',
  coordinated: '#5cb3f2',
}
/** 图表文字与轴线同样取 theme.css 的 --text-2 / --text-3 / --line / --line-soft / --text / --panel-2。 */
const CHART_TEXT = '#aeb8c3'
const CHART_MUTED = '#96a1ad'
const CHART_LINE = '#2a323c'
const CHART_GRID = '#222a33'
const CHART_LABEL = '#e6eaee'
const CHART_PANEL = '#171d24'

const NUMBER = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 })
const valueText = (value: number | null) => (value === null ? '—' : NUMBER.format(value))

/** 把 day.metrics / mock 这种平铺记录按指标名收拢成三组数值。 */
function metricRows(series: readonly SeriesPoint[]): MetricRow[] {
  const byMetric = new Map<string, SeriesPoint[]>()
  for (const point of series) {
    if (!GROUP_KEYS.includes(point.group as GroupKey)) continue
    const list = byMetric.get(point.metric)
    if (list) list.push(point)
    else byMetric.set(point.metric, [point])
  }
  const rows: MetricRow[] = []
  for (const vision of VISION_ROWS) {
    const alias = vision.aliases.find(item => byMetric.has(item.metric))
    if (!alias) continue
    const points = byMetric.get(alias.metric) ?? []
    const values: MetricValues = { baseline: null, independent: null, coordinated: null }
    for (const group of GROUP_KEYS) values[group] = points.find(point => point.group === group)?.value ?? null
    rows.push({
      metric: vision.metric,
      label: vision.label,
      unit: points.find(point => point.unit)?.unit ?? alias.unit,
      values,
      note: points.find(point => point.note)?.note ?? undefined,
    })
  }
  return rows
}

/** 第二优先级：演示日记录里的三组数值；一条都没有时退到本目录 mock。 */
function comparisonFromDay(series: readonly SeriesPoint[]): Comparison {
  const rows = metricRows(series)
  return rows.length ? { origin: 'day', rows } : { origin: 'mock', rows: metricRows(MOCK_METRICS) }
}

/** 第一优先级：B33 的 /baselines 报告，取含三组政策的最近一份；没有就抛错让 remote 层退回。 */
function comparisonFromBaselines(reports: EvaluationReportRaw[]): Comparison {
  const report = reports.find(item => GROUP_KEYS.every(group => item.policies.includes(group)))
  if (!report) throw new Error('接口没有返回含基线/独立/协同三组的报告')
  const rows: MetricRow[] = []
  for (const vision of VISION_ROWS) {
    const alias = vision.aliases.find(item => GROUP_KEYS.some(group => report.metrics[group]?.[item.metric] !== undefined))
    if (!alias) continue
    const values: MetricValues = { baseline: null, independent: null, coordinated: null }
    for (const group of GROUP_KEYS) values[group] = report.metrics[group]?.[alias.metric] ?? null
    rows.push({ metric: vision.metric, label: vision.label, unit: alias.unit, values })
  }
  if (!rows.length) throw new Error('接口报告里没有可对照的指标')
  return { origin: 'baselines', rows, comparable: report.comparable, notes: report.notes }
}

/** 协同组较基线组省下的整日电费；没有电费行时为 null。 */
function savedCost(rows: MetricRow[]): number | null {
  const row = rows.find(item => item.metric === 'cost_cny')
  const base = row?.values.baseline
  const coordinated = row?.values.coordinated
  return base == null || coordinated == null ? null : base - coordinated
}

function changeText(row: MetricRow): string {
  const base = row.values.baseline
  const coordinated = row.values.coordinated
  if (base === null || coordinated === null || base === 0) return '—'
  const pct = Math.round(((coordinated - base) / Math.abs(base)) * 100)
  return pct === 0 ? '持平' : `${pct < 0 ? '↓' : '↑'}${Math.abs(pct)}%`
}

function reviewValueLabel(metric: string): string {
  const name = metric.toLowerCase()
  if (name.includes('f1')) return 'F1'
  if (name.includes('cost') || name.includes('error')) return '费用误差'
  return metric
}

/** 容器有布局尺寸后才初始化；jsdom 和隐藏面板量不到尺寸时留空容器，等 resize 再画。 */
function EChart({ option, ariaLabel }: { option: echarts.EChartsOption; ariaLabel: string }) {
  const hostRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let chart: echarts.EChartsType | null = null
    let cancelled = false
    // echarts 约 400 kB，首屏不加载：等容器真有尺寸时再动态拉，加载失败就留空容器（数字表里已有数值）。
    const draw = async () => {
      if (chart || !host.clientWidth || !host.clientHeight) return
      const loaded = await import('echarts').catch(() => null)
      if (cancelled || chart || !loaded || !host.clientWidth || !host.clientHeight) return
      chart = loaded.init(host)
      chart.setOption(option)
    }
    void draw()
    const onResize = () => {
      if (chart) chart.resize()
      else void draw()
    }
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', onResize)
      return () => { cancelled = true; window.removeEventListener('resize', onResize); chart?.dispose() }
    }
    const observer = new ResizeObserver(onResize)
    observer.observe(host)
    return () => { cancelled = true; observer.disconnect(); chart?.dispose() }
  }, [option])

  return <div ref={hostRef} className="mx-chart-body" role="img" aria-label={ariaLabel} />
}

function BarChart({ row }: { row: MetricRow }) {
  const reduced = prefersReducedMotion()
  const option = useMemo<echarts.EChartsOption>(() => ({
    animation: !reduced,
    grid: { left: 8, right: 10, top: 22, bottom: 2, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: CHART_PANEL,
      borderColor: CHART_LINE,
      textStyle: { color: CHART_LABEL, fontSize: 12 },
    },
    xAxis: {
      type: 'category',
      data: GROUPS.map(group => group.label.replace('组', '')),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: CHART_LINE } },
      axisLabel: { color: CHART_TEXT, fontSize: 11 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: CHART_MUTED, fontSize: 10 },
      splitLine: { lineStyle: { color: CHART_GRID } },
    },
    series: [{
      type: 'bar',
      name: row.label,
      barWidth: '46%',
      data: GROUPS.map(group => ({ value: row.values[group.key], itemStyle: { color: GROUP_COLOR[group.key] } })),
      label: { show: true, position: 'top', color: CHART_LABEL, fontSize: 11 },
    }],
  }), [reduced, row])
  return <EChart option={option} ariaLabel={`${row.label}：基线组、独立组、协同组柱状对比`} />
}

function ReviewCurve({ series, yName }: { series: MetricSeriesRaw; yName: string }) {
  const reduced = prefersReducedMotion()
  const option = useMemo<echarts.EChartsOption>(() => ({
    animation: !reduced,
    grid: { left: 10, right: 18, top: 28, bottom: 28, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: CHART_PANEL,
      borderColor: CHART_LINE,
      textStyle: { color: CHART_LABEL, fontSize: 12 },
    },
    xAxis: {
      type: 'value',
      name: '复核量',
      nameLocation: 'middle',
      nameGap: 24,
      nameTextStyle: { color: CHART_MUTED, fontSize: 11 },
      min: 0,
      axisLine: { lineStyle: { color: CHART_LINE } },
      axisLabel: { color: CHART_MUTED, fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      name: yName,
      scale: true,
      nameTextStyle: { color: CHART_MUTED, fontSize: 11 },
      axisLabel: { color: CHART_MUTED, fontSize: 10 },
      splitLine: { lineStyle: { color: CHART_GRID } },
    },
    series: [{
      type: 'line',
      name: yName,
      smooth: true,
      symbol: 'circle',
      symbolSize: 5,
      data: series.points,
      lineStyle: { color: CHART_TEXT, width: 2 },
      itemStyle: { color: CHART_TEXT },
      // --text-2 的 12% 透明，做面积底不加新色
      areaStyle: { color: 'rgba(174, 184, 195, 0.12)' },
    }],
  }), [reduced, series, yName])
  return <EChart option={option} ariaLabel={`复核量 - ${yName} 曲线`} />
}

/** 数字对比屏：三组对照表、关键指标柱状图与复核量曲线；接口不通时逐项退回演示数据并标性质。 */
export function MetricsScreen() {
  const day = useDay(state => state.day)
  const metrics = useRemote<Comparison>(
    async () => comparisonFromBaselines(await getBaselines()),
    () => comparisonFromDay(day.metrics),
    [day.run_id],
  )
  const review = useRemote<MetricSeriesRaw>(
    () => getReviewMetrics(),
    () => MOCK_REVIEW,
    [],
  )

  if (!metrics || !review) {
    return (
      <div className="metrics">
        <section className="panel">
          <div className="sec"><p className="dm mx-loading">正在读取三组对照与复核曲线…</p></div>
        </section>
      </div>
    )
  }

  const rows = metrics.data.rows
  const saved = savedCost(rows)
  const series = review.data
  const yName = reviewValueLabel(series.metric)
  const metricsOrigin = metrics.data.origin === 'day' ? '标准演示场景' : '仿真演示场景'
  const reviewOrigin = review.live ? '实时评测接口' : '基准评测曲线'

  return (
    <div className="metrics">
      <section className="panel mx-hero">
        <div className="sec mx-hero-row">
          <div className="sec-title">数字对比
            <span className="dm">基线组 / 独立组 / 协同组，同一天、同一组事件、同样的储能起止条件</span>
          </div>
          <span className="mx-badges">
            <NatureBadge nature={metrics.live ? 'derived' : 'synthetic'} source={metrics.live ? '/baselines 接口' : metricsOrigin} />
          </span>
          <div className="mx-saved">
            <b className="n">{saved == null ? '—' : `¥${NUMBER.format(Math.abs(saved))}`}</b>
            <span className="lb">{saved == null ? '协同组较基线整日电费' : saved >= 0 ? '协同组较基线单日电费节省' : '协同组较基线单日电费增加'}</span>
          </div>
        </div>
        <div className="sec mx-source">
          <span className="lb">对照数据</span>
          <span className="dm mx-source-text">
            {metrics.live ? '数据来自实时评测接口（三组对照独立核算）' : metrics.note}
          </span>
          {metrics.data.comparable === false && <span className="chip">报告标注不可比</span>}
          {metrics.data.notes?.length ? <span className="dm" title={metrics.data.notes.join('；')}>报告备注 {metrics.data.notes.length} 条</span> : null}
        </div>
      </section>

      <div className="mx-top">
        <section className="panel mx-table-panel" aria-label="三组指标对比表">
          <div className="sec">
            <div className="sec-title">三组指标<span className="dm">单位、三组数值、协同组相对基线组的变化</span></div>
          </div>
          <div className="mx-table-wrap">
            <table className="mx-table">
              <thead>
                <tr>
                  <th scope="col">指标</th>
                  <th scope="col">单位</th>
                  {GROUPS.map(group => (
                    <th scope="col" key={group.key}>{group.label}<span className="dm">{group.sub}</span></th>
                  ))}
                  <th scope="col">协同组较基线</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(row => (
                  <tr key={row.metric}>
                    <th scope="row" title={row.note ?? undefined}>{row.label}</th>
                    <td className="mx-unit">{row.unit || '—'}</td>
                    {GROUPS.map(group => (
                      <td key={group.key} className="n">{valueText(row.values[group.key])}</td>
                    ))}
                    <td className="n mx-delta" title="协同组相对基线组的变化，↓ 表示降低">{changeText(row)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel mx-curve-panel" aria-label="复核量曲线">
          <div className="sec">
            <div className="sec-title">复核量 - {yName} 曲线
              <NatureBadge nature={review.live ? 'derived' : 'synthetic'} source={reviewOrigin} />
            </div>
            <p className="dm mx-note">
              {review.live ? `数据来自实时评测接口${series.note ? `：${series.note}` : ''}` : review.note}
            </p>
          </div>
          <div className="mx-curve-body">
            {series.points.length
              ? <ReviewCurve series={series} yName={yName} />
              : <p className="dm mx-loading">接口没有返回复核曲线。</p>}
          </div>
        </section>
      </div>

      <section className="panel mx-bars-panel" aria-label="关键指标柱状对比">
        <div className="sec">
          <div className="sec-title">关键指标柱状对比<span className="dm">三组数值并列，单位见各图右上；协同组用 Agent 冷蓝</span></div>
        </div>
        <div className="mx-bars">
          {rows.map(row => (
            <div className="mx-bar" key={row.metric}>
              <div className="mx-bar-head"><b>{row.label}</b><span className="dm">{row.unit || '—'}</span></div>
              <BarChart row={row} />
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
