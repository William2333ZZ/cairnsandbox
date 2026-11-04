'use client'
// 独立外壳:顶栏(应用切换、项目切换、搜索、语言、主题、角色、退出)+ 按角色长出来的侧栏。数据变了由 SSE 触发重取。
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useEffect, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { subscribeEvents } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { useI18n } from '@/lib/i18n'
import { navFor } from '@/lib/roles'
import { useGet } from '@/lib/query'
import { Badge, Select, cx } from './ui'

function useLiveInvalidation() {
  const qc = useQueryClient()
  useEffect(() => {
    const ac = new AbortController()
    subscribeEvents(e => {
      const starts = e.type === 'sandbox' ? ['/sandboxes', '/audit', '/metering'] : e.type === 'node' ? ['/nodes', '/health'] : []
      if (starts.length) void qc.invalidateQueries({ predicate: q => starts.some(s => String(q.queryKey[0]).startsWith(s)) })
    }, ac.signal)
    return () => ac.abort()
  }, [qc])
}

function useTheme() {
  const [theme, set] = useState<'light' | 'dark'>('light')
  useEffect(() => {
    let saved = ''; try { saved = localStorage.getItem('cp_theme') || '' } catch { /* */ }
    const t = saved === 'dark' || saved === 'light' ? saved : window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    set(t); document.documentElement.dataset.theme = t
  }, [])
  return [theme, () => { const t = theme === 'light' ? 'dark' : 'light'; set(t); document.documentElement.dataset.theme = t; try { localStorage.setItem('cp_theme', t) } catch { /* */ } }] as const
}

export function Shell({ children }: { children: ReactNode }) {
  const { me, role, project, projects, setProject, logout } = useAuth(); const { t, lang, setLang } = useI18n(); const path = usePathname() || ''
  const [theme, toggleTheme] = useTheme(); useLiveInvalidation()
  const pending = useGet<{ requests: unknown[] }>('/network/requests', { every: 15000 })
  const items = navFor(role); const main = items.filter(i => i.group === 'main'), ops = items.filter(i => i.group === 'ops')
  const link = (i: { id: string; href: string }) => {
    const on = path.startsWith('/console' + i.href) || path.startsWith(i.href)
    const n = i.id === 'network' ? pending.data?.requests.length ?? 0 : 0
    return <Link key={i.id} href={i.href} className={cx('flex items-center gap-2 rounded-lg px-2 py-1 text-ui-base text-fg no-underline hover:bg-hover hover:no-underline', on && 'bg-selected font-medium')}>{t(`nav.${i.id}`)}{n > 0 && <Badge tone="warning">{n}</Badge>}</Link>
  }
  return (
    <div className="flex h-screen flex-col bg-bg">
      <header className="flex items-center gap-3 border-b border-border bg-panel px-3 py-1.5">
        <div className="flex items-center gap-1 text-ui-base font-medium">{t('app.name')}</div>
        <a href="/ui/#/w" className="text-ui-sm text-fg-subtle hover:text-fg">{t('app.switch.agent')} →</a>
        <span className="text-fg-subtlest">/</span>
        <label className="flex items-center gap-1 text-ui-sm text-fg-subtle">{t('top.project')}
          <Select value={project} onChange={e => setProject(e.target.value)} aria-label={t('top.project')} className="py-0.5 text-ui-sm">
            {projects.length === 0 ? <option value="">default</option> : projects.map(p => <option key={p} value={p}>{p}</option>)}
          </Select>
        </label>
        <div className="flex-1" />
        <button className="text-ui-sm text-fg-subtle hover:text-fg" onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}>{t('top.lang')}</button>
        <button className="text-ui-sm text-fg-subtle hover:text-fg" onClick={toggleTheme} aria-label="theme">{theme === 'light' ? '☾' : '☀'}</button>
        <Badge tone={role === 'operator' ? 'warning' : role === 'owner' ? 'info' : 'neutral'}>{t(`role.${role}`)}</Badge>
        <span className="text-ui-sm text-fg-subtle">{me?.user ?? 'admin'}</span>
        <button className="text-ui-sm text-fg-subtle hover:text-fg" onClick={logout}>{t('top.logout')}</button>
      </header>
      <div className="flex min-h-0 flex-1">
        <nav className="hidden w-52 shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border bg-sidebar p-2 md:flex" aria-label="main">
          {main.map(link)}
          {ops.length > 0 && <div className="mt-3 px-2 text-ui-xs text-fg-subtlest">{t('nav.ops')}</div>}
          {ops.map(link)}
        </nav>
        <main className="min-w-0 flex-1 overflow-y-auto p-4"><div className="mx-auto max-w-6xl space-y-4">{me?.suspended && <div role="alert" className="rounded-xl border border-destructive bg-destructive-surface px-3 py-2 text-ui-base text-destructive">{t('suspended.banner', { reason: me.suspended.reason })}</div>}{children}</div></main>
      </div>
    </div>
  )
}
