import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'

import { KnowledgePage } from '../pages/KnowledgePage'

const citation = {
  chunk_id: 'chunk-1',
  source_id: 'source-1',
  source_title: '核心节点 A 网络连通性手册',
  source_updated_at: '2026-07-20T08:00:00Z',
  score: 0.92,
  text: '核心节点 A 使用 VLAN 410。',
  location: '网络参数',
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}><KnowledgePage /></QueryClientProvider>)
}

function sseResponse(frames: Array<{ event: string; data: unknown }>) {
  const text = frames.map(({ event, data }) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')
  return new Response(text, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function answeredStream() {
  return sseResponse([
    { event: 'retrieval', data: { citations: [citation], retrieval_engine: 'numpy' } },
    { event: 'meta', data: { mode: 'local', model: 'qwen3:1.7b-q4_K_M' } },
    { event: 'delta', data: { content: '根据资料，' } },
    { event: 'delta', data: { content: '核心交换机使用 VLAN 410。' } },
    {
      event: 'done',
      data: {
        status: 'answered',
        disclaimer: '模型仅提供配置查询与操作参考，不会直接下发配置。',
        elapsed_ms: 1260,
      },
    },
  ])
}

afterEach(() => vi.unstubAllGlobals())

it('shows retrieval, server-owned local generation metadata, concatenated deltas, citations, and completion time', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/knowledge/query/stream') return Promise.resolve(answeredStream())
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()

  renderPage()
  await user.type(screen.getByLabelText('运维问题'), '核心交换机 VLAN 如何划分？')
  await user.click(screen.getByRole('button', { name: '查询' }))

  expect(await screen.findByText('根据资料，核心交换机使用 VLAN 410。')).toBeVisible()
  expect(screen.getByText('已命中资料')).toBeVisible()
  expect(screen.getByText('核心节点 A 网络连通性手册')).toBeVisible()
  expect(screen.getByText('本地模型')).toBeVisible()
  expect(screen.getByText('qwen3:1.7b-q4_K_M')).toBeVisible()
  expect(screen.getByText('总耗时 1260 ms')).toBeVisible()

  await user.click(screen.getByRole('button', { name: '来源 1' }))
  expect(screen.getByRole('dialog', { name: '资料出处' })).toHaveTextContent('核心节点 A 使用 VLAN 410。')
})

it('renders answer deltas before the terminal done frame arrives', async () => {
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined
  const encoder = new TextEncoder()
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(new ReadableStream<Uint8Array>({
    start(controller) { streamController = controller },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))))
  const user = userEvent.setup()
  renderPage()
  await user.type(screen.getByLabelText('运维问题'), '流式展示问题')
  await user.click(screen.getByRole('button', { name: '查询' }))
  await waitFor(() => expect(streamController).toBeDefined())

  streamController?.enqueue(encoder.encode([
    `event: retrieval\ndata: ${JSON.stringify({ citations: [citation], retrieval_engine: 'numpy' })}\n\n`,
    'event: meta\ndata: {"mode":"local","model":"qwen3:1.7b-q4_K_M"}\n\n',
    'event: delta\ndata: {"content":"首段流式回答"}\n\n',
  ].join('')))

  expect(await screen.findByText('首段流式回答')).toBeVisible()
  expect(screen.getByText('qwen3:1.7b-q4_K_M 正在本地生成')).toBeVisible()
  streamController?.enqueue(encoder.encode('event: done\ndata: {"status":"answered","disclaimer":"仅供参考","elapsed_ms":10}\n\n'))
  streamController?.close()
  expect(await screen.findByText('总耗时 10 ms')).toBeVisible()
})

it('exposes all ten approved recommended questions', () => {
  renderPage()
  expect(within(screen.getByLabelText('推荐问题')).getAllByRole('button')).toHaveLength(10)
})

it('opens a focus-contained source drawer with complete server-derived fields', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(answeredStream())))
  const user = userEvent.setup()

  renderPage()
  await user.type(screen.getByLabelText('运维问题'), '核心交换机 VLAN 如何划分？')
  await user.click(screen.getByRole('button', { name: '查询' }))
  const sourceButton = await screen.findByRole('button', { name: '来源 1' })
  await user.click(sourceButton)

  const dialog = screen.getByRole('dialog', { name: '资料出处' })
  const closeButton = screen.getByRole('button', { name: '关闭资料出处' })
  expect(dialog).toHaveTextContent('核心节点 A 网络连通性手册')
  expect(dialog).toHaveTextContent('2026')
  expect(dialog).toHaveTextContent('0.92')
  expect(dialog).toHaveTextContent('核心节点 A 使用 VLAN 410。')
  expect(closeButton).toHaveFocus()
  const page = document.querySelector<HTMLElement>('.knowledge-page')
  expect(page).toHaveAttribute('inert')
  await user.tab()
  expect(closeButton).toHaveFocus()
  await user.tab({ shift: true })
  expect(closeButton).toHaveFocus()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog', { name: '资料出处' })).not.toBeInTheDocument()
  expect(sourceButton).toHaveFocus()
  expect(page).not.toHaveAttribute('inert')
})

it('renders no-evidence without an answer card and retains the mandatory device-control disclaimer', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(sseResponse([
    { event: 'retrieval', data: { citations: [], retrieval_engine: 'numpy' } },
    { event: 'done', data: { status: 'no_evidence', answer: '知识库中暂无可靠依据，请调整关键词或资料范围。', elapsed_ms: 24 } },
  ]))))
  const user = userEvent.setup()

  renderPage()
  await user.type(screen.getByLabelText('运维问题'), '月球节点密码是什么？')
  await user.click(screen.getByRole('button', { name: '查询' }))

  expect(await screen.findByText('知识库中暂无可靠依据，请调整关键词或资料范围。')).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: '依据资料的回答' })).not.toBeInTheDocument()
  expect(screen.getByText('未命中可靠资料')).toBeVisible()
  const noEvidenceStages = screen.getByLabelText('知识问答阶段').children
  expect(noEvidenceStages[1]).not.toHaveClass('is-reached')
  expect(noEvidenceStages[2]).not.toHaveClass('is-reached')
  expect(noEvidenceStages[3]).toHaveClass('is-reached')
  expect(screen.getByText('总耗时 24 ms')).toBeVisible()
  expect(screen.getByLabelText('操作边界')).toHaveTextContent('不会直接下发到任何设备')
})

it('does not claim hit or generation stages when the request fails before any frame', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
    JSON.stringify({ detail: '登录状态已失效' }),
    { status: 401, headers: { 'Content-Type': 'application/json' } },
  ))))
  const user = userEvent.setup()
  renderPage()
  await user.type(screen.getByLabelText('运维问题'), '预帧失败')
  await user.click(screen.getByRole('button', { name: '查询' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('登录状态已失效')
  const failedStages = screen.getByLabelText('知识问答阶段').children
  expect(failedStages[1]).not.toHaveClass('is-reached')
  expect(failedStages[2]).not.toHaveClass('is-reached')
})

it('isolates a replacement query from the cancelled old stream', async () => {
  const controllers: Array<ReadableStreamDefaultController<Uint8Array>> = []
  const requestQuestions: string[] = []
  const encoder = new TextEncoder()
  vi.stubGlobal('fetch', vi.fn((_path: string, init?: RequestInit) => {
    requestQuestions.push((JSON.parse(String(init?.body)) as { question: string }).question)
    return Promise.resolve(new Response(new ReadableStream<Uint8Array>({
      start(controller) { controllers.push(controller) },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }))
  }))
  const user = userEvent.setup()
  renderPage()
  const question = screen.getByLabelText('运维问题')
  await user.type(question, '旧问题')
  await user.click(screen.getByRole('button', { name: '查询' }))
  await waitFor(() => expect(controllers).toHaveLength(1))
  controllers[0].enqueue(encoder.encode('event: error\ndata: {"status":"error","message":"旧流错误"}\n\n'))
  expect(await screen.findByRole('alert')).toHaveTextContent('旧流错误')
  await user.clear(question)
  await user.type(question, '新问题')
  await user.click(screen.getByRole('button', { name: '重试' }))
  await waitFor(() => expect(controllers).toHaveLength(2))
  controllers[1].enqueue(encoder.encode([
    `event: retrieval\ndata: ${JSON.stringify({ citations: [citation], retrieval_engine: 'numpy' })}\n\n`,
    'event: meta\ndata: {"mode":"mock","model":"new-model"}\n\n',
    'event: delta\ndata: {"content":"新流回答"}\n\n',
    'event: done\ndata: {"status":"answered","disclaimer":"仅供参考","elapsed_ms":9}\n\n',
  ].join('')))
  controllers[1].close()

  expect(await screen.findByText('新流回答')).toBeVisible()
  expect(screen.queryByText(/已停止本次查询|旧流错误/)).not.toBeInTheDocument()
  expect(screen.getByText('new-model')).toBeVisible()
  expect(requestQuestions).toEqual(['旧问题', '新问题'])
})

it('preserves the question and offers retry, settings, and feature-scoped Mock recovery on provider outage', async () => {
  let queryCount = 0
  const requests: Array<{ path: string; body: unknown }> = []
  vi.stubGlobal('fetch', vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/settings/features/knowledge') {
      requests.push({ path, body: JSON.parse(String(init?.body)) })
      return Promise.resolve(new Response(JSON.stringify({
        feature: 'knowledge', mode: 'mock', model: 'built-in-mock', available: true, detail: 'Mock 已启用',
      }), { status: 200 }))
    }
    queryCount += 1
    if (queryCount === 1) {
      return Promise.resolve(sseResponse([
        { event: 'retrieval', data: { citations: [citation], retrieval_engine: 'numpy' } },
        { event: 'meta', data: { mode: 'local', model: 'qwen3:1.7b-q4_K_M' } },
        { event: 'error', data: { status: 'error', message: '模型服务暂不可用，请重试或手动切换为 Mock 模式。' } },
      ]))
    }
    return Promise.resolve(sseResponse([
      { event: 'retrieval', data: { citations: [citation], retrieval_engine: 'numpy' } },
      { event: 'meta', data: { mode: 'mock', model: 'built-in-mock' } },
      { event: 'delta', data: { content: '根据资料，核心交换机使用 VLAN 410。' } },
      { event: 'done', data: { status: 'answered', disclaimer: '仅供参考', elapsed_ms: 8 } },
    ]))
  }))
  const user = userEvent.setup()
  renderPage()
  const question = screen.getByLabelText('运维问题')
  await user.type(question, '保留这个问题')
  await user.click(screen.getByRole('button', { name: '查询' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('模型服务暂不可用')
  expect(question).toHaveValue('保留这个问题')
  expect(screen.getByRole('link', { name: '打开运行设置' })).toHaveAttribute('href', '/settings')
  expect(screen.getByRole('button', { name: '切换此功能至 Mock' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: '切换此功能至 Mock' }))
  expect(await screen.findByText('根据资料，核心交换机使用 VLAN 410。')).toBeInTheDocument()
  expect(screen.getByText('built-in-mock 正在以 Mock 模式生成')).toBeVisible()
  expect(requests).toEqual([{ path: '/api/settings/features/knowledge', body: { mode: 'mock' } }])
})

it('cancels an in-flight knowledge stream and keeps the question available for retry', async () => {
  let streamSignal: AbortSignal | undefined
  vi.stubGlobal('fetch', vi.fn((_path: string, init?: RequestInit) => {
    streamSignal = init?.signal as AbortSignal
    return new Promise<Response>((_resolve, reject) => {
      streamSignal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
    })
  }))
  const user = userEvent.setup()
  renderPage()
  const question = screen.getByLabelText('运维问题')
  await user.type(question, '保持问题')
  await user.click(screen.getByRole('button', { name: '查询' }))
  await user.click(await screen.findByRole('button', { name: '停止生成' }))

  await waitFor(() => expect(streamSignal?.aborted).toBe(true))
  expect(await screen.findByRole('alert')).toHaveTextContent('已停止本次查询')
  expect(question).toHaveValue('保持问题')
  expect(screen.getByRole('button', { name: '重试' })).toBeEnabled()
})
