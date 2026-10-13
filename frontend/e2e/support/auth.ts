import { expect, type Page } from '@playwright/test'

export const testUsername = 'task13-demo'
export const testPassword = 'task13-demo-password'

export async function login(page: Page, destination = '/') {
  await page.goto(destination)
  const loginHeading = page.getByRole('heading', {
    name: '演示系统登录',
  })
  await expect(loginHeading).toBeVisible()
  await page.getByLabel('账号').fill(testUsername)
  await page.getByLabel('密码').fill(testPassword)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(loginHeading).toBeHidden()
  await expect(
    page.getByRole('navigation', { name: '业务导航' }),
  ).toBeVisible()
}
