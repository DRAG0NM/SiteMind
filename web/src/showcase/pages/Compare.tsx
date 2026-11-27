import type { PageProps } from './types'
import type { DayMetric } from '../data/types'
import { LoadCurve } from '../../shared/LoadCurve'
import { pctDiff } from '../../shared/readings'
import { transformerLoadPoints } from '../data/rollup'
import { dayAtHour, dayEnd } from '../data/time'
import './knowledge.css'

const GROUPS = [
  { key: 'baseline', label: '基线组 无 Agent', tone: 'base' },
  { key: 'independent', label: '独立组 各自优化', tone: 'solo' },
  { key: 'coordinated', label: '协同组 主 Agent', tone: 'coord' },
] as const

const ROWS = [
  { metric: 'cost_cny', label: '当天电费' },
  { metric: 'energy_kwh', label: '用电量' },
  { metric: 'transformer_peak_pct', label: '变压器峰值' },
  { metric: 'avg_wait_min', label: '平均候梯' },
  { metric: 'comfort_violation_min', label: '舒适度越限' },
  { metric: 'manual_interventions', label: '人工介入' },
]

/** 知识侧指标（B26/B31/B48）：激活率只作辅助，不换算成收益。 */
const KNOWLEDGE_ROWS = [
  { metric: 'asset_activation_rate', label: '资产激活率' },
  { metric: 'standard_alignment_rate', label: '标准对齐率' },
  { metric: 'ontology_rule_f1', label: '本体与规则抽取 F1' },
  { metric: 'build_efficiency_min_per_1k', label: '构建效率' },
  { metric: 'doc_qa_accuracy', label: '跨文档问答正确率' },
  { metric: 'doc_qa_accuracy_rag', label: '对照：普通 RAG' },
]

/** 编排对照（B33/D54）：两种派发口径，指标名与设备侧三组共用 metrics.group。 */
const ORCH_ARMS = [
  { group: 'baseline', label: '全程序派发（关闭慢通道）' },
  { group: 'coordinated', label: '快慢双通道' },
]
const ORCH_ROWS = [
  { metric: 'orchestration_accuracy', label: '处理正确率', kind: 'percent' as const },
  { metric: 'orchestration_evidence_rate', label: '证据完整率', kind: 'percent' as const },
  { metric: 'orchestration_latency_s', label: '平均研判耗时', kind: 'seconds' as const },
]

/** Harness 行为回归（B27/D58）：指标名带 @版本 后缀，一个版本一行。 */
const HARNESS_ROWS = [
  { metric: 'harness_pass_rate', label: '回归集通过率', kind: 'percent' as const },
  { metric: 'harness_tool_calls_avg', label: '平均工具调用', kind: 'count' as const },
  { metric: 'harness_duration_s', label: '平均耗时', kind: 'seconds' as const },
]

/** 首屏四个比基线的变化，都是越少越好 */
const KPIS = [
  { metric: 'transformer_peak_pct', label: '变压器峰值' },
  { metric: 'avg_wait_min', label: '平均候梯时间' },
  { metric: 'comfort_violation_min', label: '舒适度越限时长' },
  { metric: 'manual_interventions', label: '人工介入次数' },
]
const B33_KPIS = [
  { metric: 'peak_import', label: '购电分钟峰值' },
  { metric: 'ordinary_temperature_violation', label: '普通区域舒适度越限' },
  { metric: 'work_orders', label: '生成工单' },
  { metric: 'electricity_cost', label: '两日电费' },
]

const money = (value: number) => `¥${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)}`
const number = (value: number) => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(value)
/** 比率类指标可能写成 0.82 或 82，单位写了 % 就按百分数，否则按 0~1 换算。 */
const ratioPercent = (value: number, unit?: string | null) =>
  `${number(unit === '%' ? value : value * 100)}%`

/**
 * 指标名可以带 `@维度` 后缀，例如 `harness_pass_rate@v2`、`learning_agent_share@批次1`。
 * B33/B36 尚未定型；没有后缀时按单点处理，页面不会因为缺维度而崩。
 */
function splitName(raw: string): { name: string; variant: string } {
  const at = raw.lastIndexOf('@')
  return at > 0 ? { name: raw.slice(0, at), variant: raw.slice(at + 1) } : { name: raw, variant: '' }
}

function formatKind(value: number, kind: 'percent' | 'count' | 'seconds', unit?: string | null) {
  if (kind === 'percent') return ratioPercent(value, unit)
  if (kind === 'seconds') return `${number(value)} s`
  return unit === '次' ? `${number(value)} 次` : number(value)
}

/** 通用小趋势图：点位少也能画；只有 1 个点时只画点，不连线。 */
function MiniTrend({ points, ariaLabel, format }: {
  points: { label: string; value: number }[]
  ariaLabel: string
  format: (value: number) => string
}) {
  if (!points.length) return null
  const width = 240
  const height = 96
  const padX = 10
  const padT = 16
  const padB = 22
  const values = points.map(point => point.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || Math.abs(max) || 1
  const X = (index: number) =>
    padX + (points.length === 1 ? (width - padX * 2) / 2 : (index / (points.length - 1)) * (width - padX * 2))
  const Y = (value: number) => height - padB - ((value - min) / span) * (height - padT - padB)
  const path = points.map((point, index) => `${index ? 'L' : 'M'}${X(index).toFixed(1)} ${Y(point.value).toFixed(1)}`).join('')

  return (
    <svg className="cmp-mini" viewBox={`0 0 ${width} ${height}`} width="100%" height="100%" role="img" aria-label={ariaLabel}>
      <line className="cmp-axis" x1={padX} y1={height - padB} x2={width - padX} y2={height - padB} />
      {points.length > 1 && <path className="cmp-line" d={path} />}
      {points.map((point, index) => (
        <g key={point.label + index}>
          <circle className="cmp-point" cx={X(index)} cy={Y(point.value)} r={3.2}>
            <title>{`${point.label}：${format(point.value)}`}</title>
          </circle>
          {(index === 0 || index === points.length - 1) && (
            <text x={X(index)} y={Y(point.value) - 7} textAnchor={index === 0 ? 'start' : 'end'}>{format(point.value)}</text>
          )}
          {(index === 0 || index === points.length - 1) && (
            <text className="cmp-xlabel" x={X(index)} y={height - 6} textAnchor={index === 0 ? 'start' : 'end'}>{point.label}</text>
          )}
        </g>
      ))}
    </svg>
  )
}

/** 协同组比基线组的变化，越少越好的指标下降记为 ↓。 */
const change = (base: number | null, coord: number | null) => {
  if (base === null || coord === null) return null
  if (coord === base) return '持平'
  if (base === 0) return null
  const pct = Math.abs((coord - base) / base) * 100
  const magnitude = pct < 0.01 ? '<0.01' : pct.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  return `${coord < base ? '↓' : '↑'}${magnitude}%`
}

export function ComparePage({ day, cursor }: PageProps) {
  const b33 = day.metrics.some(item => item.metric === 'electricity_cost')
  // B33 evaluates the whole two-day scenario independently of the replay run.
  const comparisonRows = b33 ? [
    { metric: 'electricity_cost', label: '两日电费' },
    { metric: 'load', label: '两日用电量' },
    { metric: 'peak_import', label: '购电分钟峰值' },
    { metric: 'ordinary_temperature_violation', label: '普通区域舒适度越限' },
    { metric: 'work_orders', label: '生成工单' },
  ] : ROWS
  const points = (name: string) => day.metrics
    .map(item => ({ ...splitName(item.metric), ...item }))
    .filter((item): item is DayMetric & { name: string; variant: string } => item.name === name && item.value !== null)
  const one = (name: string, group?: string) => {
    const list = points(name)
    return group ? list.find(item => item.group === group) : list[0]
  }
  const metric = (name: string) => day.metrics.filter(item => item.metric === name)
  const valueOf = (name: string, group: string) => metric(name).find(item => item.group === group)?.value ?? null
  const unitOf = (name: string) => metric(name)[0]?.unit ?? ''
  const noteOf = (name: string) => metric(name).find(item => item.note)?.note ?? ''

  const rows = comparisonRows.map(row => {
    const values = GROUPS.map(group => ({ ...group, value: valueOf(row.metric, group.key) }))
    const present = values.filter(item => item.value !== null) as (typeof values[number] & { value: number })[]
    if (!present.length) return null
    const max = Math.max(...present.map(item => Math.abs(item.value)))
    const coordinated = values.find(item => item.key === 'coordinated')?.value ?? null
    return {
      delta: change(values.find(item => item.key === 'baseline')?.value ?? null, coordinated),
      label: row.label,
      note: noteOf(row.metric),
      coordinated,
      format: (value: number) => ['cost_cny', 'electricity_cost'].includes(row.metric) ? money(value) : `${number(value)} ${unitOf(row.metric)}`,
      bars: values.map(item => ({ ...item, width: item.value === null ? 0 : Math.max(2, (Math.abs(item.value) / (max || 1)) * 100) })),
    }
  }).filter((row): row is NonNullable<typeof row> => row !== null)

  const costMetric = b33 ? 'electricity_cost' : 'cost_cny'
  const costBase = valueOf(costMetric, 'baseline')
  const costCoord = valueOf(costMetric, 'coordinated')
  const costDelta = costBase !== null && costCoord !== null ? costCoord - costBase : null
  const costComparison = (reference: number | null) => {
    if (costCoord === null || reference === null) return '—'
    if (costCoord === reference) return '持平'
    const percent = change(reference, costCoord)
    return `${costCoord > reference ? '增加' : '减少'}${percent ? `（${percent}）` : '（参照电费为零）'}`
  }
  const kpi = (name: string) => change(valueOf(name, 'baseline'), valueOf(name, 'coordinated')) ?? '—'

  // —— 知识侧 ——
  const knowledgeRows = KNOWLEDGE_ROWS.map(row => ({ ...row, point: one(row.metric, 'knowledge') ?? one(row.metric) }))
  const knowledgeAny = knowledgeRows.some(row => row.point)
  const qaPoint = knowledgeRows.find(row => row.metric === 'doc_qa_accuracy')?.point
  const ragPoint = knowledgeRows.find(row => row.metric === 'doc_qa_accuracy_rag')?.point
  // 差值统一走读数层：先按各自 unit 换算成同一比例再相减，unit=% 的 86 与 71 得 15 个百分点。
  const qaGap = qaPoint && ragPoint ? pctDiff(qaPoint, ragPoint) : null
  const knowledgeValue = (row: typeof knowledgeRows[number]) => {
    const point = row.point
    if (!point || point.value === null) return '—'
    if (row.metric === 'ontology_rule_f1') return point.value.toFixed(2)
    if (row.metric === 'build_efficiency_min_per_1k') return `${number(point.value)} ${point.unit ?? 'min/千条'}`
    return ratioPercent(point.value, point.unit)
  }

  // —— 编排对照 ——
  const orchRows = ORCH_ROWS.map(row => ({
    ...row,
    values: ORCH_ARMS.map(arm => one(row.metric, arm.group)),
  }))
  const orchAny = orchRows.some(row => row.values.some(Boolean))
  const orchCases = one('orchestration_cases')?.value

  // —— Harness ——
  const harnessPoints = HARNESS_ROWS.flatMap(row => points(row.metric).map(point => ({ ...point, row: row.metric })))
  const variants: string[] = []
  for (const point of harnessPoints) if (!variants.includes(point.variant)) variants.push(point.variant)
  const harnessRows = variants.map(variant => ({
    label: variant || '当前版本',
    values: HARNESS_ROWS.map(row => points(row.metric).find(point => point.variant === variant)),
  }))
  const passTrend = points('harness_pass_rate')
    .map(point => ({ label: point.variant || '当前', value: point.value as number }))
  const passUnit = points('harness_pass_rate')[0]?.unit
  const shareUnit = points('learning_agent_share')[0]?.unit
  const singleHarness = variants.length <= 1

  // —— 越学越省 ——
  const shareSeries = points('learning_agent_share')
    .map(point => ({ label: point.variant || '当前批次', value: point.value as number }))
  const reviewSeries = points('learning_review_min_per_100')
    .map(point => ({ label: point.variant || '当前批次', value: point.value as number }))
  const learnAny = shareSeries.length > 0 || reviewSeries.length > 0
  const learnSingle = shareSeries.length <= 1 && reviewSeries.length <= 1

  const notes = Array.from(new Set([
    ...rows.map(row => row.note),
    ...knowledgeRows.map(row => row.point?.note ?? ''),
    ...orchRows.flatMap(row => row.values.map(point => point?.note ?? '')),
    ...harnessPoints.map(point => point.note ?? ''),
  ].filter(Boolean)))

  return (
    <div className="compare-page">
      <section className="panel cmp-hero">
        <div>
          <span className="lb">{b33 ? '同一剧本、相同储能起止条件' : '同一天、同一组事件、同样的储能起止条件'}</span>
          <span className="dm">{b33 ? '独立运行的两日仿真对照结果，与当前单日回放账本分别核算。' : '三组指标在同一场景下独立重算，用于直观对比不同编排策略的日级效果。'}</span>
          <h1>协同组{b33 ? '两日' : '当天'}电费<br />较基线组差额</h1>
        </div>
        <div>
          <b className="n cmp-saved">{costDelta === null ? '—' : money(costDelta)}</b>
          <div className="lb">较基线组{costComparison(costBase)}</div>
          <div className="lb">较独立组{costComparison(valueOf(costMetric, 'independent'))}</div>
        </div>
        <div className="cmp-kpis">
          {(b33 ? B33_KPIS : KPIS).map(item => (
            <div key={item.metric}>
              <b className="n agent">{kpi(item.metric)}</b>
              <span className="lb">{item.label}</span>
            </div>
          ))}
        </div>
        {notes.length > 0 && <span className="dm">数据性质：{notes.join(' / ')}</span>}
      </section>

      <div className="cmp-right">
        <section className="panel cmp-table">
          <div className="sec">
            <div className="sec-title">分项对比
              <span className="dm cmp-legend">
                {GROUPS.map(group => <span key={group.key}><i className={'kn-dot ' + group.tone} />{group.label}</span>)}
              </span>
            </div>
          </div>
          <div className="cmp-rows">
            {rows.length === 0 && <p className="dm kn-empty">这一天没有三组对照指标。</p>}
            {rows.map(row => (
              <div className="cmp-row" key={row.label}>
                <span className="cmp-label">{row.label}</span>
                <div className="cmp-bars">
                  {row.bars.map(bar => (
                    <i key={bar.key} className={'cmp-bar ' + bar.tone} style={{ width: `${bar.width}%` }} title={bar.value === null ? '没有数据' : `${bar.label}：${row.format(bar.value)}`} />
                  ))}
                </div>
                <b className="n cmp-value">
                  {row.coordinated === null ? '—' : row.format(row.coordinated)}
                  {row.delta && <span className="cmp-delta">{row.delta}</span>}
                </b>
              </div>
            ))}
          </div>
        </section>

        <section className="panel cmp-curve">
          <div className="sec">
            <div className="sec-title">T1 负载曲线<span className="dm">只有实际运行曲线，另外两组的逐分钟曲线未导出</span></div>
          </div>
          <div className="cmp-curvebody">
            <LoadCurve points={transformerLoadPoints(day)} cursor={cursor} start={dayAtHour(day, 0)} end={dayEnd(day)} width={620} height={190} ariaLabel="实际运行的 T1 负载全天曲线" />
          </div>
        </section>

        <div className="cmp-grid">
          <section className="panel cmp-block">
            <div className="sec">
              <div className="sec-title">知识侧</div>
            </div>
            <div className="cmp-blockbody">
              {!knowledgeAny && (
                <p className="dm cmp-empty">当前快照暂未包含知识侧评测指标。</p>
              )}
              {knowledgeAny && (
                <div className="cmp-metrics">
                  {knowledgeRows.map(row => (
                    <div className="cmp-metric" key={row.metric}>
                      <span>{row.label}</span>
                      <b className="n">{knowledgeValue(row)}</b>
                      {row.point?.note && <small>{row.point.note}</small>}
                    </div>
                  ))}
                </div>
              )}
              {qaGap !== null && (
                <p className="dm cmp-note">跨文档问答较普通 RAG 高 {number(qaGap)} 个百分点；激活率只作辅助指标，不换算成收益。</p>
              )}
              {knowledgeAny && qaGap === null && (
                <p className="dm cmp-note">问答对照需要同时有 doc_qa_accuracy 与 doc_qa_accuracy_rag 两条记录。</p>
              )}
            </div>
          </section>

          <section className="panel cmp-block">
            <div className="sec">
              <div className="sec-title">编排对照<span className="dm">10~20 个未见事件</span></div>
            </div>
            <div className="cmp-blockbody">
              {!orchAny && (
                <p className="dm cmp-empty">当前快照暂未包含编排对照指标。</p>
              )}
              {orchAny && (
                <table className="cmp-xtable">
                  <thead>
                    <tr><th>指标</th>{ORCH_ARMS.map(arm => <th key={arm.group}>{arm.label}</th>)}</tr>
                  </thead>
                  <tbody>
                    {orchRows.map(row => (
                      <tr key={row.metric}>
                        <td>{row.label}</td>
                        {row.values.map((point, index) => (
                          <td className="n" key={ORCH_ARMS[index].group}>
                            {point && point.value !== null ? formatKind(point.value, row.kind, point.unit) : '—'}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p className="dm cmp-note">
                {orchCases != null ? `样本 ${number(orchCases)} 起未见事件；` : ''}
                快慢双通道以适度研判耗时换取更高的处理正确率与证据完整率，两项指标并列呈现。
              </p>
            </div>
          </section>

          <section className="panel cmp-block">
            <div className="sec">
              <div className="sec-title">Harness 工程指标<span className="dm">改提示词/工具后重跑</span></div>
            </div>
            <div className="cmp-blockbody">
              {!harnessRows.length && (
                <p className="dm cmp-empty">当前快照暂未包含行为回归集指标。</p>
              )}
              {harnessRows.length > 0 && (
                <table className="cmp-xtable">
                  <thead>
                    <tr><th>版本</th>{HARNESS_ROWS.map(row => <th key={row.metric}>{row.label}</th>)}</tr>
                  </thead>
                  <tbody>
                    {harnessRows.map(row => (
                      <tr key={row.label}>
                        <td>{row.label}</td>
                        {row.values.map((point, index) => (
                          <td className="n" key={HARNESS_ROWS[index].metric}>
                            {point && point.value !== null
                              ? formatKind(point.value, HARNESS_ROWS[index].kind, point.unit)
                              : '—'}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {passTrend.length > 1 && (
                <div className="cmp-trend">
                  <MiniTrend points={passTrend} ariaLabel="回归集通过率随版本变化"
                    format={value => ratioPercent(value, passUnit)} />
                </div>
              )}
              {harnessRows.length > 0 && singleHarness && (
                <p className="dm cmp-note">只有一次重跑结果，按单次显示；累计多版本回归记录后自动生成趋势曲线。</p>
              )}
            </div>
          </section>

          <section className="panel cmp-block">
            <div className="sec">
              <div className="sec-title">越学越省<span className="dm">按资料批次</span></div>
            </div>
            <div className="cmp-blockbody">
              {!learnAny && (
                <p className="dm cmp-empty">当前快照暂未包含资料批次效率指标。</p>
              )}
              {learnAny && (
                <div className="cmp-trends">
                  {shareSeries.length > 0 && (
                    <div>
                      <span className="lb">Agent 段占比</span>
                      <div className="cmp-trend">
                        <MiniTrend points={shareSeries} ariaLabel="Agent 段占比随资料批次变化"
                          format={value => ratioPercent(value, shareUnit)} />
                      </div>
                    </div>
                  )}
                  {reviewSeries.length > 0 && (
                    <div>
                      <span className="lb">每百条复核分钟</span>
                      <div className="cmp-trend">
                        <MiniTrend points={reviewSeries} ariaLabel="每百条复核分钟随资料批次变化"
                          format={value => `${number(value)} min`} />
                      </div>
                    </div>
                  )}
                </div>
              )}
              {learnAny && (
                <p className="dm cmp-note">
                  {learnSingle
                    ? '目前只有一个批次，先按单点显示；出现第二个批次才连线看趋势。'
                    : '批次按资料入库顺序排列；复核分钟下降来自规则沉淀，不把激活率当成收益。'}
                </p>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}
