import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'

import { CallAssistantPage } from '../pages/CallAssistantPage'

const scenario = {
  id: 'fault-report', name: '故障报修', description: '登记故障并承诺响应时间。',
  category: 'incident', priority: 'high', contact_address: null, status: 'active',
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}><CallAssistantPage /></QueryClientProvider>)
}

function sseResponse() {
  const text = [
    'event: meta\ndata: {"request_id":"request-1","session_id":"session-1","mode":"demo","template_ids":["template-1"],"templates":[{"id":"template-1","title":"故障受理话术","content":"已审核故障受理模板"}],"started_at":"2026-07-20T08:00:00Z"}\n\n',
    'event: delta\ndata: {"content":"已为您登记故障，请保持电话畅通。"}\n\n',
    'event: done\ndata: {"request_id":"request-1","elapsed_ms":42}\n\n',
  ].join('')
  return new Response(text, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function abortableStream(signal: AbortSignal) {
  return new Promise<Response>((_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true }))
}

afterEach(() => vi.unstubAllGlobals())

it('accepts a streamed suggestion and enables summary', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/demo/call-scenarios') return Promise.resolve(new Response(JSON.stringify([scenario]), { status: 200 }))
    if (path === '/api/calls/suggestions/stream') return Promise.resolve(sseResponse())
    if (path === '/api/calls/summary') return Promise.resolve(new Response(JSON.stringify({ appeal: '故障报修', process: '登记', result: '待跟进', follow_up: '回访' }), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()

  renderPage()
  await user.selectOptions(await screen.findByLabelText('来电场景'), 'fault-report')
  await user.click(screen.getByRole('button', { name: '生成推荐话术' }))

  expect(await screen.findByText(/已为您登记故障/)).toBeInTheDocument()
  expect(screen.getByText('故障受理话术')).toBeInTheDocument()
  expect(screen.getByText('已审核故障受理模板')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: '采纳话术' }))
  expect(screen.getByRole('button', { name: '生成工单摘要' })).toBeEnabled()
})

it('persists adoption and sends scenario, original transcript, and edited reply for summary', async () => {
  const requests: Array<{ path: string; body: Record<string, unknown> }> = []
  vi.stubGlobal('fetch', vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/demo/call-scenarios') return Promise.resolve(new Response(JSON.stringify([scenario]), { status: 200 }))
    if (path === '/api/calls/suggestions/stream') return Promise.resolve(sseResponse())
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>
    requests.push({ path, body })
    if (path === '/api/calls/sessions/session-1/adoption') return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }))
    if (path === '/api/calls/summary') return Promise.resolve(new Response(JSON.stringify({ appeal: '故障报修', process: '登记', result: '待跟进', follow_up: '回访' }), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()

  renderPage()
  const transcript = await screen.findByLabelText('通话记录')
  await user.clear(transcript)
  await user.type(transcript, '客户反馈原始故障情况')
  await user.click(screen.getByRole('button', { name: '生成推荐话术' }))
  await screen.findByText(/已为您登记故障/)
  await user.click(screen.getByRole('button', { name: '采纳话术' }))
  const accepted = screen.getByLabelText('已采纳话术')
  await user.clear(accepted)
  await user.type(accepted, '人工编辑后的采纳回复')
  await user.click(screen.getByRole('button', { name: '生成工单摘要' }))

  expect(await screen.findByText('待跟进')).toBeInTheDocument()
  expect(requests).toEqual([
    {
      path: '/api/calls/sessions/session-1/adoption',
      body: { accepted_reply: '已为您登记故障，请保持电话畅通。' },
    },
    {
      path: '/api/calls/summary',
      body: {
        session_id: 'session-1',
        scenario_id: 'fault-report',
        original_transcript: '客户反馈原始故障情况',
        accepted_reply: '人工编辑后的采纳回复',
      },
    },
  ])
})

it('stops an in-flight stream with its AbortController', async () => {
  let streamSignal: AbortSignal | undefined
  vi.stubGlobal('fetch', vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/demo/call-scenarios') return Promise.resolve(new Response(JSON.stringify([scenario]), { status: 200 }))
    streamSignal = init?.signal as AbortSignal
    return abortableStream(streamSignal)
  }))
  const user = userEvent.setup()
  renderPage()

  await user.click(await screen.findByRole('button', { name: '生成推荐话术' }))
  await user.click(await screen.findByRole('button', { name: '停止生成' }))

  expect(streamSignal?.aborted).toBe(true)
  expect(await screen.findByText('已停止')).toBeInTheDocument()
})

it('aborts an in-flight stream when unmounted', async () => {
  let streamSignal: AbortSignal | undefined
  vi.stubGlobal('fetch', vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/demo/call-scenarios') return Promise.resolve(new Response(JSON.stringify([scenario]), { status: 200 }))
    streamSignal = init?.signal as AbortSignal
    return abortableStream(streamSignal)
  }))
  const user = userEvent.setup()
  const view = renderPage()

  await user.click(await screen.findByRole('button', { name: '生成推荐话术' }))
  view.unmount()

  expect(streamSignal?.aborted).toBe(true)
})

it('shows an SSE error event as a generation failure', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/demo/call-scenarios') return Promise.resolve(new Response(JSON.stringify([scenario]), { status: 200 }))
    return Promise.resolve(new Response('event: error\ndata: {"message":"模型服务暂不可用"}\n\n', { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
  }))
  const user = userEvent.setup()
  renderPage()

  await user.click(await screen.findByRole('button', { name: '生成推荐话术' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('模型服务暂不可用')
  expect(screen.getByText('生成失败')).toBeInTheDocument()
})
