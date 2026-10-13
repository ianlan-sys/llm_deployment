import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

import { Disclaimer } from './Disclaimer'

const navigationItems = [
  { to: '/', label: '工作总览', icon: '▦', end: true },
  { to: '/calls', label: '坐席辅助', icon: '☎' },
  { to: '/knowledge', label: '运维知识库', icon: '◈', end: true },
  { to: '/knowledge/documents', label: '知识资料管理', icon: '▧' },
  { to: '/reviews', label: '文档审核', icon: '▤' },
  { to: '/monitoring', label: '本地模型监控', icon: '⌁' },
  { to: '/settings', label: '运行与设置', icon: '⚙' },
]

export function AppShell({ children, onLogout }: { children: ReactNode; onLogout: () => void }) {
  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="主导航">
        <a className="brand" href="/" aria-label="本地化大模型应用平台首页">
          <span className="brand-mark" aria-hidden="true">A1</span>
          <span className="brand-label">本地化大模型应用平台</span>
        </a>
        <nav className="navigation" aria-label="业务导航">
          {navigationItems.map(({ to, label, icon, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => `navigation-link${isActive ? ' is-active' : ''}`}>
              <span className="navigation-icon" aria-hidden="true">{icon}</span>
              <span className="navigation-label">{label}</span>
            </NavLink>
          ))}
        </nav>
        <button className="logout-button" type="button" onClick={onLogout}>退出登录</button>
      </aside>
      <div className="app-content">
        <main>{children}</main>
        <Disclaimer />
      </div>
    </div>
  )
}
