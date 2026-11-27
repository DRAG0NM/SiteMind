import { useEffect, useRef, useState } from 'react'
import { prefersReducedMotion } from './motion'

/** 纯数字文本（“96”“0.72”）换值时滚动过去，保留原来的小数位；“—”这类非数字原样显示。 */
export function TweenNumber({ value, ms = 600 }: { value: string; ms?: number }) {
  const match = /^-?\d+(\.(\d+))?$/.exec(value.trim())
  const target = match ? Number(value) : NaN
  const decimals = match?.[2]?.length ?? 0
  const [shown, setShown] = useState(target)
  const shownRef = useRef(target)

  useEffect(() => {
    if (!Number.isFinite(target)) return
    const from = Number.isFinite(shownRef.current) ? shownRef.current : target
    const reduced = prefersReducedMotion()
    if (from === target || reduced) { shownRef.current = target; setShown(target); return }
    const started = performance.now()
    let raf = 0
    const step = (now: number) => {
      const t = Math.min(1, (now - started) / ms)
      const next = from + (target - from) * (1 - Math.pow(1 - t, 3))
      shownRef.current = next
      setShown(next)
      if (t < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, ms])

  return <>{Number.isFinite(target) && Number.isFinite(shown) ? shown.toFixed(decimals) : value}</>
}
