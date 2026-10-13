import { useEffect, useMemo, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { AuthLoginRequest, AuthPrincipal } from '../api/types'
import { AuthContext, type AuthContextValue } from './context'
import { onSessionInvalidated } from './sessionInvalidation'

const authSessionKey = ['auth-session'] as const

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  useEffect(() => onSessionInvalidated(() => {
    void queryClient.cancelQueries({ queryKey: authSessionKey }, { silent: true })
    queryClient.setQueryData<AuthPrincipal | null>(authSessionKey, null)
  }), [queryClient])
  const sessionQuery = useQuery<AuthPrincipal | null>({
    queryKey: authSessionKey,
    queryFn: () => api.get<AuthPrincipal>('/api/auth/session'),
    retry: false,
    refetchOnWindowFocus: false,
  })
  const loginMutation = useMutation({
    mutationFn: (payload: AuthLoginRequest) => api.post<AuthPrincipal>('/api/auth/login', payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: authSessionKey })
    },
  })
  const logoutMutation = useMutation({
    mutationFn: () => api.post<void>('/api/auth/logout'),
    onSettled: () => {
      queryClient.setQueryData<AuthPrincipal | null>(authSessionKey, null)
    },
  })
  const value = useMemo<AuthContextValue>(() => ({
    principal: sessionQuery.data ?? null,
    isLoading: sessionQuery.isPending,
    login: (payload) => loginMutation.mutateAsync(payload),
    logout: () => logoutMutation.mutateAsync(),
  }), [loginMutation, logoutMutation, sessionQuery.data, sessionQuery.isPending])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
