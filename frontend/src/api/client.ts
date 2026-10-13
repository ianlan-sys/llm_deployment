import { ApiError, postSse, type SseEvent } from './sse'

export { ApiError, type SseEvent } from './sse'

type JsonBody = object | unknown[]
type RequestBody = BodyInit | JsonBody | undefined

function toRequestBody(body: RequestBody): BodyInit | undefined {
  if (body === undefined || typeof body === 'string' || body instanceof FormData || body instanceof URLSearchParams || body instanceof Blob) return body
  return JSON.stringify(body)
}

async function request<T>(path: string, init: RequestInit = {}, body?: RequestBody): Promise<T> {
  const serializedBody = toRequestBody(body)
  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    body: serializedBody,
    headers: { ...(serializedBody && !(serializedBody instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...init.headers },
  })
  if (!response.ok) throw await ApiError.fromResponse(response)
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  get: <T>(path: string, init?: RequestInit) => request<T>(path, { ...init, method: 'GET' }),
  post: <T>(path: string, body?: RequestBody, init?: RequestInit) => request<T>(path, { ...init, method: 'POST' }, body),
  put: <T>(path: string, body?: RequestBody, init?: RequestInit) => request<T>(path, { ...init, method: 'PUT' }, body),
  streamSse: async <T>(
    path: string,
    body: Record<string, unknown>,
    onEvent: (event: SseEvent<T>) => void,
    signal: AbortSignal,
  ) => postSse(path, body, onEvent, signal),
}
