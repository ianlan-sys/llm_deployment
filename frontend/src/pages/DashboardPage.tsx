import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import type { DashboardResponse } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { StatusBadge } from '../components/StatusBadge'

const entryCards = [
  { to: '/calls', title: '坐席辅助', description: '基于场景与话术模板，为坐席提供实时建议。', icon: '☎' },
  { to: '/knowledge', title: '运维知识库', description: '在已纳管资料中检索，并保留可追溯引用。', icon: '◈' },
  { to: '/reviews', title: '文档审核', description: '提交长文档审核任务并跟踪审核进度。', icon: '▤' },
]

function metricValue(value: number, suffix = '') {
  return <><strong>{value}</strong>{suffix && <span>{suffix}</span>}</>
}

export function DashboardPage() {
  const queryClient = useQueryClient()
  const dashboardQuery = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get<DashboardResponse>('/api/dashboard'),
  })

  if (dashboardQuery.isPending) return <section className="page-state" aria-live="polite">正在加载工作总览…</section>
  if (dashboardQuery.isError) return <ErrorPanel detail={dashboardQuery.error.message} onRetry={() => void dashboardQuery.refetch()} />

  const dashboard = dashboardQuery.data
  const completedReviews = dashboard.review_counts.completed ?? 0
  const realMode = dashboard.mode === 'real'

  return (
    <section className="dashboard-page" aria-labelledby="page-title">
      <header className="page-header">
        <div>
          <p className="eyebrow">运行态势</p>
          <h1 id="page-title">工作总览</h1>
          <p>汇总本地服务、业务处理量和待办队列。</p>
        </div>
        <div className="page-actions">
          <StatusBadge tone={dashboard.service === 'ok' ? 'success' : 'danger'}>{dashboard.service === 'ok' ? '服务正常' : '服务异常'}</StatusBadge>
          <StatusBadge tone={realMode ? 'warning' : 'neutral'}>{realMode ? '真实模型' : '演示数据'}</StatusBadge>
          <button type="button" className="secondary-button" onClick={() => void queryClient.invalidateQueries({ queryKey: ['dashboard'] })}>刷新数据</button>
        </div>
      </header>

      <section className="metric-grid" aria-label="运行指标">
        <article className="metric-card"><p>今日请求</p><div>{metricValue(dashboard.today_requests, '次')}</div></article>
        <article className="metric-card"><p>平均响应耗时</p><div>{metricValue(dashboard.average_latency_ms, 'ms')}</div></article>
        <article className="metric-card"><p>引用覆盖率</p><div>{metricValue(dashboard.citation_coverage, '%')}</div></article>
        <article className="metric-card"><p>已完成审核</p><div>{metricValue(completedReviews, '项')}</div></article>
      </section>

      <section className="dashboard-section" aria-labelledby="queue-title">
        <div className="section-heading"><h2 id="queue-title">处理队列</h2><p>当前待处理任务数量</p></div>
        <div className="queue-grid">
          <article className="queue-card"><span>实时请求队列</span><strong>{dashboard.queues.realtime ?? 0}</strong><small>坐席建议与知识查询</small></article>
          <article className="queue-card"><span>长文档审核队列</span><strong>{dashboard.queues.long_document ?? 0}</strong><small>解析、规则匹配与审核任务</small></article>
        </div>
      </section>

      <section className="dashboard-section" aria-labelledby="entry-title">
        <div className="section-heading"><h2 id="entry-title">业务入口</h2><p>进入具体业务工作台</p></div>
        <div className="entry-grid">
          {entryCards.map((card) => (
            <Link className="entry-card" key={card.to} to={card.to} aria-label={card.title}>
              <span className="entry-icon" aria-hidden="true">{card.icon}</span>
              <span><strong>{card.title}</strong><small>{card.description}</small></span>
              <b aria-hidden="true">→</b>
            </Link>
          ))}
        </div>
      </section>
    </section>
  )
}
