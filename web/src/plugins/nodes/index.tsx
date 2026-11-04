// 节点: health and usage per node.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { GET, POST } from '../../api/client'
import { Badge, Button, ErrorLine, Table } from '../../ui'

interface NodeRow { name: string; url: string; alive: boolean; cordoned?: boolean; health?: Record<string, unknown> | null; load?: number; reserve_vms?: number; reserve_mem_mib?: number }

function NodesPage() {
  const [d, setD] = useState<NodeRow[]>([]); const [err, setErr] = useState(''); const [msg, setMsg] = useState('')
  const f = () => GET<{ nodes: NodeRow[] }>('/nodes').then(r => { setD(r.nodes); setErr('') }).catch(e => setErr((e as Error).message))
  useEffect(() => { void f(); const i = setInterval(f, 5000); return () => clearInterval(i) }, [])
  const g = (n: NodeRow, k: string) => String((n.health || {})[k] ?? '-')
  const op = async (path: string, body?: unknown) => { try { const r = await POST<Record<string, unknown>>(path, body); setMsg(JSON.stringify(r)); setErr(''); await f() } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex items-center gap-3"><h1 className="text-ui-xl font-semibold">节点</h1><div className="flex-1" /><Button size="sm" onClick={() => op('/fleet/balance', { max: 1 })} title="把最忙节点的一台 persistent 沙箱搬去最闲的">均衡一次</Button></div>
      <p className="text-ui-sm text-fg-subtle">cordon 后不放新沙箱;drain = cordon 并把 persistent 沙箱迁走(D38)。</p>
      {err && <ErrorLine>{err}</ErrorLine>}
      {msg && <p className="text-ui-sm text-fg-subtle"><code>{msg.slice(0, 300)}</code></p>}
      <Table cols={[{ h: '名称', cell: n => <span className="flex items-center gap-1">{n.name}{n.cordoned && <Badge tone="warning">cordoned</Badge>}</span> }, { h: '存活', cell: n => n.alive ? <span className="text-success">是</span> : <span className="text-destructive">否</span> }, { h: 'VM 运行/总', cell: n => `${g(n, 'vms_running')}/${g(n, 'vms_total')}` }, { h: '上限', cell: n => g(n, 'max_vms') },
        { h: '内存 MiB', cell: n => `${g(n, 'mem_mib_running')}/${g(n, 'mem_budget_mib')}` }, { h: '负载', cell: n => <span title={`给交互留的余量:${n.reserve_vms || 2} 台 / ${n.reserve_mem_mib || 1024} MiB`}>{n.load === undefined ? '-' : `${Math.round(n.load * 100)}%`}</span> }, { h: '模板快照', cell: n => g(n, 'template_snapshots') }, { h: 'jail', cell: n => g(n, 'jail') }, { h: 'uffd', cell: n => g(n, 'uffd') }, { h: '层', cell: n => g(n, 'layers') },
        { h: '', cell: n => <span className="flex gap-1">{n.cordoned ? <Button size="sm" onClick={() => op(`/nodes/${n.name}/uncordon`)}>uncordon</Button> : <Button size="sm" onClick={() => op(`/nodes/${n.name}/cordon`)}>cordon</Button>}<Button size="sm" onClick={() => { if (window.confirm(`drain ${n.name}:cordon 并迁走它的 persistent 沙箱?`)) void op(`/nodes/${n.name}/drain`) }}>drain</Button>{!n.alive && <Button size="sm" kind="danger" onClick={() => { if (window.confirm(`evacuate ${n.name}:从最近 bundle 在别的节点恢复它的 durable 沙箱?`)) void op(`/nodes/${n.name}/evacuate`) }}>evacuate</Button>}</span> }]} rows={d} keyOf={n => n.name} />
    </div>
  )
}

export const nodes: Plugin = { id: 'nodes', activate(ctx) { ctx.nav({ id: 'nodes', label: '节点', href: '#/nodes', order: 40 }); ctx.route({ id: 'nodes', pattern: /^#\/nodes$/, component: NodesPage, nav: 'nodes' }) } }
