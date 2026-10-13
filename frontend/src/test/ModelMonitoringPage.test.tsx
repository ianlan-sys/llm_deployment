import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { ModelMonitoringPage } from '../pages/ModelMonitoringPage'

const liveFixture = {
  sampling_status: 'ok' as const,
  ollama: {
    available: true,
    installed: true,
    loaded: true,
    display_label: 'Qwen3 1.7B Q4',
    version: '0.32.1',
    size_bytes: 1_400_000_000,
  },
  inference: { running: 1, queued: 2, concurrency: 1, max_queue: 3 },
  features: [
    { feature: 'call_assistant' as const, mode: 'local' as const },
    { feature: 'knowledge' as const, mode: 'local' as const },
    { feature: 'document_review' as const, mode: 'mock' as const },
  ],
}

const historyFixture = {
  hours: 24,
  total_requests: 12,
  average_elapsed_ms: 4100,
  p95_elapsed_ms: 8300,
  average_ttft_ms: 780,
  average_queue_wait_ms: 120,
  max_queue_depth: 2,
  input_tokens: 320,
  output_tokens: 180,
  requests: [
    {
      created_at: '2026-07-24T08:00:00Z',
      feature: 'call_assistant' as const,
      mode: 'local' as const,
      result_status: 'success',
      request_count: 5,
      average_elapsed_ms: 3000,
      p95_elapsed_ms: 4000,
      average_ttft_ms: 600,
      average_queue_wait_ms: 80,
      input_tokens: 200,
      output_tokens: 100,
    },
    {
      created_at: '2026-07-24T08:05:00Z',
      feature: 'knowledge' as const,
      mode: 'local' as const,
      result_status: 'timeout',
      request_count: 2,
      average_elapsed_ms: 8000,
      p95_elapsed_ms: 8300,
      average_ttft_ms: null,
      average_queue_wait_ms: 250,
      input_tokens: 120,
      output_tokens: 80,
    },
    {
      created_at: '2026-07-24T08:05:00Z',
      feature: 'document_review' as const,
      mode: 'mock' as const,
      result_status: 'error',
      request_count: 5,
      average_elapsed_ms: 1500,
      p95_elapsed_ms: 2000,
      average_ttft_ms: null,
      average_queue_wait_ms: 40,
      input_tokens: null,
      output_tokens: null,
    },
  ],
  resources: [{
    created_at: '2026-07-24T08:05:00Z',
    cpu_percent: 41.2,
    memory_percent: 63.5,
    disk_percent: 72.1,
    ollama_available: true,
    model_loaded: true,
    running_count: 1,
    queued_count: 2,
  }],
}

function jsonResponse(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status }))
}

function mockMonitoring(
  live: unknown = liveFixture,
  history: unknown = historyFixture,
) {
  const fetchMock = vi.fn((path: string) => {
    if (path === '/api/monitoring/live') return jsonResponse(live)
    if (path === '/api/monitoring/history?hours=24') return jsonResponse(history)
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <ModelMonitoringPage />
      </QueryClientProvider>,
    ),
  }
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('shows actual Ollama, queue, resource, and 24-hour request state', async () => {
  mockMonitoring()

  renderPage()

  expect(await screen.findByText('Qwen3 1.7B Q4')).toBeVisible()
  expect(screen.getByText('Ollama 0.32.1')).toBeVisible()
  expect(screen.getByText('模型大小 1.30 GiB')).toBeVisible()
  expect(screen.getByText('运行中 1 / 排队 2')).toBeVisible()
  expect(screen.getByText('并发 1 / 队列上限 3')).toBeVisible()
  expect(screen.getByRole('heading', { name: '服务器整体资源' })).toBeVisible()
  expect(screen.getByText('CPU 41.2%')).toBeVisible()
  expect(screen.getByText('内存 63.5%')).toBeVisible()
  expect(screen.getByText('磁盘 72.1%')).toBeVisible()
  expect(screen.getByRole('img', { name: '最近 24 小时请求趋势' })).toBeVisible()
  expect(screen.getByRole('table', { name: '最近 24 小时请求趋势数据' })).toBeInTheDocument()
  expect(screen.getByText('P95 8300 ms')).toBeVisible()
  expect(screen.getByText('Local 58.3%（7）')).toBeVisible()
  expect(screen.getByText('Mock 41.7%（5）')).toBeVisible()
  expect(screen.getByText('成功 5')).toBeVisible()
  expect(screen.getByText('失败 5')).toBeVisible()
  expect(screen.getByText('超时 2')).toBeVisible()
  expect(screen.getByText('取消 0')).toBeVisible()
  expect(screen.getByText('429 0')).toBeVisible()
  expect(screen.getByText('输入 320 / 输出 180')).toBeVisible()
})

it('does not invent runtime or history values when collections are empty', async () => {
  mockMonitoring({
    ...liveFixture,
    sampling_status: 'sampling_error',
    ollama: {
      available: false,
      installed: false,
      loaded: false,
      display_label: 'Qwen3 1.7B Q4',
      version: null,
      size_bytes: null,
    },
  }, {
    ...historyFixture,
    total_requests: 0,
    average_elapsed_ms: 0,
    p95_elapsed_ms: 0,
    average_ttft_ms: null,
    average_queue_wait_ms: 0,
    max_queue_depth: 0,
    input_tokens: null,
    output_tokens: null,
    requests: [],
    resources: [],
  })

  renderPage()

  expect(await screen.findByText('资源采集失败，业务功能不受影响')).toBeVisible()
  expect(screen.getByText('下载状态暂无数据')).toBeVisible()
  expect(screen.getByText('加载状态暂无数据')).toBeVisible()
  expect(screen.getAllByText('暂无数据').length).toBeGreaterThan(0)
  expect(screen.getByText('最近 24 小时暂无请求记录')).toBeVisible()
  expect(screen.getByText('最近 24 小时暂无采集数据')).toBeVisible()
  expect(screen.getByText('平均耗时').closest('article')).toHaveTextContent('暂无数据')
  expect(screen.getByText('尾部延迟').closest('article')).toHaveTextContent('P95 暂无数据')
  expect(screen.getByText('平均排队').closest('div')).toHaveTextContent('暂无数据')
  expect(screen.queryByText('Ollama 0.0.0')).not.toBeInTheDocument()
  expect(screen.queryByText('CPU 0%')).not.toBeInTheDocument()
})

it('surfaces request failures without replacing them with made-up values', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/monitoring/live') return jsonResponse({ detail: 'nope' }, 503)
    if (path === '/api/monitoring/history?hours=24') return jsonResponse({ detail: 'nope' }, 503)
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))

  renderPage()

  expect(await screen.findByRole('alert')).toHaveTextContent('实时状态获取失败')
  expect(screen.getByRole('alert')).toHaveTextContent('历史指标获取失败')
  expect(screen.queryByText('Qwen3 1.7B Q4')).not.toBeInTheDocument()
  expect(screen.queryByText('总请求 0')).not.toBeInTheDocument()
})

it('polls live every 10 seconds and history every 60 seconds', async () => {
  vi.useFakeTimers()
  const fetchMock = mockMonitoring()

  renderPage()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(fetchMock).toHaveBeenCalledTimes(2)

  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000)
  })
  expect(fetchMock.mock.calls.filter(([path]) => path === '/api/monitoring/live')).toHaveLength(2)
  expect(fetchMock.mock.calls.filter(([path]) => path === '/api/monitoring/history?hours=24')).toHaveLength(1)

  await act(async () => {
    await vi.advanceTimersByTimeAsync(50_000)
  })
  expect(fetchMock.mock.calls.filter(([path]) => path === '/api/monitoring/live')).toHaveLength(7)
  expect(fetchMock.mock.calls.filter(([path]) => path === '/api/monitoring/history?hours=24')).toHaveLength(2)
})

it('cancels in-flight collections when the page unmounts', async () => {
  const observedSignals: AbortSignal[] = []
  vi.stubGlobal('fetch', vi.fn((_path: string, init?: RequestInit) => {
    if (init?.signal) observedSignals.push(init.signal)
    return new Promise<Response>(() => undefined)
  }))

  const view = renderPage()
  await waitFor(() => expect(observedSignals).toHaveLength(2))

  view.unmount()

  expect(observedSignals.every((signal) => signal.aborted)).toBe(true)
})

it('does not overlap a slow live poll with its next interval', async () => {
  vi.useFakeTimers()
  let resolveLive: ((response: Response) => void) | undefined
  const liveResponse = new Promise<Response>((resolve) => {
    resolveLive = resolve
  })
  const fetchMock = vi.fn((path: string) => {
    if (path === '/api/monitoring/live') return liveResponse
    if (path === '/api/monitoring/history?hours=24') return jsonResponse(historyFixture)
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  })
  vi.stubGlobal('fetch', fetchMock)

  renderPage()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000)
  })

  expect(fetchMock.mock.calls.filter(([path]) => path === '/api/monitoring/live')).toHaveLength(1)

  resolveLive?.(new Response(JSON.stringify(liveFixture), { status: 200 }))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000)
  })
  expect(fetchMock.mock.calls.filter(([path]) => path === '/api/monitoring/live')).toHaveLength(2)
})

it('hides cached panels after their background collections fail', async () => {
  const attempts = new Map<string, number>()
  const fetchMock = vi.fn((path: string) => {
    const attempt = (attempts.get(path) ?? 0) + 1
    attempts.set(path, attempt)
    if (attempt > 1) return jsonResponse({ detail: 'collection failed' }, 503)
    if (path === '/api/monitoring/live') return jsonResponse(liveFixture)
    if (path === '/api/monitoring/history?hours=24') return jsonResponse(historyFixture)
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  })
  vi.stubGlobal('fetch', fetchMock)

  const { queryClient } = renderPage()
  expect(await screen.findByText('Qwen3 1.7B Q4')).toBeVisible()
  expect(screen.getByRole('heading', { name: '服务器整体资源' })).toBeVisible()

  await act(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['monitoring-live'] }),
      queryClient.invalidateQueries({ queryKey: ['monitoring-history', 24] }),
    ])
  })

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('实时状态获取失败')
  expect(alert).toHaveTextContent('历史指标获取失败')
  expect(screen.queryByText('Qwen3 1.7B Q4')).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '服务器整体资源' })).not.toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '最近 24 小时指标' })).not.toBeInTheDocument()
})
