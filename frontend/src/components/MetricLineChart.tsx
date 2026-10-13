export type MetricChartPoint = {
  label: string
  value: number
}

type MetricLineChartProps = {
  title: string
  valueLabel: string
  points: MetricChartPoint[]
}

const WIDTH = 640
const HEIGHT = 180
const PADDING = 18

function buildPath(points: MetricChartPoint[]) {
  const values = points.map((point) => point.value)
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const range = maximum - minimum
  const drawableWidth = WIDTH - PADDING * 2
  const drawableHeight = HEIGHT - PADDING * 2

  return points.map((point, index) => {
    const x = points.length === 1
      ? WIDTH / 2
      : PADDING + index * drawableWidth / (points.length - 1)
    const y = range === 0
      ? HEIGHT / 2
      : PADDING + (maximum - point.value) * drawableHeight / range
    return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`
  }).join(' ')
}

export function MetricLineChart({
  title,
  valueLabel,
  points,
}: MetricLineChartProps) {
  if (points.length === 0) {
    return (
      <figure className="metric-chart metric-chart--empty">
        <figcaption>{title}</figcaption>
        <p>最近 24 小时暂无采集数据</p>
      </figure>
    )
  }

  return (
    <figure className="metric-chart">
      <figcaption>{title}</figcaption>
      <svg
        role="img"
        aria-label={title}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
      >
        <line x1={PADDING} y1={HEIGHT - PADDING} x2={WIDTH - PADDING} y2={HEIGHT - PADDING} />
        <line x1={PADDING} y1={PADDING} x2={PADDING} y2={HEIGHT - PADDING} />
        <path d={buildPath(points)} />
      </svg>
      <table className="sr-only" aria-label={`${title}数据`}>
        <thead>
          <tr><th scope="col">时间</th><th scope="col">{valueLabel}</th></tr>
        </thead>
        <tbody>
          {points.map((point, index) => (
            <tr key={`${point.label}-${index}`}>
              <td>{point.label}</td>
              <td>{point.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
