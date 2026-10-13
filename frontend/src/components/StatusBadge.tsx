type StatusTone = 'success' | 'warning' | 'danger' | 'neutral'

export function StatusBadge({ children, tone = 'neutral' }: { children: string; tone?: StatusTone }) {
  return <span className={`status-badge status-badge--${tone}`}>{children}</span>
}
