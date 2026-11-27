/**
 * 负载曲线：从 .design/v2/iso.js 的 curve 移植。
 * 已发生段实线，预测段虚线，容量上限画虚线，当前时刻画竖线与圆点。
 */
export interface LoadPoint {
  /** 时刻（毫秒） */
  at: number
  /** 负载率，单位 % */
  value: number
}

export function LoadCurve({ points, cursor, start, end, limit = 100, alarm, height = 170, width = 430, ariaLabel, className }: {
  points: LoadPoint[]
  cursor: number
  start: number
  end: number
  limit?: number
  /** 告警线（容量的百分比），有配置时画一条更淡的虚线 */
  alarm?: number
  height?: number
  width?: number
  ariaLabel?: string
  className?: string
}) {
  const w = width
  const h = height
  const padL = 34
  const padB = 22
  const low = 40
  const high = 108
  const span = Math.max(1, end - start)
  const X = (at: number) => padL + ((at - start) / span) * (w - padL - 10)
  const Y = (value: number) => h - padB - ((Math.min(high, Math.max(low, value)) - low) * (h - padB - 8)) / (high - low)
  const sorted = [...points].sort((a, b) => a.at - b.at)
  const past = sorted.filter(point => point.at <= cursor)
  const future = sorted.filter(point => point.at >= cursor)
  const toPath = (list: LoadPoint[]) => list.map((point, index) =>
    `${index ? 'L' : 'M'}${X(point.at).toFixed(1)} ${Y(point.value).toFixed(1)}`).join('')
  const band = sorted.length
    ? `${toPath(sorted)}L${X(sorted[sorted.length - 1].at).toFixed(1)} ${h - padB}L${X(sorted[0].at).toFixed(1)} ${h - padB}Z`
    : ''
  const peak = past.reduce<LoadPoint | null>((best, point) => (!best || point.value > best.value ? point : best), null)
  const limitY = Y(limit)
  const ticks = [0, 0.5, 1].map(ratio => Math.round(start + span * ratio))

  return (
    <svg className={className} viewBox={`0 0 ${w} ${h}`} width="100%" height="100%" preserveAspectRatio="xMidYMid meet"
      role="img" aria-label={ariaLabel ?? `峰值负载曲线，容量上限 ${limit}%`}>
      {band && <path d={band} fill="#1A2129" />}
      <line x1={padL} y1={limitY} x2={w - 10} y2={limitY} stroke="var(--accent)" strokeDasharray="5 4" />
      <text x={w - 10} y={limitY - 5} fontSize="12" fill="var(--accent)" textAnchor="end">容量上限</text>
      {alarm !== undefined && (
        <>
          <line x1={padL} y1={Y(alarm)} x2={w - 10} y2={Y(alarm)} stroke="var(--line-hover)" strokeDasharray="4 4" />
          <text x={w - 10} y={Y(alarm) + 13} fontSize="12" fill="var(--text-3)" textAnchor="end">告警线</text>
        </>
      )}
      {future.length > 1 && <path d={toPath(future)} fill="none" stroke="var(--text)" strokeWidth="1.5" strokeDasharray="3 4" />}
      {past.length > 1 && <path d={toPath(past)} fill="none" stroke="var(--text)" strokeWidth="2" />}
      <line x1={X(cursor)} y1="8" x2={X(cursor)} y2={h - padB} stroke="var(--text)" />
      {peak && <circle cx={X(peak.at)} cy={Y(peak.value)} r="4" fill="var(--accent)" />}
      <text x={padL} y={h - 6} fontSize="12" fill="var(--text-3)">00:00</text>
      <text x={X(ticks[1])} y={h - 6} fontSize="12" fill="var(--text-3)" textAnchor="middle">12:00</text>
      <text x={w - 10} y={h - 6} fontSize="12" fill="var(--text-3)" textAnchor="end">24:00</text>
      <text x="4" y={limitY + 4} fontSize="12" fill="var(--text-3)">{limit}</text>
      <text x="4" y={Y(60) + 4} fontSize="12" fill="var(--text-3)">60</text>
    </svg>
  )
}
