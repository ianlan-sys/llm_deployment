import { render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { App } from '../app/App'
import { queryClient } from '../app/queryClient'
import { router } from '../app/router'

it.each([
  ['/', '工作总览'],
  ['/calls', '坐席辅助'],
  ['/knowledge', '运维知识库'],
  ['/reviews', '文档审核'],
  ['/monitoring', '本地模型监控'],
  ['/settings', '运行与设置'],
])('renders %s at %s and retains the advisory disclaimer', async (path, heading) => {
  vi.stubGlobal('fetch', vi.fn((request: string) => {
    if (request === '/api/auth/session') return Promise.resolve(new Response(JSON.stringify({ username: 'demo' }), { status: 200 }))
    if (request === '/api/dashboard') return Promise.resolve(new Response(JSON.stringify({ service: 'ok', mode: 'demo', today_requests: 0, average_latency_ms: 0, citation_coverage: 0, review_counts: {}, queues: {} }), { status: 200 }))
    if (request === '/api/monitoring/live') return Promise.resolve(new Response(JSON.stringify({
      sampling_status: 'idle',
      ollama: { available: false, installed: false, loaded: false, display_label: 'Qwen3 1.7B Q4', version: null, size_bytes: null },
      inference: { running: 0, queued: 0, concurrency: 1, max_queue: 3 },
      features: [],
    }), { status: 200 }))
    if (request === '/api/monitoring/history?hours=24') return Promise.resolve(new Response(JSON.stringify({
      hours: 24, total_requests: 0, average_elapsed_ms: 0, p95_elapsed_ms: 0, average_ttft_ms: null,
      average_queue_wait_ms: 0, max_queue_depth: 0, input_tokens: null, output_tokens: null, requests: [], resources: [],
    }), { status: 200 }))
    if (request === '/api/settings/provider') return Promise.resolve(new Response(JSON.stringify({ mode: 'demo', base_url: null, model: null }), { status: 200 }))
    return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
  }))
  queryClient.clear()
  await router.navigate(path)
  render(<App />)

  expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument()
  expect(screen.getByRole('complementary', { name: '使用提示' })).toHaveTextContent('不构成设备控制指令或正式审核结论')
})

it('shows the public login page instead of the protected layout when no session exists', async () => {
  vi.stubGlobal('fetch', vi.fn((request: string) => {
    if (request === '/api/auth/session') return Promise.resolve(new Response(JSON.stringify({ detail: '请先登录' }), { status: 401 }))
    return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
  }))
  queryClient.clear()
  await router.navigate('/knowledge')
  render(<App />)

  expect(await screen.findByRole('heading', { name: '演示系统登录' })).toBeVisible()
  expect(screen.queryByRole('complementary', { name: '使用提示' })).not.toBeInTheDocument()
})

afterEach(() => {
  queryClient.clear()
  vi.unstubAllGlobals()
})
