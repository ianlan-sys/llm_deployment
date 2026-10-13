import { expect, test, type Page } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { login, testPassword, testUsername } from './support/auth'

const here = path.dirname(fileURLToPath(import.meta.url))
const upload = path.resolve(here, 'fixtures/task13-upload.txt')
const evidenceDir = path.resolve(
  here,
  '../../.superpowers/sdd/evidence/task-13',
)

async function capture(page: Page, name: string) {
  await expect.poll(
    () => page.evaluate(() => document.fonts.status),
  ).toBe('loaded')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({
    path: path.join(evidenceDir, `${name}-1366x768.png`),
    fullPage: false,
  })
}

test.beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true })
})

test.use({ viewport: { width: 1366, height: 768 } })

test('authenticated local model knowledge lifecycle is observable end to end', async ({
  page,
}) => {
  await page.goto('/')
  await expect(
    page.getByRole('heading', { name: '演示系统登录' }),
  ).toBeVisible()
  await expect(
    page.getByText('当前为 HTTP 短期演示环境'),
  ).toBeVisible()
  await capture(page, 'login')
  await page.getByLabel('账号').fill(testUsername)
  await page.getByLabel('密码').fill(testPassword)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(
    page.getByRole('heading', { name: '工作总览' }),
  ).toBeVisible()

  await page.getByRole('link', { name: '本地模型监控' }).click()
  await expect(
    page.getByRole('heading', { name: '本地模型监控' }),
  ).toBeVisible()
  await expect(page.getByText('Ollama 在线')).toBeVisible()
  await expect(page.getByText('模型已下载')).toBeVisible()
  await expect(page.getByText('模型已加载')).toBeVisible()

  await page.getByRole('link', { name: '运维知识库' }).click()
  await page.getByRole('button', {
    name: '核心节点A的 VLAN 和网关分别是什么？',
  }).click()
  await page.getByRole('button', { name: '查询' }).click()
  await expect(page.getByText('正在检索本地资料')).toBeVisible()
  await expect(page.getByText('已命中资料')).toBeVisible()
  await expect(
    page.getByText(/qwen3:1\.7b-q4_K_M 正在本地生成/),
  ).toBeVisible()
  await expect(page.getByText('流式回答完成')).toBeVisible()
  await expect(
    page.getByText('本地模型已依据已审核资料生成回答。'),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: /来源 1/ }),
  ).toBeVisible()

  await page.getByRole('link', { name: '知识资料管理' }).click()
  await page.getByLabel('选择运维资料').setInputFiles(upload)
  await page.getByLabel('资料标题').fill('任务十三备份切换指南')
  await page.getByRole('button', { name: '上传并解析' }).click()
  await expect(
    page.getByRole('heading', {
      name: '任务十三备份切换指南',
      level: 3,
    }),
  ).toBeVisible()
  await expect(
    page.getByRole('region', { name: '解析片段' }).getByText(
      /星河数据库的备份切换窗口固定为每周三/,
    ),
  ).toBeVisible()
  await expect(
    page.getByRole('button', { name: '审核通过' }),
  ).toBeVisible()
  await capture(page, 'knowledge-management')
  await page.getByRole('button', { name: '审核通过' }).click()
  await expect(
    page.getByRole('dialog', { name: '确认审核通过' }),
  ).toBeVisible()
  await page.getByRole('button', { name: '确认入库' }).click()
  await expect(
    page
      .getByRole('button', { name: /任务十三备份切换指南/ })
      .getByText('已生效', { exact: true }),
  ).toBeVisible()

  await page.getByRole('link', { name: '运维知识库' }).click()
  await page.getByLabel('运维问题').fill(
    '星河数据库的备份切换窗口是什么时间？',
  )
  await page.getByRole('button', { name: '查询' }).click()
  await expect(page.getByText('流式回答完成')).toBeVisible()
  await expect(page.getByText('任务十三备份切换指南')).toBeVisible()
  await expect(
    page.getByRole('button', { name: /来源 1/ }),
  ).toBeVisible()

  await page.getByRole('link', { name: '运行与设置' }).click()
  await expect(
    page.getByText('文档审核：当前 Mock'),
  ).toBeVisible()
  await page.getByLabel('坐席辅助模式').selectOption('local')
  await page.getByRole('button', {
    name: '测试坐席辅助本地模型',
  }).click()
  await expect(page.getByText('本地模型可用')).toBeVisible()
  await page.getByRole('button', {
    name: '保存坐席辅助设置',
  }).click()
  await expect(
    page.getByText('坐席辅助已切换为 Local'),
  ).toBeVisible()
  await expect(
    page.getByText('文档审核：当前 Mock'),
  ).toBeVisible()

  await page.getByRole('link', { name: '本地模型监控' }).click()
  await page.reload()
  await expect(page.getByText('Ollama 在线')).toBeVisible()
  await expect(
    page.getByRole('heading', { name: /总请求 [2-9]\d*/ }),
  ).toBeVisible()
  await capture(page, 'monitoring')
})

test('login helper authenticates against the real session flow', async ({
  page,
}) => {
  await login(page, '/knowledge')
  await expect(
    page.getByRole('heading', { name: '运维知识库' }),
  ).toBeVisible()
})
