'use client'
import { QueryClient, QueryClientProvider, useQuery, type UseQueryOptions } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { GET } from './api'
import { useAuth } from './auth'

export function QueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { staleTime: 4000, retry: 1, refetchOnWindowFocus: true } } }))
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

/** GET 一个路径;键里带项目,切项目时自动重取。path 为 null 时不发请求。 */
export function useGet<T>(path: string | null, opts: Partial<UseQueryOptions<T>> & { every?: number } = {}) {
  const { project } = useAuth()
  const { every, ...rest } = opts
  return useQuery<T>({ queryKey: [path, project], queryFn: ({ signal }) => GET<T>(path as string, signal), enabled: path !== null, refetchInterval: every, ...(rest as object) })
}
