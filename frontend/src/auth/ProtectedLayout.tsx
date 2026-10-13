import { Navigate, useLocation, useNavigate } from 'react-router-dom'

import { ShellLayout } from '../app/routes'
import { useAuth } from './context'

export function ProtectedLayout() {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()

  if (auth.isLoading) return <section className="page-state" aria-live="polite">正在验证登录状态…</section>
  if (!auth.principal) return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}${location.hash}` }} />

  const logout = async () => {
    try {
      await auth.logout()
    } finally {
      navigate('/login', { replace: true })
    }
  }

  return <ShellLayout onLogout={() => void logout()} />
}
