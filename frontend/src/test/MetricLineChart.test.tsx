import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'

import { MetricLineChart } from '../components/MetricLineChart'

it('renders a labelled SVG and an accessible data table from real points', () => {
  render(
    <MetricLineChart
      title="最近 24 小时请求趋势"
      valueLabel="请求数"
      points={[
        { label: '08:00', value: 2 },
        { label: '08:05', value: 5 },
      ]}
    />,
  )

  expect(screen.getByRole('img', { name: '最近 24 小时请求趋势' })).toBeVisible()
  expect(screen.getByRole('table', { name: '最近 24 小时请求趋势数据' })).toBeInTheDocument()
  expect(screen.getByRole('cell', { name: '08:05' })).toBeInTheDocument()
  expect(screen.getByRole('cell', { name: '5' })).toBeInTheDocument()
  expect(document.querySelector('path')).toHaveAttribute('d', expect.stringContaining('L'))
})

it('shows a truthful empty state without drawing a fabricated line', () => {
  render(
    <MetricLineChart
      title="最近 24 小时请求趋势"
      valueLabel="请求数"
      points={[]}
    />,
  )

  expect(screen.getByText('最近 24 小时暂无采集数据')).toBeVisible()
  expect(screen.queryByRole('img', { name: '最近 24 小时请求趋势' })).not.toBeInTheDocument()
  expect(document.querySelector('path')).not.toBeInTheDocument()
})
