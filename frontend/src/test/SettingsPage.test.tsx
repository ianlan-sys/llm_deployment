import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'

import type { FeatureProviderSettings } from '../api/types'
import { SettingsPage } from '../pages/SettingsPage'

const auditFixture = [{
  id: 'audit-1', event_type: 'knowledge.query.completed', request_summary: '已脱敏的操作摘要',
  reference_id: 'chunk-1', mode: 'local' as const, elapsed_ms: 142, result_status: 'answered', created_at: '2026-07-20T08:00:00Z',
}]

const defaultFeatureSettings: FeatureProviderSettings[] = [
  { feature: 'call_assistant', mode: 'mock' },
  { feature: 'knowledge', mode: 'local' },
  { feature: 'document_review', mode: 'mock' },
]

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}><SettingsPage /></QueryClientProvider>)
}

function jsonBody(init?: RequestInit) {
  return JSON.parse(String(init?.body))
}

function mockFeatureSettings(
  settings = defaultFeatureSettings,
  respond?: (path: string, init?: RequestInit) => Promise<Response> | undefined,
) {
  const fetchMock = vi.fn((path: string, init?: RequestInit) => {
    const customResponse = respond?.(path, init)
    if (customResponse) return customResponse
    if (path === '/api/settings/features') return Promise.resolve(new Response(JSON.stringify(settings), { status: 200 }))
    if (path === '/api/audit-events?limit=50') return Promise.resolve(new Response(JSON.stringify(auditFixture), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path} ${init?.method}`))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => vi.unstubAllGlobals())

it('switches knowledge to mock without changing call or review', async () => {
  const settings = defaultFeatureSettings.map((item) => ({ ...item }))
  const fetchMock = mockFeatureSettings(settings, (path, init) => {
    if (path === '/api/settings/features/knowledge' && init?.method === 'PUT') {
      const result = { feature: 'knowledge', mode: 'mock' }
      return Promise.resolve(new Response(JSON.stringify(result), { status: 200 }))
    }
  })
  const user = userEvent.setup()

  renderPage()
  await user.selectOptions(await screen.findByLabelText('运维知识库模式'), 'mock')

  expect(screen.getByText('运维知识库：当前 Local')).toBeVisible()
  await user.click(screen.getByRole('button', { name: '保存运维知识库设置' }))

  const update = fetchMock.mock.calls.find(([path, init]) => path === '/api/settings/features/knowledge' && init?.method === 'PUT')
  expect(update).toBeDefined()
  expect(jsonBody(update?.[1])).toEqual({ mode: 'mock' })
  expect(await screen.findByText('运维知识库：当前 Mock')).toBeVisible()
  expect(screen.getByText('坐席辅助：当前 Mock')).toBeVisible()
  expect(screen.getByText('文档审核：当前 Mock')).toBeVisible()
  expect(fetchMock.mock.calls.some(([path]) => path === '/api/settings/features/knowledge/test')).toBe(false)
})

it('keeps persisted badges unchanged for unsaved candidate selections', async () => {
  mockFeatureSettings()
  const user = userEvent.setup()

  renderPage()
  await user.selectOptions(await screen.findByLabelText('坐席辅助模式'), 'local')

  expect(screen.getByText('坐席辅助：当前 Mock')).toBeVisible()
  expect(screen.getByText('待保存：Local')).toBeVisible()
  expect(screen.getByRole('button', { name: '保存坐席辅助设置' })).toBeDisabled()
  expect(screen.getByText('运维知识库：当前 Local')).toBeVisible()
  expect(screen.getByText('文档审核：当前 Mock')).toBeVisible()
})

it('requires a matching successful local test and sends mode-only payloads', async () => {
  const fetchMock = mockFeatureSettings(defaultFeatureSettings, (path, init) => {
    if (path === '/api/settings/features/call_assistant/test' && init?.method === 'POST') {
      return Promise.resolve(new Response(JSON.stringify({ available: true, detail: '本地模型可用' }), { status: 200 }))
    }
    if (path === '/api/settings/features/call_assistant' && init?.method === 'PUT') {
      return Promise.resolve(new Response(JSON.stringify({
        feature: 'call_assistant', mode: 'local',
      }), { status: 200 }))
    }
  })
  const user = userEvent.setup()

  renderPage()
  await user.selectOptions(await screen.findByLabelText('坐席辅助模式'), 'local')

  const saveButton = screen.getByRole('button', { name: '保存坐席辅助设置' })
  expect(saveButton).toBeDisabled()
  await user.click(screen.getByRole('button', { name: '测试坐席辅助本地模型' }))
  expect(await screen.findByText('本地模型可用')).toBeVisible()
  expect(saveButton).toBeEnabled()

  await user.selectOptions(screen.getByLabelText('坐席辅助模式'), 'mock')
  await user.selectOptions(screen.getByLabelText('坐席辅助模式'), 'local')
  expect(saveButton).toBeDisabled()

  await user.click(screen.getByRole('button', { name: '测试坐席辅助本地模型' }))
  expect(saveButton).toBeEnabled()
  await user.click(saveButton)

  const tests = fetchMock.mock.calls.filter(([path]) => path === '/api/settings/features/call_assistant/test')
  expect(tests).toHaveLength(2)
  expect(tests.map(([, init]) => jsonBody(init))).toEqual([{ mode: 'local' }, { mode: 'local' }])
  const update = fetchMock.mock.calls.find(([path, init]) => path === '/api/settings/features/call_assistant' && init?.method === 'PUT')
  expect(jsonBody(update?.[1])).toEqual({ mode: 'local' })
  expect(await screen.findByText('坐席辅助：当前 Local')).toBeVisible()
})

it('renders the private Ollama service as a safe label and keeps audit data redacted', async () => {
  const fetchMock = mockFeatureSettings()

  renderPage()

  expect(await screen.findByText('Docker 内网服务')).toBeVisible()
  expect(screen.getByRole('heading', { name: '最近审计记录' })).toBeVisible()
  expect(screen.getByText('knowledge.query.completed')).toBeVisible()
  expect(screen.queryByText('完整用户问题正文')).not.toBeInTheDocument()
  expect(document.body).not.toHaveTextContent('http://ollama:11434')
  expect(document.body).not.toHaveTextContent('/v1')
  expect(screen.queryByLabelText(/服务地址|API 密钥/)).not.toBeInTheDocument()
  expect(fetchMock.mock.calls.some(([path]) => path === '/api/settings/provider')).toBe(false)
})
