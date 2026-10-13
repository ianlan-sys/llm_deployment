import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { it, expect, vi } from 'vitest'

import { AppShell } from '../components/AppShell'

it('renders the approved navigation entries including model monitoring', () => {
  const onLogout = vi.fn()
  render(
    <MemoryRouter>
      <AppShell onLogout={onLogout}><div>内容</div></AppShell>
    </MemoryRouter>,
  )

  for (const name of ['工作总览', '坐席辅助', '运维知识库', '文档审核', '本地模型监控', '运行与设置']) {
    expect(screen.getByRole('link', { name })).toBeInTheDocument()
  }
  expect(screen.getByRole('button', { name: '退出登录' })).toBeInTheDocument()
})
