import { Outlet } from 'react-router-dom'

import { AppShell } from '../components/AppShell'

export function ShellLayout({ onLogout }: { onLogout: () => void }) {
  return <AppShell onLogout={onLogout}><Outlet /></AppShell>
}

export function PlaceholderPage({ title, description }: { title: string; description: string }) {
  return (
    <section className="placeholder-page" aria-labelledby="page-title">
      <p className="eyebrow">本地化大模型应用平台</p>
      <h1 id="page-title">{title}</h1>
      <p>{description}</p>
    </section>
  )
}
