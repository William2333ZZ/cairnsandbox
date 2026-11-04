'use client'
// 当前登录的人、角色与项目。令牌存在 localStorage;没有令牌或 401 时 AuthGate 显示登录页。
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { GET, getProject, getToken, setProjectKey, setToken } from './api'
import { roleOf, type Me, type Role } from './roles'

type State = { me: Me | null; role: Role; project: string; projects: string[]; loading: boolean; error: string; login: (token: string) => Promise<boolean>; logout: () => void; setProject: (p: string) => void }
const Ctx = createContext<State | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const [me, setMe] = useState<Me | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState(''), [project, setProjectState] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    if (!getToken()) {                                   // 控制面没开令牌时 /me 也能答(部署者身份),所以没有令牌也先问一次
      try { const m = await GET<Me>('/me'); setMe(m.auth ? null : m) } catch { setMe(null) }
    } else {
      try { setMe(await GET<Me>('/me')); setError('') } catch (e) { setMe(null); setError((e as Error).message) }
    }
    setLoading(false)
  }, [])
  useEffect(() => { void load(); setProjectState(getProject()) }, [load])
  useEffect(() => { const f = () => { setMe(null); setLoading(false) }; window.addEventListener('cp-unauthorized', f); return () => window.removeEventListener('cp-unauthorized', f) }, [])

  const projects = useMemo(() => me?.projects ?? [], [me])
  const current = project && (projects.length === 0 || projects.includes(project)) ? project : (projects[0] ?? '')
  const value: State = {
    me, role: roleOf(me), project: current, projects, loading, error,
    login: async (token: string) => {
      setToken(token.trim())
      try { const m = await GET<Me>('/me'); setMe(m); setError(''); qc.clear(); return true } catch (e) { setToken(''); setError((e as Error).message); return false }
    },
    logout: () => { setToken(''); setMe(null); qc.clear() },
    setProject: (p: string) => { setProjectKey(p); setProjectState(p); void qc.invalidateQueries() },
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
export function useAuth() { const v = useContext(Ctx); if (!v) throw new Error('useAuth outside AuthProvider'); return v }
