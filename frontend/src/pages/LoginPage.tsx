import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'

import { useAuth } from '../auth/context'

interface LoginLocationState { from?: string }

function returnPath(state: unknown) {
  const from = (state as LoginLocationState | null)?.from
  if (typeof from !== 'string' || from.startsWith('//')) return '/'
  try {
    const candidate = new URL(from, window.location.origin)
    const normalizedPathname = candidate.pathname.replace(/\/+$/, '') || '/'
    if (candidate.origin !== window.location.origin || normalizedPathname === '/login') return '/'
    return `${candidate.pathname}${candidate.search}${candidate.hash}`
  } catch {
    return '/'
  }
}

export function LoginPage() {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isPending, setIsPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const destination = returnPath(location.state)

  if (auth.principal) return <Navigate to={destination} replace />

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setIsPending(true)
    setError(null)
    try {
      await auth.login({ username, password })
      setPassword('')
      navigate(destination, { replace: true })
    } catch (requestError) {
      setPassword('')
      setError(requestError instanceof Error ? requestError.message : '登录失败，请稍后重试')
    } finally {
      setIsPending(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <p className="eyebrow">本地化大模型应用平台</p>
        <h1 id="login-title">演示系统登录</h1>
        <p className="login-description">使用演示账号进入业务工作台。</p>
        <aside className="login-warning" role="note">当前为 HTTP 短期演示环境，请勿输入真实账号、密码、生产配置或敏感文档。</aside>
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor="login-username">账号</label>
          <input id="login-username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required disabled={isPending} />
          <label htmlFor="login-password">密码</label>
          <input id="login-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required disabled={isPending} />
          {error && <p className="danger-text" role="alert">{error}</p>}
          <button className="primary-button login-submit" type="submit" disabled={isPending}>{isPending ? '正在登录…' : '登录'}</button>
        </form>
      </section>
    </main>
  )
}
