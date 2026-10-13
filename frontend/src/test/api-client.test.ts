import { afterEach, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'

afterEach(() => vi.unstubAllGlobals())

it('serializes REST object bodies as JSON and returns typed JSON responses', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)

  await expect(api.post<{ ok: boolean }>('/api/example', { task: 'demo' })).resolves.toEqual({ ok: true })
  expect(fetchMock).toHaveBeenCalledWith('/api/example', expect.objectContaining({
    method: 'POST',
    body: JSON.stringify({ task: 'demo' }),
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  }))
})

it('normalizes unauthorized API responses into the expired-session message', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ detail: '请先登录' }), { status: 401, headers: { 'Content-Type': 'application/json' } },
  )))

  await expect(api.get('/api/auth/session')).rejects.toMatchObject({
    status: 401,
    message: '登录状态已失效',
  } satisfies Partial<ApiError>)
})

it('raises ApiError with the server error text', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('service unavailable', { status: 503 })))

  await expect(api.get('/api/health')).rejects.toMatchObject({ status: 503, message: 'service unavailable' } satisfies Partial<ApiError>)
})

it('prefers a JSON detail message over raw response text', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ detail: '模型服务暂不可用' }),
    { status: 503, headers: { 'Content-Type': 'application/json' } },
  )))

  await expect(api.post('/api/knowledge/query', { question: '问题' })).rejects.toMatchObject({
    status: 503,
    message: '模型服务暂不可用',
  } satisfies Partial<ApiError>)
})

it('parses named SSE events and forwards an abort signal to fetch', async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('event: meta\ndata: {"mode":"demo"}\n\nevent: delta\ndata: {"content":"你好"}\n\n'))
      controller.close()
    },
  })
  const fetchMock = vi.fn().mockResolvedValue(new Response(stream, { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  const abortController = new AbortController()
  const events: Array<{ event: string; data: unknown }> = []

  await api.streamSse('/api/calls/suggestions/stream', { transcript: 'test' }, (event) => events.push(event), abortController.signal)

  expect(events).toEqual([
    { event: 'meta', data: { mode: 'demo' } },
    { event: 'delta', data: { content: '你好' } },
  ])
  expect(fetchMock).toHaveBeenCalledWith('/api/calls/suggestions/stream', expect.objectContaining({ signal: abortController.signal, credentials: 'same-origin' }))
})

it('rejects when an in-flight SSE request is aborted', async () => {
  const fetchMock = vi.fn((_path: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new DOMException('Request aborted', 'AbortError')))
  }))
  vi.stubGlobal('fetch', fetchMock)
  const abortController = new AbortController()
  const streaming = api.streamSse('/api/calls/suggestions/stream', { transcript: 'test' }, () => undefined, abortController.signal)
  abortController.abort()

  await expect(streaming).rejects.toThrow('Request aborted')
})

it('rejects when abort cancels an already-open response stream', async () => {
  const stream = new ReadableStream<Uint8Array>({ start() {} })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))
  const abortController = new AbortController()
  const streaming = api.streamSse('/api/knowledge/query/stream', { question: 'test' }, () => undefined, abortController.signal)
  await vi.waitFor(() => expect(fetch).toHaveBeenCalled())
  abortController.abort()

  await expect(streaming).rejects.toMatchObject({ name: 'AbortError' })
})

it('stops delivering already-buffered SSE frames as soon as a callback aborts', async () => {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode([
        'event: delta\r\ndata: {"content":"first"}\r\n\r\n',
        'event: delta\r\ndata: {"content":"stale"}\r\n\r\n',
      ].join('')))
      controller.close()
    },
  })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))
  const abortController = new AbortController()
  const contents: string[] = []

  await expect(api.streamSse<{ content: string }>(
    '/api/knowledge/query/stream',
    { question: 'test' },
    ({ data }) => {
      contents.push(data.content)
      abortController.abort()
    },
    abortController.signal,
  )).rejects.toMatchObject({ name: 'AbortError' })
  expect(contents).toEqual(['first'])
})
