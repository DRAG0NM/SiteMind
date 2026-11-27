/**
 * 是否减弱动效。展示页是演示用，默认不跟系统“减弱动效/关闭动画效果”走（html 上 data-motion="full"），
 * 地址加 ?motion=reduce 时设 data-motion="reduce" 真正减弱；控制台没有这个标记，始终跟系统设置。
 */
export function prefersReducedMotion() {
  if (typeof window === 'undefined') return false
  if (document.documentElement.dataset.motion === 'reduce') return true
  if (document.documentElement.dataset.motion === 'full') return false
  return Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
}
