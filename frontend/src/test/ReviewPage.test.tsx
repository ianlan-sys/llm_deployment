import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'

import { ReviewPage } from '../pages/ReviewPage'

const queuedReview = {
  id: 'review-1', document_filename: '方案.txt', document_type: 'technical_proposal',
  status: 'matching_rules', stage: 'matching_rules', progress: 25, error_summary: null,
  started_at: '2026-07-20T08:00:00Z', completed_at: null, created_at: '2026-07-20T08:00:00Z', updated_at: '2026-07-20T08:00:01Z', findings: [],
}

const completedReview = {
  ...queuedReview, status: 'completed', stage: 'completed', progress: 100,
  completed_at: '2026-07-20T08:01:00Z',
  findings: [{
    id: 'finding-1', rule_id: 'rule-1', rule_source: '技术方案编制规范 第 4.2 条', risk: 'high',
    description: '未明确核心服务的故障恢复目标。', location: '第 3 章 / 可用性设计',
    excerpt: '系统具备高可用能力。', recommendation: '补充 RTO、RPO 目标和故障切换流程。', human_review_status: 'pending',
  }],
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

it('shows asynchronous review stages and findings', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string, init?: RequestInit) => {
    if (path === '/api/reviews' && init?.method === 'GET') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
    if (path === '/api/reviews' && init?.method === 'POST') return Promise.resolve(new Response(JSON.stringify(queuedReview), { status: 202 }))
    if (path === '/api/reviews/review-1') return Promise.resolve(new Response(JSON.stringify(completedReview), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()

  render(<ReviewPage />)
  const file = new File(['技术方案正文'], '方案.txt', { type: 'text/plain' })
  await user.upload(screen.getByLabelText('选择待审文件'), file)
  await user.selectOptions(screen.getByLabelText('文件类型'), 'technical_proposal')
  await user.click(screen.getByRole('button', { name: '开始审核' }))

  expect(await screen.findByText('规则匹配')).toBeInTheDocument()
  expect(await screen.findByText('审核完成')).toBeInTheDocument()
  expect(screen.getByText('高风险')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '导出审核清单' })).toBeEnabled()
})

it('lists server tasks with localized human-review status and a complete finding detail', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/reviews') return Promise.resolve(new Response(JSON.stringify([completedReview]), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))

  render(<ReviewPage />)

  expect(await screen.findByRole('button', { name: /待复核/ })).toBeInTheDocument()
  expect(screen.getByLabelText('风险项详情')).toHaveTextContent('系统具备高可用能力。')
  expect(screen.getByLabelText('风险项详情')).toHaveTextContent('技术方案编制规范 第 4.2 条')
  expect(screen.getByLabelText('风险项详情')).toHaveTextContent('补充 RTO、RPO 目标和故障切换流程。')
  expect(screen.getByLabelText('风险项详情')).toHaveTextContent('不构成正式审核结论')
})

it('keeps failed tasks reachable and only offers retry for a terminal failure', async () => {
  const failedReview = { ...completedReview, id: 'review-failed', document_filename: '失败文档.txt', status: 'failed' as const, stage: 'reviewing', progress: 40, error_summary: '模型服务暂不可用。' }
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/reviews') return Promise.resolve(new Response(JSON.stringify([completedReview, failedReview]), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  const user = userEvent.setup()

  render(<ReviewPage />)
  await user.click(await screen.findByRole('button', { name: /失败文档.txt/ }))

  expect(screen.getByRole('button', { name: '重新审核' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: '取消审核' })).not.toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('模型服务暂不可用。')
})

it('rejects files without the backend-required media type before submission', async () => {
  const fetchMock = vi.fn((path: string) => {
    if (path === '/api/reviews') return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  })
  vi.stubGlobal('fetch', fetchMock)
  const user = userEvent.setup()

  render(<ReviewPage />)
  await user.upload(screen.getByLabelText('选择待审文件'), new File(['正文'], '方案.txt'))

  expect(await screen.findByRole('alert')).toHaveTextContent('文件格式与其内容类型不一致')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('polls every active task once per second and stops after all become terminal', async () => {
  const activeA = { ...queuedReview, id: 'active-a', document_filename: 'A.txt', status: 'queued', stage: 'queued' }
  const activeB = { ...queuedReview, id: 'active-b', document_filename: 'B.txt', status: 'reviewing', stage: 'reviewing' }
  const terminalA = { ...activeA, status: 'completed', stage: 'completed', progress: 100 }
  const terminalB = { ...activeB, status: 'completed', stage: 'completed', progress: 100 }
  const fetchMock = vi.fn((path: string) => {
    if (path === '/api/reviews') return Promise.resolve(new Response(JSON.stringify([activeA, activeB]), { status: 200 }))
    if (path === '/api/reviews/active-a') return Promise.resolve(new Response(JSON.stringify(terminalA), { status: 200 }))
    if (path === '/api/reviews/active-b') return Promise.resolve(new Response(JSON.stringify(terminalB), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  })
  vi.stubGlobal('fetch', fetchMock)
  vi.useFakeTimers()
  render(<ReviewPage />)
  await act(async () => { await Promise.resolve() })
  expect(screen.getByRole('button', { name: /A.txt/ })).toBeInTheDocument()

  await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve() })
  expect(fetchMock).toHaveBeenCalledWith('/api/reviews/active-a', expect.anything())
  expect(fetchMock).toHaveBeenCalledWith('/api/reviews/active-b', expect.anything())
  const callCountAfterTerminal = fetchMock.mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(fetchMock).toHaveBeenCalledTimes(callCountAfterTerminal)
})

it('keeps the user selection when an earlier background poll resolves', async () => {
  const activeA = { ...queuedReview, id: 'active-a', document_filename: 'A.txt', status: 'queued', stage: 'queued' }
  const activeB = { ...queuedReview, id: 'active-b', document_filename: 'B.txt', status: 'reviewing', stage: 'reviewing' }
  let resolveA: ((response: Response) => void) | undefined
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/reviews') return Promise.resolve(new Response(JSON.stringify([activeA, activeB]), { status: 200 }))
    if (path === '/api/reviews/active-a') return new Promise<Response>((resolve) => { resolveA = resolve })
    if (path === '/api/reviews/active-b') return Promise.resolve(new Response(JSON.stringify(activeB), { status: 200 }))
    return Promise.reject(new Error(`Unexpected request: ${path}`))
  }))
  vi.useFakeTimers()
  render(<ReviewPage />)
  await act(async () => { await Promise.resolve() })
  const taskA = screen.getByRole('button', { name: /A.txt/ })
  const taskB = screen.getByRole('button', { name: /B.txt/ })

  await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve() })
  fireEvent.click(taskB)
  await act(async () => { resolveA?.(new Response(JSON.stringify({ ...activeA, progress: 10 }), { status: 200 })); await Promise.resolve() })

  expect(taskA).not.toHaveClass('is-selected')
  expect(taskB).toHaveClass('is-selected')
})

it('uses backend stage for terminal timeline position rather than terminal status', async () => {
  const failedReview = { ...completedReview, status: 'failed', stage: 'reviewing', progress: 40, error_summary: '审核中断。' }
  vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(new Response(JSON.stringify(path === '/api/reviews' ? [failedReview] : failedReview), { status: 200 }))))
  render(<ReviewPage />)

  const reviewingStep = await screen.findByText('内容审核')
  expect(reviewingStep.closest('li')).toHaveAttribute('aria-current', 'step')
  expect(reviewingStep.closest('li')).toHaveClass('is-terminal-failed')
  expect(screen.getByText('审核完成').closest('li')).not.toHaveAttribute('aria-current')
  expect(screen.getByText(/审核失败.*内容审核/)).toBeInTheDocument()
})

it('clears a selected finding when the active filters exclude it', async () => {
  const lowFinding = { ...completedReview.findings[0], id: 'finding-low', risk: 'low', description: '低风险项', excerpt: '低风险原文', human_review_status: 'confirmed' }
  const filteredReview = { ...completedReview, findings: [completedReview.findings[0], lowFinding] }
  vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(new Response(JSON.stringify(path === '/api/reviews' ? [filteredReview] : filteredReview), { status: 200 }))))
  const user = userEvent.setup()
  render(<ReviewPage />)
  await user.click(await screen.findByRole('button', { name: /低风险项/ }))
  expect(screen.getByLabelText('风险项详情')).toHaveTextContent('低风险原文')
  await user.selectOptions(screen.getByLabelText('风险等级'), 'high')

  await waitFor(() => expect(screen.getByLabelText('风险项详情')).not.toHaveTextContent('低风险原文'))
  expect(screen.getByLabelText('风险项详情')).toHaveTextContent('选择风险项后查看')
})

it('creates, clicks, removes, and later revokes a CSV download URL', async () => {
  const createObjectURL = vi.fn(() => 'blob:review-csv')
  const revokeObjectURL = vi.fn()
  Object.assign(URL, { createObjectURL, revokeObjectURL })
  vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(new Response(path === '/api/reviews' ? JSON.stringify([completedReview]) : '风险清单', { status: 200 }))))
  render(<ReviewPage />)
  await screen.findByRole('button', { name: /方案.txt/ })
  const appendSpy = vi.spyOn(document.body, 'append')
  const removeSpy = vi.spyOn(HTMLAnchorElement.prototype, 'remove')
  const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  vi.useFakeTimers()
  fireEvent.click(screen.getByRole('button', { name: '导出审核清单' }))
  await act(async () => { await Promise.resolve() })

  expect(createObjectURL).toHaveBeenCalled()
  expect(appendSpy).toHaveBeenCalled()
  expect(clickSpy).toHaveBeenCalled()
  expect(removeSpy).toHaveBeenCalled()
  expect(revokeObjectURL).not.toHaveBeenCalled()
  await act(async () => { await vi.runOnlyPendingTimersAsync() })
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:review-csv')
})

it('reports a CSV export failure without creating a download anchor', async () => {
  const createObjectURL = vi.fn()
  Object.assign(URL, { createObjectURL })
  vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(new Response(path === '/api/reviews' ? JSON.stringify([completedReview]) : '导出失败', { status: path === '/api/reviews' ? 200 : 500 }))))
  const user = userEvent.setup()
  render(<ReviewPage />)
  await screen.findByRole('button', { name: /方案.txt/ })
  const appendSpy = vi.spyOn(document.body, 'append')
  await user.click(screen.getByRole('button', { name: '导出审核清单' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('导出失败')
  expect(createObjectURL).not.toHaveBeenCalled()
  expect(appendSpy).not.toHaveBeenCalled()
})

it('validates upload size, extension, MIME, UTF-8 TXT, PDF, and DOCX before submission', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => Promise.resolve(new Response(JSON.stringify(path === '/api/reviews' ? [] : queuedReview), { status: 200 }))))
  const user = userEvent.setup()
  render(<ReviewPage />)
  const input = screen.getByLabelText('选择待审文件')

  await user.upload(input, new File([new Uint8Array(20 * 1024 * 1024 + 1)], 'oversize.txt', { type: 'text/plain' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('不能超过 20 MB')
  await user.upload(input, new File(['x'], 'unsupported.csv', { type: 'text/plain' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('仅支持 PDF、DOCX')
  await user.upload(input, new File(['x'], 'mismatch.pdf', { type: 'text/plain' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('内容类型不一致')
  await user.upload(input, new File([new Uint8Array([0xc3, 0x28])], 'invalid.txt', { type: 'text/plain' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('必须使用 UTF-8')
  await user.upload(input, new File(['%PDF-1.7'], 'valid.pdf', { type: 'application/pdf' }))
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(screen.getByRole('button', { name: '开始审核' })).toBeEnabled()
  await user.upload(input, new File(['PK'], 'valid.docx', { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }))
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(screen.getByRole('button', { name: '开始审核' })).toBeEnabled()
})

it('does not apply a resolving background poll after unmount', async () => {
  const activeReview = { ...queuedReview, status: 'reviewing', stage: 'reviewing' }
  let resolvePoll: ((response: Response) => void) | undefined
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/reviews') return Promise.resolve(new Response(JSON.stringify([activeReview]), { status: 200 }))
    return new Promise<Response>((resolve) => { resolvePoll = resolve })
  }))
  vi.useFakeTimers()
  const view = render(<ReviewPage />)
  await act(async () => { await Promise.resolve() })
  await act(async () => { vi.advanceTimersByTime(1000); await Promise.resolve() })
  view.unmount()
  await act(async () => { resolvePoll?.(new Response(JSON.stringify(completedReview), { status: 200 })); await Promise.resolve() })

  expect(view.container).toBeEmptyDOMElement()
})

it('retains actionable task state when cancel or retry requests fail', async () => {
  const activeReview = { ...queuedReview, status: 'reviewing', stage: 'reviewing' }
  const failedReview = { ...queuedReview, id: 'failed-review', document_filename: '失败.txt', status: 'failed', stage: 'reviewing', error_summary: '原始失败信息' }
  let mode: 'active' | 'failed' = 'active'
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/reviews') return Promise.resolve(new Response(JSON.stringify(mode === 'active' ? [activeReview] : [failedReview]), { status: 200 }))
    return Promise.reject(new Error(mode === 'active' ? '取消失败' : '重试失败'))
  }))
  const user = userEvent.setup()
  const view = render(<ReviewPage />)
  await user.click(await screen.findByRole('button', { name: '取消审核' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('取消失败')
  expect(screen.getByRole('button', { name: '取消审核' })).toBeEnabled()

  view.unmount(); mode = 'failed'
  render(<ReviewPage />)
  await user.click(await screen.findByRole('button', { name: '重新审核' }))
  expect(await screen.findByText('重试失败')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '重新审核' })).toBeEnabled()
})
