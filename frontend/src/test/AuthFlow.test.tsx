import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'

import { App } from '../app/App'
import { queryClient } from '../app/queryClient'
import { router } from '../app/router'

function response(status: number, body: unknown = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function mockAuthRequests({ sessions, login }: { sessions: number[]; login: () => Response }) {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/auth/session') {
      const status = sessions.shift() ?? 401
      return Promise.resolve(response(status, status === 200 ? { username: 'demo' } : { detail: '请先登录' }))
    }
    if (path === '/api/auth/login') return Promise.resolve(login())
    return Promise.resolve(response(200, []))
  }))
}

async function renderAt(path: string, state?: unknown) {
  queryClient.clear()
  await router.navigate(path, { state })
  return render(<App />)
}

afterEach(() => {
  queryClient.clear()
  vi.unstubAllGlobals()
})

it('redirects an unauthenticated deep link to login and returns after success', async () => {
  mockAuthRequests({
    sessions: [401, 200],
    login: () => response(200, { username: 'demo' }),
  })
  const user = userEvent.setup()
  await renderAt('/knowledge')

  expect(await screen.findByRole('heading', { name: '演示系统登录' })).toBeVisible()
  await user.type(screen.getByLabelText('账号'), 'demo')
  await user.type(screen.getByLabelText('密码'), 'correct horse')
  await user.click(screen.getByRole('button', { name: '登录' }))

  expect(await screen.findByRole('heading', { name: '运维知识库' })).toBeVisible()
})

it('returns to the complete protected deep link after success', async () => {
  mockAuthRequests({
    sessions: [401, 200],
    login: () => response(200, { username: 'demo' }),
  })
  const user = userEvent.setup()
  await renderAt('/knowledge?scope=core#answer')

  await user.type(await screen.findByLabelText('账号'), 'demo')
  await user.type(screen.getByLabelText('密码'), 'correct horse')
  await user.click(screen.getByRole('button', { name: '登录' }))

  await waitFor(() => expect(router.state.location).toMatchObject({ pathname: '/knowledge', search: '?scope=core', hash: '#answer' }))
})

it('clears the active session before navigating to login on logout', async () => {
  mockAuthRequests({
    sessions: [200],
    login: () => response(200, { username: 'demo' }),
  })
  const user = userEvent.setup()
  await renderAt('/knowledge')

  await user.click(await screen.findByRole('button', { name: '退出登录' }))

  expect(await screen.findByRole('heading', { name: '演示系统登录' })).toBeVisible()
})

it('clears cached authentication and preserves the return path after a REST request receives 401', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/auth/session') return Promise.resolve(response(200, { username: 'demo' }))
    if (path === '/api/dashboard') return Promise.resolve(response(401, { detail: '请先登录' }))
    return Promise.resolve(response(200, []))
  }))
  await renderAt('/?scope=core#queue')

  expect(await screen.findByRole('heading', { name: '演示系统登录' })).toBeVisible()
  expect(router.state.location).toMatchObject({
    pathname: '/login',
    state: { from: '/?scope=core#queue' },
  })
})

it('clears cached authentication and preserves the return path after an SSE request receives 401', async () => {
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/auth/session') return Promise.resolve(response(200, { username: 'demo' }))
    if (path === '/api/knowledge/query/stream') return Promise.resolve(response(401, { detail: '请先登录' }))
    return Promise.resolve(response(200, []))
  }))
  const user = userEvent.setup()
  await renderAt('/knowledge?scope=core#answer')

  await user.type(await screen.findByLabelText('运维问题'), '会话过期测试')
  await user.click(screen.getByRole('button', { name: '查询' }))

  expect(await screen.findByRole('heading', { name: '演示系统登录' })).toBeVisible()
  expect(router.state.location).toMatchObject({
    pathname: '/login',
    state: { from: '/knowledge?scope=core#answer' },
  })
})

it('returns to login with the protected path when CSV export receives 401', async () => {
  const completedReview = {
    id: 'review-1',
    document_filename: '方案.txt',
    document_type: 'technical_proposal',
    status: 'completed',
    stage: 'completed',
    progress: 100,
    error_summary: null,
    started_at: '2026-07-20T08:00:00Z',
    completed_at: '2026-07-20T08:01:00Z',
    created_at: '2026-07-20T08:00:00Z',
    updated_at: '2026-07-20T08:01:00Z',
    findings: [],
  }
  vi.stubGlobal('fetch', vi.fn((path: string) => {
    if (path === '/api/auth/session') return Promise.resolve(response(200, { username: 'demo' }))
    if (path === '/api/reviews') return Promise.resolve(response(200, [completedReview]))
    if (path === '/api/reviews/review-1/export.csv') return Promise.resolve(response(401, { detail: '请先登录' }))
    return Promise.resolve(response(200, []))
  }))
  const user = userEvent.setup()
  await renderAt('/reviews?scope=core#findings')

  await user.click(await screen.findByRole('button', { name: '导出审核清单' }))

  expect(await screen.findByRole('heading', { name: '演示系统登录' })).toBeVisible()
  expect(router.state.location).toMatchObject({
    pathname: '/login',
    state: { from: '/reviews?scope=core#findings' },
  })
})

it.each(['/login?next=x', '/login#x', '/login/'])('does not return an authenticated user to %s', async (from) => {
  mockAuthRequests({
    sessions: [200],
    login: () => response(200, { username: 'demo' }),
  })
  const visited: string[] = []
  const unsubscribe = router.subscribe((state) => visited.push(`${state.location.pathname}${state.location.search}${state.location.hash}`))
  try {
    await renderAt('/login', { from })
    await waitFor(() => expect(router.state.location.pathname).toBe('/'))
  } finally {
    unsubscribe()
  }

  expect(visited).not.toContain(from)
})

it.each([
  ['same-origin', `//${window.location.host}/knowledge`],
  ['external', '//example.invalid/knowledge'],
])('rejects %s protocol-relative return candidate', async (_origin, from) => {
  mockAuthRequests({
    sessions: [200],
    login: () => response(200, { username: 'demo' }),
  })
  await renderAt('/login', { from })

  await waitFor(() => expect(router.state.location.pathname).toBe('/'))
})

it('preserves the account and clears only the password after a failed login', async () => {
  mockAuthRequests({
    sessions: [401],
    login: () => response(401, { detail: '用户名或密码错误' }),
  })
  const user = userEvent.setup()
  await renderAt('/login')

  await user.type(screen.getByLabelText('账号'), 'demo')
  await user.type(screen.getByLabelText('密码'), 'wrong password')
  await user.click(screen.getByRole('button', { name: '登录' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('登录状态已失效')
  expect(screen.getByLabelText('账号')).toHaveValue('demo')
  expect(screen.getByLabelText('密码')).toHaveValue('')
})
