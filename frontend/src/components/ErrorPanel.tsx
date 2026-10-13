export function ErrorPanel({ title = '加载失败', detail, onRetry }: { title?: string; detail?: string; onRetry?: () => void }) {
  return (
    <section className="error-panel" role="alert">
      <h2>{title}</h2>
      {detail && <p>{detail}</p>}
      {onRetry && <button type="button" onClick={onRetry}>重试</button>}
    </section>
  )
}
