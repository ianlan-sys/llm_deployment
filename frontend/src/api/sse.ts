import { notifySessionInvalidated } from '../auth/sessionInvalidation'

export interface SseEvent<T> {
  event: string
  data: T
}

async function errorMessage(response: Response, fallback: string) {
  const text = await response.text()
  if (!text) return fallback
  try {
    const payload = JSON.parse(text) as { detail?: unknown }
    if (typeof payload.detail === 'string' && payload.detail.trim()) return payload.detail
  } catch {
    // Non-JSON server responses are still useful diagnostic summaries.
  }
  return text
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }

  static async fromResponse(response: Response, fallback = `请求失败（${response.status}）`) {
    if (response.status === 401) notifySessionInvalidated()
    return new ApiError(
      response.status,
      response.status === 401 ? '登录状态已失效' : await errorMessage(response, fallback),
    )
  }
}

function parseFrame<T>(frame: string): SseEvent<T> | null {
  const lines = frame.split(/\r?\n/)
  const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim() || 'message'
  const data = lines
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).replace(/^ /, ''))
    .join('\n')
  return data ? { event, data: JSON.parse(data) as T } : null
}

export async function readSseFrames<T>(
  stream: ReadableStream<Uint8Array>,
  onEvent: (frame: SseEvent<T>) => void,
  signal: AbortSignal,
) {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const cancelReader = () => { void reader.cancel() }
  signal.addEventListener('abort', cancelReader, { once: true })
  try {
    while (true) {
      signal.throwIfAborted()
      const { value, done } = await reader.read()
      signal.throwIfAborted()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() ?? ''
      for (const rawFrame of frames) {
        signal.throwIfAborted()
        const frame = parseFrame<T>(rawFrame)
        if (frame) {
          signal.throwIfAborted()
          onEvent(frame)
          signal.throwIfAborted()
        }
      }
      if (done) {
        signal.throwIfAborted()
        const finalFrame = parseFrame<T>(buffer)
        if (finalFrame) {
          signal.throwIfAborted()
          onEvent(finalFrame)
          signal.throwIfAborted()
        }
        return
      }
    }
  } finally {
    signal.removeEventListener('abort', cancelReader)
    reader.releaseLock()
  }
}

export async function postSse<T>(
  path: string,
  body: Record<string, unknown>,
  onEvent: (frame: SseEvent<T>) => void,
  signal: AbortSignal,
) {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    signal,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
  })
  if (!response.ok || !response.body) throw await ApiError.fromResponse(response, `流式请求失败（${response.status}）`)
  await readSseFrames(response.body, onEvent, signal)
}
