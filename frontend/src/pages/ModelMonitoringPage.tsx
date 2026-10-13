import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type {
  FeatureKey,
  MonitoringHistoryResponse,
  MonitoringLiveResponse,
  MonitoringRequestPoint,
} from '../api/types'
import { MetricLineChart, type MetricChartPoint } from '../components/MetricLineChart'
import { StatusBadge } from '../components/StatusBadge'

const featureLabels: Record<FeatureKey, string> = {
  call_assistant: '坐席辅助',
  knowledge: '运维知识库',
  document_review: '文档审核',
}

function formatBytes(value: number) {
  return `${(value / 1024 ** 3).toFixed(2)} GiB`
}

function formatPercent(count: number, total: number) {
  return total === 0 ? '暂无数据' : `${(count / total * 100).toFixed(1)}%（${count}）`
}

function formatMetric(value: number | null, suffix = ' ms') {
  return value === null ? '暂无数据' : `${Math.round(value)}${suffix}`
}

function requestTrend(points: MonitoringRequestPoint[]): MetricChartPoint[] {
  const totals = new Map<string, number>()
  for (const point of points) {
    totals.set(point.created_at, (totals.get(point.created_at) ?? 0) + point.request_count)
  }
  return [...totals.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([createdAt, value]) => ({
      label: new Intl.DateTimeFormat('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(new Date(createdAt)),
      value,
    }))
}

function sumRequests(
  points: MonitoringRequestPoint[],
  predicate: (point: MonitoringRequestPoint) => boolean,
) {
  return points.reduce(
    (total, point) => total + (predicate(point) ? point.request_count : 0),
    0,
  )
}

function ModeBadge({ mode }: { mode: 'mock' | 'local' }) {
  return (
    <StatusBadge tone={mode === 'local' ? 'warning' : 'neutral'}>
      {mode === 'local' ? 'Local' : 'Mock'}
    </StatusBadge>
  )
}

function LiveStatus({ data }: { data: MonitoringLiveResponse }) {
  const version = data.ollama.version
    ? `Ollama ${data.ollama.version}`
    : 'Ollama 版本暂无数据'
  const size = data.ollama.size_bytes === null
    ? '模型大小 暂无数据'
    : `模型大小 ${formatBytes(data.ollama.size_bytes)}`
  const installedLabel = !data.ollama.available
    ? '下载状态暂无数据'
    : data.ollama.installed ? '模型已下载' : '模型未下载'
  const loadedLabel = !data.ollama.available
    ? '加载状态暂无数据'
    : data.ollama.loaded ? '模型已加载' : '模型未加载'

  return (
    <section className="monitoring-section" aria-labelledby="live-status-title">
      <div className="section-heading">
        <h2 id="live-status-title">实时状态</h2>
        <p>状态每 10 秒刷新；页面仅展示经过后端白名单处理的运行信息。</p>
      </div>
      {data.sampling_status === 'sampling_error' && (
        <p className="monitoring-alert" role="alert">
          资源采集失败，业务功能不受影响
        </p>
      )}
      <div className="monitoring-live-grid">
        <article className="monitoring-card monitoring-model-card">
          <div className="monitoring-card-heading">
            <div>
              <p>本地模型</p>
              <h3>{data.ollama.display_label}</h3>
            </div>
            <StatusBadge tone={data.ollama.available ? 'success' : 'danger'}>
              {data.ollama.available ? 'Ollama 在线' : 'Ollama 离线'}
            </StatusBadge>
          </div>
          <p>{version}</p>
          <p>{size}</p>
          <div className="monitoring-inline-status">
            <StatusBadge tone={!data.ollama.available ? 'neutral' : data.ollama.installed ? 'success' : 'warning'}>
              {installedLabel}
            </StatusBadge>
            <StatusBadge tone={data.ollama.available && data.ollama.loaded ? 'success' : 'neutral'}>
              {loadedLabel}
            </StatusBadge>
          </div>
          <p className="monitoring-location">运行位置：Docker 内网服务</p>
        </article>

        <article className="monitoring-card">
          <p>推理队列</p>
          <h3>运行中 {data.inference.running} / 排队 {data.inference.queued}</h3>
          <p>并发 {data.inference.concurrency} / 队列上限 {data.inference.max_queue}</p>
        </article>

        <article className="monitoring-card">
          <p>功能模式</p>
          <ul className="monitoring-mode-list">
            {data.features.map((snapshot) => (
              <li key={snapshot.feature}>
                <span>{featureLabels[snapshot.feature]}</span>
                <ModeBadge mode={snapshot.mode} />
              </li>
            ))}
          </ul>
          {data.features.length === 0 && <p>暂无数据</p>}
        </article>
      </div>
    </section>
  )
}

function HistoryStatus({ data }: { data: MonitoringHistoryResponse }) {
  const points = data.requests
  const bucketTotal = sumRequests(points, () => true)
  const localCount = sumRequests(points, (point) => point.mode === 'local')
  const mockCount = sumRequests(points, (point) => point.mode === 'mock')
  const results = {
    success: sumRequests(points, (point) => point.result_status === 'success'),
    failed: sumRequests(points, (point) => ['error', 'shutdown'].includes(point.result_status)),
    timeout: sumRequests(points, (point) => point.result_status === 'timeout'),
    cancelled: sumRequests(points, (point) => point.result_status === 'cancelled'),
    queueFull: sumRequests(points, (point) => point.result_status === 'queue_full'),
  }
  const latestResource = data.resources.at(-1)
  const hasRequests = data.total_requests > 0

  return (
    <>
      <section className="monitoring-section" aria-labelledby="resource-title">
        <div className="section-heading">
          <h2 id="resource-title">服务器整体资源</h2>
          <p>资源百分比来自服务器采样，不代表单个模型的独占用量。</p>
        </div>
        {latestResource ? (
          <div className="monitoring-resource-grid">
            <strong>CPU {latestResource.cpu_percent}%</strong>
            <strong>内存 {latestResource.memory_percent}%</strong>
            <strong>磁盘 {latestResource.disk_percent}%</strong>
          </div>
        ) : (
          <p className="monitoring-empty">暂无数据</p>
        )}
      </section>

      <section className="monitoring-section" aria-labelledby="history-title">
        <div className="section-heading">
          <h2 id="history-title">最近 24 小时指标</h2>
          <p>历史指标每 60 秒刷新，统计结果来自请求记录与资源采样。</p>
        </div>
        <div className="monitoring-summary-grid">
          <article className="monitoring-card"><p>请求量</p><h3>总请求 {data.total_requests}</h3></article>
          <article className="monitoring-card"><p>平均耗时</p><h3>{hasRequests ? formatMetric(data.average_elapsed_ms) : '暂无数据'}</h3></article>
          <article className="monitoring-card"><p>尾部延迟</p><h3>P95 {hasRequests ? formatMetric(data.p95_elapsed_ms) : '暂无数据'}</h3></article>
          <article className="monitoring-card"><p>首字耗时</p><h3>{hasRequests ? formatMetric(data.average_ttft_ms) : '暂无数据'}</h3></article>
        </div>

        <div className="monitoring-history-grid">
          <MetricLineChart
            title="最近 24 小时请求趋势"
            valueLabel="请求数"
            points={requestTrend(points)}
          />
          <article className="monitoring-card monitoring-stat-list">
            <h3>模型调用占比</h3>
            {bucketTotal === 0 ? (
              <p>最近 24 小时暂无请求记录</p>
            ) : (
              <dl>
                <div><dt>Local</dt><dd>Local {formatPercent(localCount, bucketTotal)}</dd></div>
                <div><dt>Mock</dt><dd>Mock {formatPercent(mockCount, bucketTotal)}</dd></div>
              </dl>
            )}
          </article>
        </div>

        <div className="monitoring-detail-grid">
          <article className="monitoring-card monitoring-stat-list">
            <h3>功能请求分布</h3>
            {bucketTotal === 0 ? <p>暂无数据</p> : (
              <dl>
                {(Object.entries(featureLabels) as [FeatureKey, string][]).map(([feature, label]) => (
                  <div key={feature}>
                    <dt>{label}</dt>
                    <dd>{sumRequests(points, (point) => point.feature === feature)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </article>
          <article className="monitoring-card monitoring-stat-list">
            <h3>结果分布</h3>
            {bucketTotal === 0 ? <p>暂无数据</p> : (
              <ul>
                <li>成功 {results.success}</li>
                <li>失败 {results.failed}</li>
                <li>超时 {results.timeout}</li>
                <li>取消 {results.cancelled}</li>
                <li>429 {results.queueFull}</li>
              </ul>
            )}
          </article>
          <article className="monitoring-card monitoring-stat-list">
            <h3>队列与 Token</h3>
            <dl>
              <div><dt>平均排队</dt><dd>{hasRequests ? formatMetric(data.average_queue_wait_ms) : '暂无数据'}</dd></div>
              <div><dt>最大队列深度</dt><dd>{data.resources.length > 0 ? data.max_queue_depth : '暂无数据'}</dd></div>
              <div>
                <dt>Token 合计</dt>
                <dd>
                  {data.input_tokens === null && data.output_tokens === null
                    ? '暂无数据'
                    : `输入 ${data.input_tokens ?? '暂无数据'} / 输出 ${data.output_tokens ?? '暂无数据'}`}
                </dd>
              </div>
            </dl>
          </article>
        </div>
      </section>
    </>
  )
}

export function ModelMonitoringPage() {
  const liveQuery = useQuery({
    queryKey: ['monitoring-live'],
    queryFn: ({ signal }) => api.get<MonitoringLiveResponse>(
      '/api/monitoring/live',
      { signal },
    ),
    refetchInterval: 10_000,
  })
  const historyQuery = useQuery({
    queryKey: ['monitoring-history', 24],
    queryFn: ({ signal }) => api.get<MonitoringHistoryResponse>(
      '/api/monitoring/history?hours=24',
      { signal },
    ),
    refetchInterval: 60_000,
  })
  const failures = [
    liveQuery.isError ? '实时状态获取失败' : null,
    historyQuery.isError ? '历史指标获取失败' : null,
  ].filter(Boolean)

  return (
    <section className="monitoring-page" aria-labelledby="page-title">
      <header className="page-header">
        <div>
          <p className="eyebrow">模型运行观测</p>
          <h1 id="page-title">本地模型监控</h1>
          <p>查看本地模型、推理队列与最近 24 小时的真实采集指标。</p>
        </div>
      </header>

      {failures.length > 0 && (
        <section className="error-panel monitoring-fetch-error" role="alert">
          <h2>监控数据获取失败</h2>
          <p>{failures.join('；')}</p>
          <button
            type="button"
            onClick={() => {
              if (liveQuery.isError) void liveQuery.refetch()
              if (historyQuery.isError) void historyQuery.refetch()
            }}
          >
            重试
          </button>
        </section>
      )}
      {liveQuery.isPending && historyQuery.isPending && (
        <p className="page-state" aria-live="polite">正在加载监控数据…</p>
      )}
      {liveQuery.data && !liveQuery.isError && <LiveStatus data={liveQuery.data} />}
      {historyQuery.data && !historyQuery.isError && <HistoryStatus data={historyQuery.data} />}
    </section>
  )
}
