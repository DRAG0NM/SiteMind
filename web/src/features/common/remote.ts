/** 真实接口优先、失败退回演示数据：每个视图都用这一层，界面只按 live 决定标哪种数据性质。 */
import { useEffect, useState } from 'react'

export interface Remote<T> {
  data: T
  /** 数据来自真实接口；false 表示接口不通或没有数据，已退回演示替身 */
  live: boolean
  /** 退回原因，直接显示给人看；live 时为空串 */
  note: string
}

export async function remoteLoad<T>(load: () => Promise<T>, fallback: () => T): Promise<Remote<T>> {
  try {
    return { data: await load(), live: true, note: '' }
  } catch (reason) {
    const message = reason instanceof Error ? reason.message : String(reason)
    return { data: fallback(), live: false, note: '当前展示标准演示场景数据' }
  }
}

/** 挂载后读一次；deps 变化时重读。未读完前返回 null，组件先显示占位。 */
export function useRemote<T>(load: () => Promise<T>, fallback: () => T, deps: readonly unknown[]): Remote<T> | null {
  const [remote, setRemote] = useState<Remote<T> | null>(null)
  useEffect(() => {
    let active = true
    void remoteLoad(load, fallback).then(result => { if (active) setRemote(result) })
    return () => { active = false }
    // load/fallback 每次渲染都是新函数，按调用方给的 deps 重读
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return remote
}
