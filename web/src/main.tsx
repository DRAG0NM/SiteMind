import React from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { ErrorBoundary } from './shared/day/ErrorBoundary'

// 工作台是演示用：默认播放动效（演示机常关着系统动画），?motion=reduce 时真正减弱，与展示页口径一致。
document.documentElement.dataset.motion = new URLSearchParams(location.search).get('motion') === 'reduce' ? 'reduce' : 'full'

createRoot(document.getElementById('root')!).render(<React.StrictMode><ErrorBoundary title="工作台渲染出错"><App/></ErrorBoundary></React.StrictMode>)
