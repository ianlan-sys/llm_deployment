import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'

import { DashboardPage } from '../pages/DashboardPage'

const dashboardFixture = {
  service: 'ok',
  mode: 'demo' as const,
  today_requests: 18,
  average_latency_ms: 230,
  citation_coverage: 96,
  review_counts: { completed: 7 },
  queues: { realtime: 2, long_document: 1 },
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={queryClient}><DashboardPage /></QueryClientProvider></MemoryRouter>)
}

afterEach(() => vi.unstubAllGlobals())

it('shows all three business entry cards and live dashboard metrics', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(dashboardFixture), { status: 200 })))

  renderPage()

  expect(await screen.findByRole('link', { name: '坐席辅助' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '运维知识库' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '文档审核' })).toBeInTheDocument()
  expect(screen.getByText('18')).toBeInTheDocument()
  expect(screen.getByText('演示数据')).toBeInTheDocument()
})
