import { Component, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
  title?: string
  /** 变化时自动复位错误卡片：展示页按“标签页 + run_id”，控制台按“当前屏 + run_id”。 */
  resetKey?: string
}

/** 最小 error boundary：把漏网的渲染期异常（如坏时间/时区导致的 RangeError）变成可读错误卡片。 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, { message: string }> {
  state = { message: '' }

  static getDerivedStateFromError(reason: unknown) {
    return { message: reason instanceof Error ? reason.message : String(reason) }
  }

  componentDidUpdate(prevProps: ErrorBoundaryProps) {
    // 切换标签页或换一天后旧的错误卡片不该一直挡着页面，直接复位让孩子重新渲染。
    if (this.state.message && prevProps.resetKey !== this.props.resetKey) this.setState({ message: '' })
  }

  render() {
    if (this.state.message) {
      return (
        <div className="boot">
          <p className="booterror" role="alert">{this.props.title ?? '页面渲染出错'}：{this.state.message}</p>
        </div>
      )
    }
    return this.props.children
  }
}
