import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'

import { App } from '../app/App'
import { queryClient } from '../app/queryClient'
import { router } from '../app/router'
import type { KnowledgeDocumentDetail, KnowledgeDocumentSummary } from '../api/types'

const baseDocument = {
  id: 'document-1',
  title: '恢复手册',
  category: '数据库、备份与恢复',
  scope: '全局',
  version: '1.0',
  effective_date: null,
  status: 'pending_review',
  content_summary: '恢复步骤摘要',
  error_summary: null,
  reviewed_at: null,
  reviewed_by: null,
  created_at: '2026-07-24T08:00:00Z',
  updated_at: '2026-07-24T08:00:01Z',
} satisfies KnowledgeDocumentSummary

const preview = {
  ...baseDocument,
  media_type: 'text/plain',
  file_size: 12,
  parse_warnings: [],
  chunks: [{ id: 'chunk-1', content: '恢复步骤', location: '正文第 1 段', position: 1 }],
} satisfies KnowledgeDocumentDetail

async function renderRoute(path: string) {
  queryClient.clear()
  await router.navigate(path)
  return render(<App />)
}

afterEach(() => {
  queryClient.clear()
  vi.unstubAllGlobals()
})

it('previews and approves an uploaded document before showing it active', async () => {
  let document: KnowledgeDocumentDetail = preview
  vi.stubGlobal('fetch', vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/auth/session') return Promise.resolve(new Response(JSON.stringify({ username: 'demo' }), { status: 200 }))
    if (path === '/api/knowledge-documents' && init?.method === 'POST') {
      return Promise.resolve(new Response(JSON.stringify({ ...baseDocument, status: 'uploaded' }), { status: 202 }))
    }
    if (path === '/api/knowledge-documents') return Promise.resolve(new Response(JSON.stringify(document ? [document] : []), { status: 200 }))
    if (path === '/api/knowledge-documents/document-1/approve') {
      document = { ...preview, status: 'active', reviewed_at: '2026-07-24T08:01:00Z', reviewed_by: 'demo' }
      return Promise.resolve(new Response(JSON.stringify(document), { status: 200 }))
    }
    if (path === '/api/knowledge-documents/document-1') return Promise.resolve(new Response(JSON.stringify(document), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()
  await renderRoute('/knowledge/documents')

  await user.upload(await screen.findByLabelText('选择运维资料'), new File(['恢复步骤'], '恢复手册.txt', { type: 'text/plain' }))
  await user.click(screen.getByRole('button', { name: '上传并解析' }))

  expect(await screen.findByText('待审核')).toBeVisible()
  expect(screen.getByText('恢复步骤')).toBeVisible()
  await user.click(screen.getByRole('button', { name: '审核通过' }))
  const dialog = screen.getByRole('dialog', { name: '确认审核通过' })
  expect(dialog).toHaveTextContent('恢复手册')
  await user.click(screen.getByRole('button', { name: '确认入库' }))
  expect(await screen.findByText('已生效')).toBeVisible()
  expect(screen.getByRole('heading', { name: '恢复手册' })).toHaveFocus()
})

it('keeps a distinct newly uploaded document selected when the active filter excludes uploaded rows', async () => {
  const uploadedSummary = {
    ...baseDocument,
    id: 'document-2',
    title: '新恢复手册',
    status: 'uploaded',
  } satisfies KnowledgeDocumentSummary
  const uploadedPreview = {
    ...preview,
    id: 'document-2',
    title: '新恢复手册',
    status: 'pending_review',
    chunks: [{ id: 'chunk-2', content: '新恢复步骤', location: '正文第 1 段', position: 1 }],
  } satisfies KnowledgeDocumentDetail
  vi.stubGlobal('fetch', vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/auth/session') return Promise.resolve(new Response(JSON.stringify({ username: 'demo' }), { status: 200 }))
    if (path === '/api/knowledge-documents' && init?.method === 'POST') {
      return Promise.resolve(new Response(JSON.stringify(uploadedSummary), { status: 202 }))
    }
    if (path === '/api/knowledge-documents' || path === '/api/knowledge-documents?status=pending_review') {
      return Promise.resolve(new Response(JSON.stringify([preview]), { status: 200 }))
    }
    if (path === '/api/knowledge-documents/document-1') return Promise.resolve(new Response(JSON.stringify(preview), { status: 200 }))
    if (path === '/api/knowledge-documents/document-2') return Promise.resolve(new Response(JSON.stringify(uploadedPreview), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()
  await renderRoute('/knowledge/documents')
  await user.selectOptions(await screen.findByLabelText('状态筛选'), 'pending_review')
  await screen.findByRole('button', { name: /恢复手册/ })

  await user.upload(screen.getByLabelText('选择运维资料'), new File(['新恢复步骤'], '新恢复手册.txt', { type: 'text/plain' }))
  fireEvent.submit(screen.getByRole('button', { name: '上传并解析' }).closest('form')!)

  expect(await screen.findByRole('heading', { name: '新恢复手册' })).toBeVisible()
  expect(screen.getByText('新恢复步骤')).toBeVisible()
  expect(screen.getByRole('button', { name: /恢复手册/ })).toHaveAttribute('aria-pressed', 'false')
})

it('retains the selected metadata and parse preview when an approval action fails', async () => {
  let approvalFailed = false
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/auth/session') return Promise.resolve(new Response(JSON.stringify({ username: 'demo' }), { status: 200 }))
    if (path === '/api/knowledge-documents') return Promise.resolve(new Response(JSON.stringify([
      approvalFailed ? { ...preview, status: 'index_failed', error_summary: '资料索引失败，请重试审核。' } : preview,
    ]), { status: 200 }))
    if (path === '/api/knowledge-documents/document-1') return Promise.resolve(new Response(JSON.stringify(
      approvalFailed ? { ...preview, status: 'index_failed', error_summary: '资料索引失败，请重试审核。' } : preview,
    ), { status: 200 }))
    if (path === '/api/knowledge-documents/document-1/approve') {
      approvalFailed = true
      return Promise.resolve(new Response(JSON.stringify({ detail: '资料索引失败，请重试审核。' }), { status: 503 }))
    }
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()
  await renderRoute('/knowledge/documents')

  await user.click(await screen.findByRole('button', { name: /恢复手册/ }))
  await user.click(screen.getByRole('button', { name: '审核通过' }))
  await user.click(screen.getByRole('button', { name: '确认入库' }))

  expect((await screen.findAllByRole('alert')).some((alert) => alert.textContent?.includes('资料索引失败'))).toBe(true)
  expect(screen.getByText('恢复步骤')).toBeVisible()
  expect(screen.getByText('数据库、备份与恢复')).toBeVisible()
  expect(screen.getByText('全局')).toBeVisible()
  expect(screen.getByRole('button', { name: '重试' })).toBeEnabled()
  expect(await within(screen.getByRole('button', { name: /恢复手册/ })).findByText('入库失败')).toBeVisible()
})
