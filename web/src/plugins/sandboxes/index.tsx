// 沙箱: list, create, detail (exec, pause/resume/fork/pack/delete); the terminal/files/events rails come from the rails plugin.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { useStore } from '../../store'
import { Button, Confirm, ErrorLine, Input, KV, Panel, Select, Table, age, when } from '../../ui'
import { DEL, GET, POST, token } from '../../api/client'
import { SandboxBadge } from '../../ui/domain'
import type { Sandbox } from '../../api/types'
import { navigate } from '../../layout/Shell'

function SandboxesPage() {
  const s = useStore(s => s.sandboxes), load = useStore(s => s.load), act = useStore(s => s.act), live = useStore(s => s.live)
  const [tpl, setTpl] = useState('debian-13'), [mem, setMem] = useState('512'), [cls, setCls] = useState('persistent'), [hosts, setHosts] = useState(''), [err, setErr] = useState('')
  useEffect(() => { void load.sandboxes(); const i = setInterval(() => void load.sandboxes(), live ? 30000 : 5000); return () => clearInterval(i) }, [live])
  const rows = s.data ? s.data.filter(x => x.status !== 'deleted') : null
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-ui-xl font-semibold">沙箱</h1>
      {s.error && <ErrorLine>{s.error}</ErrorLine>}
      <Panel title="新建">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col text-ui-sm text-fg-subtle">模板<Input className="w-32" value={tpl} onChange={e => setTpl(e.target.value)} /></label>
          <label className="flex flex-col text-ui-sm text-fg-subtle">内存 MiB<Input className="w-20" value={mem} onChange={e => setMem(e.target.value)} /></label>
          <label className="flex flex-col text-ui-sm text-fg-subtle">类别<Select value={cls} onChange={e => setCls(e.target.value)}><option value="persistent">persistent</option><option value="ephemeral">ephemeral</option></Select></label>
          <label className="flex flex-col text-ui-sm text-fg-subtle">允许的域名<Input className="w-64" value={hosts} onChange={e => setHosts(e.target.value)} placeholder="fc-model, pypi.org" /></label>
          <Button kind="primary" onClick={async () => { setErr(''); try { const r = await act.createSandbox({ template: tpl, mem: Number(mem) || 512, class: cls, ...(hosts.trim() ? { network: { allow_hosts: hosts.split(/[,\s]+/).filter(Boolean) } } : {}) }); navigate(`#/sandboxes/${r.id}`) } catch (e) { setErr((e as Error).message) } }}>创建</Button>
          {err && <span className="text-ui-sm text-destructive">{err}</span>}
        </div>
      </Panel>
      <Table cols={[{ h: 'ID', cell: (x: Sandbox) => <a href={`#/sandboxes/${x.id}`} className="font-mono">{x.id}</a> }, { h: '状态', cell: (x: Sandbox) => <SandboxBadge s={x.status} /> }, { h: '类别', cell: (x: Sandbox) => x.class || '-' }, { h: '模板', cell: (x: Sandbox) => x.template || '-' },
        { h: '层', cell: (x: Sandbox) => (x.layers || []).length }, { h: '规格', cell: (x: Sandbox) => `${x.vcpu ?? '-'} vCPU · ${x.mem ?? '-'} MiB` }, { h: '节点', cell: (x: Sandbox) => x.node || '-' }, { h: '启动', cell: (x: Sandbox) => x.boot_mode || '-' }, { h: '创建', cell: (x: Sandbox) => age(x.created) }]}
        rows={rows} keyOf={x => x.id} onRow={x => navigate(`#/sandboxes/${x.id}`)} empty="没有沙箱" />
    </div>
  )
}

type Ckpt = { name: string; note?: string; created: number; paused_s?: number; chain_len?: number; upper_mib?: number }

function Checkpoints({ id, running }: { id: string; running: boolean }) {
  const [list, setList] = useState<Ckpt[] | null>(null); const [err, setErr] = useState(''); const [name, setName] = useState(''); const [busy, setBusy] = useState('')
  const load = async () => { try { setList((await GET<{ checkpoints: Ckpt[] }>(`/sandboxes/${id}/checkpoints`)).checkpoints); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load() }, [id])
  const run = async (label: string, f: () => Promise<unknown>) => { setBusy(label); try { await f(); setErr('') } catch (e) { setErr((e as Error).message) } finally { setBusy(''); await load() } }
  return (
    <Panel title="检查点" actions={<div className="flex gap-1"><Input className="w-32" value={name} onChange={e => setName(e.target.value)} placeholder="名字(可空)" /><Button size="sm" kind="primary" disabled={!!busy || !running} onClick={() => run('take', () => POST(`/sandboxes/${id}/checkpoints`, name.trim() ? { name: name.trim() } : {}).then(() => setName('')))}>打一个</Button></div>}>
      <p className="mb-2 text-ui-sm text-fg-subtle">运行中打,几百毫秒停顿;回滚原地回到那一刻(id、目录、网络不变);也可以从某个检查点 fork 出一台新的,这台不动。随 bundle 迁移。</p>
      {err && <ErrorLine>{err}</ErrorLine>}
      <Table cols={[{ h: '名字', cell: (c: Ckpt) => <span className="font-mono">{c.name}</span> }, { h: '说明', cell: c => c.note || '-' }, { h: '链', cell: c => c.chain_len ?? '-' }, { h: '盘 MiB', cell: c => c.upper_mib ?? '-' }, { h: '', cell: c => age(c.created) },
        { h: '', cell: c => <span className="flex gap-1"><Button size="sm" disabled={!!busy} onClick={() => { if (window.confirm(`回滚到 ${c.name}?之后的改动会丢`)) void run('rb', () => POST(`/sandboxes/${id}/rollback`, { checkpoint: c.name })) }}>回滚</Button><Button size="sm" disabled={!!busy} onClick={() => run('fork', async () => { const r = await POST<{ clones?: { id: string }[] }>(`/sandboxes/${id}/fork`, { n: 1, checkpoint: c.name, class: 'persistent' }); const f = r.clones?.[0]; if (f) navigate(`#/sandboxes/${f.id}`) })}>从这里 fork</Button><button className="text-ui-sm text-fg-subtle hover:text-destructive" onClick={() => { if (window.confirm(`删除检查点 ${c.name}?`)) void run('del', () => DEL(`/sandboxes/${id}/checkpoints/${c.name}`)) }}>删</button></span> }]}
        rows={list} keyOf={c => c.name} empty="还没有检查点" />
    </Panel>
  )
}

function Ports({ id }: { id: string }) {
  const [port, setPort] = useState('8080'); const [out, setOut] = useState<string>('')
  const base = `${window.location.origin}/sandboxes/${id}/ports/${port}/`
  return (
    <Panel title="端口">
      <p className="mb-2 text-ui-sm text-fg-subtle">沙箱里起的服务经节点反向代理;WebSocket 握手后是字节隧道。地址要带工作台的 Bearer 头,所以浏览器直接打开不行,用 curl 或 SDK。</p>
      <div className="flex flex-wrap items-center gap-2 text-ui-base"><Input className="w-24" value={port} onChange={e => setPort(e.target.value)} /><code className="select-all rounded border border-border bg-surface px-2 py-0.5 text-ui-sm">{base}</code>
        <Button size="sm" onClick={async () => { try { const r = await fetch(`/sandboxes/${id}/ports/${port}/`, { headers: { Authorization: `Bearer ${token()}` } }); setOut(`${r.status} ${(await r.text()).slice(0, 200)}`) } catch (e) { setOut((e as Error).message) } }}>试一下 GET /</Button></div>
      {out && <pre className="mt-2 max-h-24 overflow-auto text-ui-sm text-fg-subtle">{out}</pre>}
    </Panel>
  )
}

function SandboxPage({ params: [id] }: { params: string[] }) {
  const all = useStore(s => s.sandboxes), load = useStore(s => s.load), act = useStore(s => s.act)
  useEffect(() => { void load.sandboxes(); const i = setInterval(() => void load.sandboxes(), 5000); return () => clearInterval(i) }, [id])
  const x = (all.data ?? []).find(s => s.id === id)
  if (!x) return all.error ? <ErrorLine>{all.error}</ErrorLine> : <p className="text-ui-base text-fg-subtle">{all.data ? `没有沙箱 ${id}` : '加载中'}</p>
  const rail = (t: string) => `#/sandboxes/${id}?rail=${t}&sandbox=${id}`
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="font-mono text-ui-xl font-semibold">{x.id}</h1><SandboxBadge s={x.status} />
        <div className="flex-1" />
        <a className="text-ui-sm" href={rail('terminal')}>终端</a><a className="text-ui-sm" href={rail('files')}>文件</a><a className="text-ui-sm" href={rail('events')}>事件流</a><a className="text-ui-sm" href={rail('exec')}>执行</a>
      </header>
      <Panel title="状态" actions={<div className="flex gap-1">
        {x.status === 'running' && <Button size="sm" onClick={() => act.sandbox(id, 'pause')}>暂停</Button>}
        {x.status === 'paused' && <Button size="sm" onClick={() => act.sandbox(id, 'resume')}>恢复</Button>}
        <Button size="sm" onClick={async () => { const r = await act.sandbox(id, 'fork', { n: 1 }) as { clones?: Sandbox[] }; const c = r.clones?.[0]; if (c) navigate(`#/sandboxes/${c.id}`) }}>fork</Button>
        <Button size="sm" onClick={async () => { const name = window.prompt('层名字'); if (name) { const r = await act.sandbox(id, 'pack', { name }) as { id?: string }; window.alert('层 ' + (r.id || '?')) } }}>打包为层</Button>
        <Confirm text={`删除沙箱 ${id}?`} onYes={() => act.sandbox(id, 'delete').then(() => navigate('#/sandboxes'))}>删除</Confirm>
      </div>}>
        <KV rows={[['类别', `${x.class || '-'}${x.priority ? ` · ${x.priority}` : ''}${x.durable ? ' · durable' : ''}`], ['模板', x.template || '-'], ['层', (x.layers || []).join(', ') || '-'], ['规格', `${x.vcpu ?? '-'} vCPU · ${x.mem ?? '-'} MiB · 盘 ${x.disk ?? '-'} MiB(用了 ${x.disk_used_mib ?? '-'})`], ['节点', `${x.node || '-'}${x.ip ? ` · ${x.ip}` : ''}`], ['启动方式', x.boot_mode || '-'],
          ['网络', `${x.network?.mode || 'closed'}${(x.network?.allow_hosts || []).length ? ': ' + (x.network?.allow_hosts || []).join(', ') : ''}${x.network?.limits?.requests_per_min ? ` · ≤${x.network.limits.requests_per_min}/分` : ''}${x.egress ? ` · 出 ${Math.round((x.egress.out || 0) / 1024)} KiB 入 ${Math.round((x.egress.in || 0) / 1024)} KiB` : ''}`],
          ['卷', (x.volumes || []).map(v => `${v.id} → ${v.mount}${v.ro ? ' (ro)' : ''}`).join(', ') || '-'], ['环境', (x.env_keys || []).join(', ') || '-'],
          ['TTL', `空闲 ${x.ttl_idle_s ?? '-'} s · 最长 ${x.ttl_max_s ?? '-'} s · 暂停后保留 ${x.retain_paused_s ? `${x.retain_paused_s} s` : '一直'}`], ['创建', when(x.created)]]} />
      </Panel>
      <Checkpoints id={id} running={x.status === 'running'} />
      <Ports id={id} />
    </div>
  )
}

export const sandboxes: Plugin = {
  id: 'sandboxes',
  activate(ctx) {
    ctx.nav({ id: 'sandboxes', label: '沙箱', href: '#/sandboxes', order: 20, badge: s => { const d = (s as { sandboxes: { data: { status: string }[] | null } }).sandboxes.data; const n = d?.filter(x => x.status === 'running').length ?? 0; return n || null } })
    ctx.route({ id: 'sandboxes', pattern: /^#\/sandboxes$/, component: SandboxesPage, nav: 'sandboxes' })
    ctx.route({ id: 'sandbox', pattern: /^#\/sandboxes\/([^/?]+)$/, component: SandboxPage, nav: 'sandboxes' })
  },
}
