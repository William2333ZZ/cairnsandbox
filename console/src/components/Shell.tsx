'use client'
// 外壳照工作台(web/src/layout/Shell.tsx)的样子:左侧一条侧栏(应用标记、项目、"新沙箱"、导航、底部状态),
// 窄于 1024 px 自动收成 52 px 的图标条,窄于 768 px 变抽屉;内容居中、不超过 max-w-5xl。数据变了由 SSE 触发重取。
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { useEffect, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { subscribeEvents } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { useI18n } from '@/lib/i18n'
import { navFor } from '@/lib/roles'
import { useGet } from '@/lib/query'
import { Select, cx } from './ui'

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

function useWidth() {
  const [vw, setVw] = useState(1280)
  useEffect(() => { const f = () => setVw(window.innerWidth); f(); window.addEventListener('resize', f); return () => window.removeEventListener('resize', f) }, [])
  return vw
}

function useCollapsed(auto: boolean) {
  const [pref, setPref] = useState(false)
  useEffect(() => { try { setPref(localStorage.getItem('cp_sidebar') === 'collapsed') } catch { /* */ } }, [])
  return [pref || auto, () => { const n = !pref; setPref(n); try { localStorage.setItem('cp_sidebar', n ? 'collapsed' : 'open') } catch { /* */ } }] as const
}

/** 线条图标(lucide 的几何),不引图标库。 */
const PATHS: Record<string, string> = {
  plus: 'M12 5v14M5 12h14',
  panel: 'M3 5h18v14H3zM9 5v14',
  overview: 'M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z',
  sandboxes: 'M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8',
  templates: 'M12 2 2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
  volumes: 'M22 12H2M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11zM6 16h.01M10 16h.01',
  network: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18',
  usage: 'M3 3v18h18M7 16v-5M12 16V7M17 16v-3',
  members: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8',
  audit: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  nodes: 'M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01',
  admin: 'M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.8 7.8 5.5 5.5 0 0 1 7.8-7.8zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4',
  agent: 'M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z',
  moon: 'M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z',
  sun: 'M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
}
export function Icon({ name, className }: { name: string; className?: string }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" className={cx('size-4 shrink-0', className)} aria-hidden="true"><path d={PATHS[name] || PATHS.overview} /></svg>
}

/** 应用标记:三块叠起来的石头(CairnSandbox 的标志),只用前景色阶,品牌色只给最上面那一粒。 */
function Mark() {
  return (
    <svg viewBox="0 0 24 24" className="size-6 shrink-0" aria-hidden="true">
      <ellipse cx="12" cy="20" rx="9" ry="2.6" className="fill-fg-subtlest" />
      <ellipse cx="13" cy="15" rx="6.5" ry="2.3" className="fill-fg-subtle" />
      <ellipse cx="11.5" cy="10.5" rx="4.5" ry="2" className="fill-fg" />
      <circle cx="12" cy="6" r="1.6" className="fill-brand" />
    </svg>
  )
}

function Health({ collapsed }: { collapsed: boolean }) {
  const { t } = useI18n()
  const h = useGet<{ ok?: boolean; nodes?: Record<string, { alive?: boolean }> }>('/health', { every: 15000 })
  const nodes = Object.values(h.data?.nodes ?? {}); const alive = nodes.filter(n => n.alive).length
  const ok = !!h.data?.ok && nodes.length > 0 && alive === nodes.length
  const label = h.error ? t('nav.unreachable') : t('nav.nodes.count', { alive, total: nodes.length })
  if (collapsed) return <div className="py-1 text-center" title={label}><span className={cx('inline-block size-1.5 rounded-full', ok ? 'bg-success' : 'bg-destructive')} /></div>
  return <div className="flex items-center gap-2 px-2.5 py-1 text-ui-xs text-fg-subtlest"><span className={cx('inline-block size-1.5 rounded-full', ok ? 'bg-success' : 'bg-destructive')} />{h.error ? <span className="text-destructive">{label}</span> : label}</div>
}

export function Shell({ children }: { children: ReactNode }) {
  const { me, role, project, projects, setProject, logout } = useAuth(); const { t, lang, setLang } = useI18n(); const path = usePathname() || ''
  const [theme, toggleTheme] = useTheme(); useLiveInvalidation()
  const vw = useWidth(); const narrow = vw < 768, auto = vw < 1024
  const [collapsed, toggleCollapsed] = useCollapsed(auto)
  const [drawer, setDrawer] = useState(false)
  useEffect(() => { setDrawer(false) }, [path])
  const pending = useGet<{ requests: unknown[] }>('/network/requests', { every: 15000 })
  const items = navFor(role); const main = items.filter(i => i.group === 'main'), ops = items.filter(i => i.group === 'ops')

  const link = (i: { id: string; href: string }, full: boolean) => {
    const on = path.startsWith('/console' + i.href) || path.startsWith(i.href)
    const n = i.id === 'network' ? pending.data?.requests.length ?? 0 : 0
    return (
      <Link key={i.id} href={i.href} title={t(`nav.${i.id}`)} className={cx('flex h-7 items-center gap-2 rounded-md px-2 text-ui-sm text-fg-subtle no-underline hover:bg-hover hover:text-fg hover:no-underline', on && 'bg-selected text-fg', !full && 'w-8 justify-center px-0')}>
        <Icon name={i.id} className="size-3.5" />
        {full && <span className="flex-1 truncate">{t(`nav.${i.id}`)}</span>}
        {full && n > 0 && <span className="rounded-full bg-warning-surface px-1.5 text-ui-xs text-warning">{n}</span>}
      </Link>
    )
  }

  const side = (full: boolean) => (
    <>
      <div className={cx('flex items-center gap-2 px-3 pb-2 pt-3', !full && 'justify-center px-0')}>
        {full && <Link href="/overview/" className="flex items-center gap-2 text-ui-base font-medium text-fg no-underline hover:no-underline"><Mark />CairnSandbox <span className="font-normal text-fg-subtle">{t('app.name')}</span></Link>}
        {full && <div className="flex-1" />}
        {!auto && <button className="rounded-lg px-1.5 py-1 text-fg-subtle hover:bg-hover hover:text-fg" onClick={toggleCollapsed} title={t('nav.collapse')} aria-label={t('nav.collapse')}><Icon name="panel" /></button>}
        {!full && auto && <Mark />}
      </div>
      {full && (
        <div className="px-2 pb-2">
          <Select value={project} onChange={e => setProject(e.target.value)} aria-label={t('top.project')} className="w-full">
            {projects.length === 0 ? <option value="">default</option> : projects.map(p => <option key={p} value={p}>{p}</option>)}
          </Select>
        </div>
      )}
      <div className={cx('px-2', !full && 'px-1.5')}>
        <Link href="/sandboxes/" title={t('nav.new')} className={cx('flex h-8 items-center gap-2 rounded-lg border border-border bg-panel px-2.5 text-ui-base text-fg no-underline hover:bg-hover hover:no-underline', !full && 'justify-center px-0')}><Icon name="plus" />{full && <span>{t('nav.new')}</span>}</Link>
      </div>
      <nav className={cx('flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto p-2 pt-3', !full && 'items-center px-1.5')} aria-label="main">
        {main.map(i => link(i, full))}
        {ops.length > 0 && full && <div className="px-2.5 pb-1 pt-3 text-ui-xs font-medium text-fg-subtlest">{t('nav.ops')}</div>}
        {ops.length > 0 && !full && <div className="my-1 h-px w-6 bg-border" />}
        {ops.map(i => link(i, full))}
      </nav>
      <div className={cx('flex flex-col gap-0.5 border-t border-border p-2', !full && 'items-center px-1.5')}>
        <a href="/ui/#/w" title={t('app.switch.agent')} className={cx('flex h-7 items-center gap-2 rounded-md px-2 text-ui-sm text-fg-subtle no-underline hover:bg-hover hover:text-fg hover:no-underline', !full && 'w-8 justify-center px-0')}><Icon name="agent" className="size-3.5" />{full && <span className="flex-1 truncate">{t('app.switch.agent')}</span>}{full && <span aria-hidden="true">→</span>}</a>
        <Health collapsed={!full} />
        {full ? (
          <div className="flex items-center gap-1 px-1.5 pt-1 text-ui-xs text-fg-subtlest">
            <span className="min-w-0 flex-1 truncate" title={me?.suspended ? me.suspended.reason : undefined}>{me?.user ?? 'admin'} · {t(`role.${role}`)}</span>
            <button className="rounded px-1 hover:bg-hover hover:text-fg" onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}>{t('top.lang')}</button>
            <button className="rounded p-1 hover:bg-hover hover:text-fg" onClick={toggleTheme} aria-label="theme" title="theme"><Icon name={theme === 'light' ? 'moon' : 'sun'} className="size-3.5" /></button>
            <button className="rounded p-1 hover:bg-hover hover:text-fg" onClick={logout} aria-label={t('top.logout')} title={t('top.logout')}><Icon name="logout" className="size-3.5" /></button>
          </div>
        ) : (
          <>
            <button className="grid size-8 place-items-center rounded-md text-fg-subtle hover:bg-hover hover:text-fg" onClick={toggleTheme} aria-label="theme" title="theme"><Icon name={theme === 'light' ? 'moon' : 'sun'} className="size-3.5" /></button>
            <button className="grid size-8 place-items-center rounded-md text-fg-subtle hover:bg-hover hover:text-fg" onClick={logout} aria-label={t('top.logout')} title={t('top.logout')}><Icon name="logout" className="size-3.5" /></button>
          </>
        )}
      </div>
    </>
  )

  const leftW = narrow ? 0 : collapsed ? 52 : 248
  return (
    <div className="flex h-screen w-full overflow-hidden bg-bg">
      {!narrow && <aside style={{ width: leftW }} className="flex shrink-0 flex-col bg-sidebar">{side(!collapsed)}</aside>}
      {narrow && drawer && <div className="fixed inset-0 z-20 bg-bg/80" onClick={() => setDrawer(false)}><aside className="flex h-full w-72 flex-col bg-sidebar" onClick={e => e.stopPropagation()}>{side(true)}</aside></div>}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {narrow && <div className="flex items-center gap-2 px-2 py-1"><button className="rounded-lg px-2 py-1 hover:bg-hover" onClick={() => setDrawer(true)} aria-label="menu">☰</button><span className="text-ui-base font-medium">CairnSandbox</span></div>}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-5xl space-y-5 px-6 py-6">
            {me?.suspended && <div role="alert" className="rounded-xl border border-destructive bg-destructive-surface px-3 py-2 text-ui-base text-destructive">{t('suspended.banner', { reason: me.suspended.reason })}</div>}
            {children}
          </div>
        </div>
      </main>
    </div>
  )
}
