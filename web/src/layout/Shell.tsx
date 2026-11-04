// Shell (docs/008): left sidebar in ZCode's shape — app mark, "新任务", the task list a plugin puts in the `sidebar` slot, and the
// management pages as a small group at the bottom; center object; right dock opened by `?rail=`. The shell renders only
// what the kernel registry holds: nav entries, routes, rail tabs, slots.
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { registry, useRegistry, type NavEntry } from '../kernel'
import { useStore, type Store } from '../store'
import { Button, Input, cx } from '../ui'
import { login, me, token, setToken } from '../api/client'

export function useHash() {
  const [hash, setHash] = useState(location.hash || '#/w')
  useEffect(() => { const f = () => setHash(location.hash || '#/w'); window.addEventListener('hashchange', f); return () => window.removeEventListener('hashchange', f) }, [])
  return hash
}
export function navigate(hash: string) { location.hash = hash }

/** Right-dock request: `#/ticket/x?rail=events&sandbox=sb1` -> tab "events" with args. */
function railFromHash(hash: string): { tab: string | null; args: Record<string, string> } {
  const q = hash.split('?')[1]; if (!q) return { tab: null, args: {} }
  const p = new URLSearchParams(q); const tab = p.get('rail'); const args: Record<string, string> = {}; p.forEach((v, k) => { if (k !== 'rail') args[k] = v })
  return { tab, args }
}

/** Small line icons (lucide geometry) for the shell; no icon library, no brand marks. */
const PATHS: Record<string, string> = {
  plus: 'M12 5v14M5 12h14',
  panel: 'M3 5h18v14H3zM9 5v14',
  attention: 'M22 12h-6l-2 3h-4l-2-3H2M5.5 5h13l3.5 7v7H2v-7z',
  intake: 'M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9',
  sandboxes: 'M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8M12 13v8',
  eval: 'M9 3h6M10 3v6L4 20h16l-6-11V3',
  versions: 'M12 3v6M12 15v6M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  nodes: 'M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  session: 'M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z',
  routines: 'M12 6v6l4 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
  agents: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8',
  connectors: 'M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.5 1.5M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.5-1.5',
  memory: 'M12 3a4 4 0 0 0-4 4v1a3 3 0 0 0-2 5 3 3 0 0 0 1 5v1a3 3 0 0 0 5 2 3 3 0 0 0 5-2v-1a3 3 0 0 0 1-5 3 3 0 0 0-2-5V7a4 4 0 0 0-4-4zM12 3v18',
  close: 'M18 6 6 18M6 6l12 12',
}
export function Icon({ name, className }: { name: string; className?: string }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" className={cx('size-4 shrink-0', className)} aria-hidden="true"><path d={PATHS[name] || PATHS.session} /></svg>
}

/** Shown when the control plane answers 401: the one-user login of the first phase is a token (docs/009 鉴权与隔离). */
function Login() {
  const [need, setNeed] = useState(false); const [t, setT] = useState(''); const [bad, setBad] = useState(false)
  useEffect(() => { const f = () => setNeed(true); window.addEventListener('cp-unauthorized', f); return () => window.removeEventListener('cp-unauthorized', f) }, [])
  if (!need) return null
  const go = async () => { if (await login(t.trim())) { setNeed(false); location.reload() } else setBad(true) }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80">
      <div className="w-80 rounded-2xl border border-border bg-popover p-5 shadow-composer">
        <h2 className="text-ui-lg font-medium">登录</h2>
        <p className="mt-1 text-ui-sm text-fg-subtle">{token() ? '令牌不对或已更换。' : '这台控制面要令牌。'}它在部署机的密钥目录 cp.token 里。</p>
        <Input className="mt-3 w-full" type="password" value={t} onChange={e => { setT(e.target.value); setBad(false) }} onKeyDown={e => { if (e.key === 'Enter') void go() }} placeholder="令牌" autoFocus />
        {bad && <p className="mt-1 text-ui-sm text-destructive">控制面不认这个令牌。</p>}
        <div className="mt-3 flex justify-end"><Button kind="primary" disabled={!t.trim()} onClick={go}>进入</Button></div>
      </div>
    </div>
  )
}

export function Shell() {
  useRegistry()
  const hash = useHash()
  const path = hash.split('?')[0]
  const sidebar = useStore(s => s.sidebar), toggleSidebar = useStore(s => s.toggleSidebar)
  const theme = useStore(s => s.theme)
  const rightWidth = useStore(s => s.rightWidth), setRightWidth = useStore(s => s.setRightWidth)
  const [vw, setVw] = useState(window.innerWidth)
  useEffect(() => { const f = () => setVw(window.innerWidth); window.addEventListener('resize', f); return () => window.removeEventListener('resize', f) }, [])
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const apply = () => { document.documentElement.dataset.theme = theme === 'dark' || (theme === 'system' && mq.matches) ? 'dark' : 'light' }
    apply(); mq.addEventListener('change', apply); return () => mq.removeEventListener('change', apply)
  }, [theme])

  const safeDecode = (s: string | undefined) => { if (s == null) return ''; try { return decodeURIComponent(s) } catch { return s } }      // unmatched optional group -> '' (never the string "undefined"); a malformed escape must not take the shell down
  const route = useMemo(() => { for (const r of registry.routes) { const m = path.match(r.pattern); if (m) return { r, params: m.slice(1).map(safeDecode) } } return null }, [path, registry.version])
  const { tab, args } = railFromHash(hash)
  const rail = tab ? registry.rails.find(t => t.id === tab) : null
  const narrow = vw < 768, auto = vw < 1024
  const collapsed = sidebar === 'collapsed' || auto
  const [drawer, setDrawer] = useState(false)
  useEffect(() => { setDrawer(false) }, [path])
  const navs = [...registry.nav].filter(n => n.id !== 'session').sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
  const slots = registry.slots.filter(s => s.slot === 'sidebar').sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
  const dragging = useRef(false)
  const onDrag = (e: React.MouseEvent) => {
    dragging.current = true; e.preventDefault()
    const move = (ev: MouseEvent) => { if (!dragging.current) return; const w = Math.min(vw * 0.7, Math.max(300, vw - ev.clientX)); setRightWidth(w) }
    const up = () => { dragging.current = false; window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up)
  }
  const rw = rail ? Math.min(vw * 0.7, Math.max(300, rightWidth || vw * 0.45)) : 0
  const leftW = narrow ? 0 : collapsed ? 52 : 272

  const side = (full: boolean) => (
    <>
      <div className={cx('flex items-center gap-2 px-3 pt-3 pb-2', !full && 'justify-center px-0')}>
        {full && <a href="#/w" className="flex items-center gap-2 text-ui-base font-medium text-fg"><span className="grid size-6 place-items-center rounded-md bg-brand text-ui-xs font-semibold text-fg-inverse">fc</span>工作台</a>}
        {full && <div className="flex-1" />}
        {!auto && <Button kind="ghost" size="sm" onClick={toggleSidebar} title="收起/展开侧栏 (Mod+B)" className="px-1.5"><Icon name="panel" /></Button>}
      </div>
      {full && registry.slots.filter(x => x.slot === 'sidebar-head').map(x => <x.component key={x.id} />)}
      <div className={cx('px-2', !full && 'px-1.5')}>
        <a href="#/w?new=1" title="新任务" className={cx('flex h-8 items-center gap-2 rounded-lg border border-border bg-panel px-2.5 text-ui-base text-fg hover:bg-hover', !full && 'justify-center px-0')}><Icon name="plus" />{full && <span>新任务</span>}</a>
      </div>
      <div className="min-h-0 flex-1 overflow-auto pt-2">
        {slots.map(s => <s.component key={s.id} collapsed={!full} />)}
      </div>
      <nav className={cx('flex flex-col gap-0.5 border-t border-border p-2', !full && 'items-center px-1.5')}>
        {full && <div className="px-2.5 pb-1 pt-1 text-ui-xs font-medium text-fg-subtlest">管理</div>}
        {navs.map(n => <NavItem key={n.id} n={n} active={path.startsWith(n.href.split('?')[0]) || (route?.r.nav === n.id)} showLabel={full} />)}
        <Health collapsed={!full} />
        <Who collapsed={!full} />
      </nav>
    </>
  )

  return (
    <div className="flex h-full w-full overflow-hidden">
      <Login />
      {!narrow && <aside style={{ width: leftW }} className="flex shrink-0 flex-col bg-sidebar">{side(!collapsed)}</aside>}
      {narrow && drawer && <div className="fixed inset-0 z-20 bg-bg/80" onClick={() => setDrawer(false)}><aside className="flex h-full w-72 flex-col bg-sidebar" onClick={e => e.stopPropagation()}>{side(true)}</aside></div>}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-bg">
        {narrow && <div className="flex items-center gap-2 px-2 py-1"><Button kind="ghost" size="sm" onClick={() => setDrawer(true)}>☰</Button><span className="text-ui-base font-medium">fc 工作台</span></div>}
        <div className="min-h-0 flex-1 overflow-auto">
          <PageBoundary key={path}>{route ? <route.r.component params={route.params} /> : <p className="p-4 text-ui-base text-fg-subtle">没有这个页面:{path}</p>}</PageBoundary>
        </div>
      </main>
      {rail && (
        <aside style={{ width: rw }} className="relative flex shrink-0 flex-col border-l border-border bg-panel">
          <div onMouseDown={onDrag} className="absolute left-0 top-0 h-full w-1 cursor-col-resize hover:bg-link" />
          <header className="flex items-center gap-2 border-b border-border px-3 py-1.5"><span className="text-ui-caption font-medium text-fg-subtle">{rail.label}</span><div className="flex-1" /><Button kind="ghost" size="sm" onClick={() => navigate(path)} title="关闭右栏" className="px-1.5"><Icon name="close" className="size-3.5" /></Button></header>
          <div className="min-h-0 flex-1 overflow-auto"><rail.component args={args} /></div>
        </aside>
      )}
    </div>
  )
}

function NavItem({ n, active, showLabel }: { n: NavEntry; active: boolean; showLabel: boolean }) {
  const badge = useStore(n.badge ? (s => n.badge!(s as Store)) : () => null)        // subscribed: counts update while the user is on another page
  return <a href={n.href} title={n.label} className={cx('flex h-7 items-center gap-2 rounded-md px-2 text-ui-sm text-fg-subtle hover:bg-hover hover:text-fg', active && 'bg-selected text-fg', !showLabel && 'w-8 justify-center px-0')}>
    <Icon name={n.id} className="size-3.5" />
    {showLabel && <span className="flex-1 truncate">{n.label}</span>}
    {showLabel && badge != null && badge !== 0 && badge !== '' && <span className="rounded-full bg-hover px-1.5 text-ui-xs">{badge}</span>}
  </a>
}

class PageBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() { return this.state.error ? <p className="m-4 rounded-lg border border-destructive bg-destructive-surface px-3 py-2 text-ui-base text-destructive">这一页出错了:{String(this.state.error.message || this.state.error)}</p> : this.props.children }
}

function Who({ collapsed }: { collapsed: boolean }) {
  const [m, setM] = useState<{ user: string | null; admin: boolean; auth: boolean } | null>(null)
  useEffect(() => { me().then(setM).catch(() => setM(null)) }, [])
  if (!m || !m.auth || collapsed) return null
  return <div className="flex items-center gap-2 px-2.5 py-1 text-ui-xs text-fg-subtlest"><span>{m.admin ? '部署者' : m.user}</span><button className="hover:text-fg" onClick={() => { setToken(''); location.reload() }}>退出</button></div>
}

function Health({ collapsed }: { collapsed: boolean }) {
  const h = useStore(s => s.health), live = useStore(s => s.live), load = useStore(s => s.load)
  useEffect(() => { void load.health(); const id = setInterval(() => void load.health(), live ? 30000 : 8000); return () => clearInterval(id) }, [live])
  const nodes = Object.entries(h.data?.nodes ?? {}); const alive = nodes.filter(([, n]) => n.alive).length
  const ok = h.data?.ok && alive === nodes.length && nodes.length > 0
  if (collapsed) return <div className="py-1 text-center" title={h.error || `节点 ${alive}/${nodes.length}`}><span className={cx('inline-block size-1.5 rounded-full', ok ? 'bg-success' : 'bg-destructive')} /></div>
  return <div className="flex items-center gap-2 px-2.5 py-1 text-ui-xs text-fg-subtlest"><span className={cx('inline-block size-1.5 rounded-full', ok ? 'bg-success' : 'bg-destructive')} />{h.error ? <span className="text-destructive">控制面不可达</span> : <>节点 {alive}/{nodes.length}{live ? ' · 实时' : ''}</>}</div>
}
