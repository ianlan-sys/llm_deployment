import { createContext, useContext } from 'react'

import type { AuthLoginRequest, AuthPrincipal } from '../api/types'

export interface AuthContextValue {
  principal: AuthPrincipal | null
  isLoading: boolean
  login: (payload: AuthLoginRequest) => Promise<AuthPrincipal>
  logout: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
