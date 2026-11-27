import React from 'react'
import { createRoot } from 'react-dom/client'
import { ErrorBoundary } from '../shared/day/ErrorBoundary'
import { App } from './App'

// 展示页默认始终播放动效（演示机常关着系统动画效果）；?motion=reduce 时真正减弱，不看系统设置。
document.documentElement.dataset.motion = new URLSearchParams(location.search).get('motion') === 'reduce' ? 'reduce' : 'full'

createRoot(document.getElementById('root')!).render(<React.StrictMode><ErrorBoundary title="回放渲染出错"><App /></ErrorBoundary></React.StrictMode>)
