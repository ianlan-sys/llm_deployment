import { expect, test } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { login } from './support/auth'

const here = path.dirname(fileURLToPath(import.meta.url))
const proposal = path.resolve(here, '../../backend/app/data/review_samples/technical-proposal.txt')

test('offline demo completes all three business flows', async ({ page }) => {
  await login(page)
  await expect(page.getByRole('heading', { name: '工作总览' })).toBeVisible()

  await page.getByRole('link', { name: '坐席辅助' }).first().click()
  await page.getByLabel('通话记录').fill('客户反馈原始故障情况')
  await page.getByRole('button', { name: '生成推荐话术' }).click()
  await expect(page.getByText(/我已记录设备故障情况/)).toBeVisible()
  await expect(page.getByLabel('已审核话术来源')).toBeVisible()
  await page.getByRole('button', { name: '采纳话术' }).click()
  await page.getByLabel('已采纳话术').fill('人工编辑后的采纳回复：两小时内回访。')
  const summaryRequest = page.waitForRequest((request) => request.url().endsWith('/api/calls/summary'))
  await page.getByRole('button', { name: '生成工单摘要' }).click()
  const summaryPayload = (await summaryRequest).postDataJSON()
  expect(summaryPayload).toMatchObject({
    original_transcript: '客户反馈原始故障情况',
    accepted_reply: '人工编辑后的采纳回复：两小时内回访。',
  })
  expect(summaryPayload.scenario_id).toBeTruthy()
  expect(summaryPayload.session_id).toBeTruthy()
  await expect(page.getByText('请求已受理，等待后续处理')).toBeVisible()

  await page.getByRole('link', { name: '运维知识库' }).click()
  await page.getByLabel('运维问题').fill('核心交换机 VLAN 如何划分？')
  await page.getByRole('button', { name: '查询' }).click()
  const source = page.getByRole('button', { name: /来源 1/ })
  await expect(source).toBeVisible()
  await source.click()
  await expect(page.getByRole('dialog', { name: '资料出处' })).toBeVisible()
  await page.getByRole('button', { name: '关闭资料出处' }).click()

  await page.getByRole('link', { name: '文档审核' }).click()
  await page.getByLabel('选择待审文件').setInputFiles(proposal)
  await page.getByLabel('文件类型').selectOption('technical_proposal')
  await page.getByRole('button', { name: '开始审核' }).click()
  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('button', { name: '导出审核清单' })).toBeEnabled()
  await expect(page.getByText('未发现“验收指标”相关说明。')).toHaveCount(1)
  await expect(page.getByText('未发现“项目范围”相关说明。')).toHaveCount(0)
  await expect(page.getByText('未发现“实施计划”相关说明。')).toHaveCount(0)
})
