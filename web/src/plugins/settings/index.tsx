// 设置: appearance, control-plane token, loaded plugins.
import { useState, useEffect } from 'react'
import type { Plugin } from '../../kernel'
import { registry, useRegistry } from '../../kernel'
import { DEL, GET, POST, me } from '../../api/client'
import { useStore } from '../../store'
import { setToken, token } from '../../api/client'
import { Button, Input, Panel, Select, Table } from '../../ui'

interface UserRow { user: string; name?: string; created?: number }
function UsersPanel() {
  const [admin, setAdmin] = useState(false); const [rows, setRows] = useState<UserRow[]>([]); const [u, setU] = useState(''); const [n, setN] = useState(''); const [issued, setIssued] = useState<{ user: string; token: string } | null>(null); const [err, setErr] = useState('')
  const load = async () => { try { setRows((await GET<{ users: UserRow[] }>('/users')).users) } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { me().then(m => { setAdmin(m.admin && m.auth); if (m.admin && m.auth) void load() }).catch(() => undefined) }, [])
  if (!admin) return null
  const add = async () => { try { const r = await POST<{ user: string; token: string }>('/users', { user: u.trim(), name: n.trim() }); setIssued(r); setU(''); setN(''); setErr(''); await load() } catch (e) { setErr((e as Error).message) } }
  return (
    <Panel title="用户">
      <p className="mb-2 text-ui-sm text-fg-subtle">每个用户有自己的 agent(对话、例行、记忆、策略),共用沙箱与代码托管。令牌只显示一次,交给对方登录。</p>
      {err && <p className="mb-2 text-ui-sm text-destructive">{err}</p>}
      {rows.map(r => <div key={r.user} className="flex items-center gap-3 py-1 text-ui-base"><span className="font-mono">{r.user}</span><span className="text-fg-subtle">{r.name}</span><div className="flex-1" /><button className="text-ui-sm text-fg-subtle hover:text-destructive" onClick={async () => { if (window.confirm(`删除用户 ${r.user}?`)) { await DEL(`/users/${r.user}`); await load() } }}>删除</button></div>)}
      <div className="mt-2 flex gap-2"><Input className="w-40" value={u} onChange={e => setU(e.target.value)} placeholder="用户名(小写)" /><Input className="flex-1" value={n} onChange={e => setN(e.target.value)} placeholder="显示名" /><Button kind="primary" disabled={!u.trim()} onClick={add}>新建</Button></div>
      {issued && <div className="mt-2 rounded-lg border border-border bg-surface px-3 py-2 text-ui-sm"><span className="text-fg">{issued.user} 的令牌(只显示这一次):</span> <code className="inline select-all">{issued.token}</code></div>}
    </Panel>
  )
}

function SettingsPage() {
  useRegistry()
  const theme = useStore(s => s.theme), setTheme = useStore(s => s.setTheme)
  const [tok, setTok] = useState(token()); const [saved, setSaved] = useState(false)
  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <h1 className="text-ui-xl font-semibold">设置</h1>
      {registry.slots.filter(x => x.slot === 'settings').sort((a, b) => (a.order ?? 50) - (b.order ?? 50)).map(x => <x.component key={x.id} />)}
      <UsersPanel />
      <Panel title="外观"><label className="flex items-center gap-2 text-ui-base">主题<Select value={theme} onChange={e => setTheme(e.target.value as 'light' | 'dark' | 'system')}><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></Select></label></Panel>
      <Panel title="控制面" actions={tok !== token() && <Button size="sm" kind="primary" onClick={() => { setToken(tok); setSaved(true); setTimeout(() => location.reload(), 300) }}>保存</Button>}>
        <label className="flex items-center gap-2 text-ui-base">Token<Input type="password" className="w-72" value={tok} onChange={e => setTok(e.target.value)} placeholder="CP_TOKEN(未设置时留空)" /></label>
        {saved && <p className="mt-1 text-ui-sm text-fg-subtle">已保存,重新加载。</p>}
      </Panel>
      <Panel title="插件"><Table cols={[{ h: '插件', cell: (p: [string, unknown]) => p[0] }, { h: '状态', cell: () => <span className="text-success">已激活</span> }]} rows={[...registry.active.entries()]} keyOf={p => p[0]} />
        <p className="mt-2 text-ui-xs text-fg-subtle">导航 {registry.nav.length} · 路由 {registry.routes.length} · 右栏面板 {registry.rails.length} · 命令 {registry.commands.length}</p></Panel>
    </div>
  )
}

export const settings: Plugin = { id: 'settings', activate(ctx) { ctx.nav({ id: 'settings', label: '设置', href: '#/settings', order: 90 }); ctx.route({ id: 'settings', pattern: /^#\/settings$/, component: SettingsPage, nav: 'settings' }) } }
