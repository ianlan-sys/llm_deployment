import { expect, test, type Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { login } from './support/auth'

const here = path.dirname(fileURLToPath(import.meta.url))
const evidenceDir = path.resolve(here, '../../.superpowers/sdd/evidence/task-12')
const proposal = path.resolve(here, '../../backend/app/data/review_samples/technical-proposal.txt')

async function capture(page: Page, name: string) {
  await expect.poll(() => page.evaluate(() => document.fonts.status)).toBe('loaded')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.waitForTimeout(100)
  await page.screenshot({ path: path.join(evidenceDir, `${name}-1366x768.png`), fullPage: false })
}

async function expectNoHorizontalPageScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
}

test.beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true })
})

test.use({ viewport: { width: 1366, height: 768 } })

test('all routes fit a 1366x768 desktop viewport', async ({ page }) => {
  await login(page)
  for (const [name, route, heading] of [
    ['dashboard', '/', '工作总览'],
    ['calls', '/calls', '坐席辅助'],
    ['knowledge', '/knowledge', '运维知识库'],
    ['reviews', '/reviews', '文档审核'],
    ['settings', '/settings', '运行与设置'],
    ['monitoring', '/monitoring', '本地模型监控'],
  ] as const) {
    await page.goto(route)
    await expect(page.getByRole('heading', { name: heading })).toBeVisible()
    await expectNoHorizontalPageScroll(page)
    await capture(page, name)
  }
})

test('source drawer and completed review actions remain reachable', async ({ page }) => {
  await login(page, '/knowledge')
  await page.getByLabel('运维问题').fill('核心交换机 VLAN 如何划分？')
  await page.getByRole('button', { name: '查询' }).click()
  await page.getByRole('button', { name: /来源 1/ }).click()
  const drawer = page.getByRole('dialog', { name: '资料出处' })
  await expect(drawer).toBeVisible()
  const bounds = await drawer.boundingBox()
  expect(bounds).not.toBeNull()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(1366)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(768)
  await capture(page, 'knowledge-source-drawer')
  await page.getByRole('button', { name: '关闭资料出处' }).click()

  await page.goto('/reviews')
  await page.getByLabel('选择待审文件').setInputFiles(proposal)
  await page.getByLabel('文件类型').selectOption('technical_proposal')
  await page.getByRole('button', { name: '开始审核' }).click()
  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 30_000 })
  const exportButton = page.getByRole('button', { name: '导出审核清单' })
  await exportButton.scrollIntoViewIfNeeded()
  await expect(exportButton).toBeVisible()
  await expect(exportButton).toBeEnabled()
  await expect(page.getByLabel('风险等级')).toBeVisible()
  await expect(page.getByLabel('人工复核状态')).toBeVisible()
  await expectNoHorizontalPageScroll(page)
  await capture(page, 'reviews-completed')
})

test('loading, empty, and error states remain legible', async ({ page }) => {
  let releaseDashboard = () => {}
  const dashboardGate = new Promise<void>((resolve) => { releaseDashboard = resolve })
  await page.route('**/api/dashboard', async (route) => {
    await dashboardGate
    await route.continue()
  })
  await login(page)
  await expect(page.getByText('正在加载工作总览…')).toBeVisible()
  await capture(page, 'dashboard-loading')
  releaseDashboard()
  await expect(page.getByRole('heading', { name: '工作总览' })).toBeVisible()

  await page.unroute('**/api/dashboard')
  await page.route('**/api/dashboard', (route) => route.fulfill({ status: 503, body: '演示错误' }))
  await page.reload()
  await expect(page.getByRole('heading', { name: '加载失败' })).toBeVisible()
  await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
  await capture(page, 'dashboard-error')

  await page.unroute('**/api/dashboard')
  await page.route('**/api/reviews', (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    : route.continue())
  await page.goto('/reviews')
  await expect(page.getByText('暂无审核任务。')).toBeVisible()
  await capture(page, 'reviews-empty')
})
