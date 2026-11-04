'use client'
import Link from 'next/link'
import { useAuth } from '@/lib/auth'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import { age, pct, usd, when } from '@/lib/format'
import type { AuditEvent, NetRequest, NodeRow, Sandbox } from '@/lib/types'
import { Badge, CodeBlock, EmptyState, ErrorLine, Panel, StatusBadge, Table, Tile } from '@/components/ui'

type Health = { nodes: Record<string, { alive: boolean; running?: number; total?: number; mem_free_mib?: number }> }

export default function Overview() {
  const { t } = useI18n(); const { project, role } = useAuth()
  const sbs = useGet<{ sandboxes: Sandbox[] }>('/sandboxes', { every: 15000 })
  const today = useGet<{ spent_usd: number }>(project ? `/metering/projects/${project}/today` : null, { every: 30000 })
  const quota = useGet<{ max_sandboxes: number; daily_usd: number }>(project ? `/projects/${project}/quota` : null)
  const health = useGet<Health>('/health', { every: 15000 })
  const nodes = useGet<{ nodes: NodeRow[] }>(role === 'operator' ? '/nodes' : null, { every: 15000 })
  const reqs = useGet<{ requests: NetRequest[] }>('/network/requests', { every: 15000 })
  const au = useGet<{ events: AuditEvent[] }>(`/audit?n=8${project ? `&project=${encodeURIComponent(project)}` : ''}`, { every: 15000 })

  const mine = (sbs.data?.sandboxes ?? []).filter(s => !project || !s.project || s.project === project)
  const running = mine.filter(s => s.status === 'running').length, paused = mine.filter(s => s.status === 'paused').length
  const hn = Object.values(health.data?.nodes ?? {}); const up = hn.filter(n => n.alive).length
  const loads = (nodes.data?.nodes ?? []).filter(n => n.alive).map(n => n.load ?? 0); const cap = loads.length ? loads.reduce((a, b) => a + b, 0) / loads.length : null
  const spent = today.data?.spent_usd, budget = quota.data?.daily_usd
  const err = sbs.error || health.error

  if (sbs.data && mine.length === 0) {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://<控制面>'
    return (
      <>
        {err && <ErrorLine>{(err as Error).message}</ErrorLine>}
        <EmptyState title={t('ov.start.title')} body={t('ov.start.body')}>
          <div className="space-y-2">
            <CodeBlock label={t('ov.start.install')} code="pip install -e sdk/python" />
            <CodeBlock label={t('ov.start.run')} code={`from cairnbox import Client\n\nc = Client("${origin}", token="…", project="${project || 'default'}")\nwith c.sandboxes.create(mem=256) as sb:\n    print(sb.commands.run("echo hello").stdout)`} />
          </div>
        </EmptyState>
      </>
    )
  }
  return (
    <>
      {err && <ErrorLine>{(err as Error).message}</ErrorLine>}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label={t('ov.spend')} value={usd(spent)} sub={budget ? <><span className="mb-1 block h-1 rounded bg-surface"><span className="block h-1 rounded bg-link" style={{ width: pct(Math.min(1, (spent ?? 0) / budget)) }} /></span>{t('ov.budget')} {usd(budget)}</> : t('ov.noBudget')} />
        <Tile label={t('ov.running')} value={running} sub={`${paused} ${t('ov.paused')}${quota.data?.max_sandboxes ? ` · ${t('ov.quota')} ${mine.length} / ${quota.data.max_sandboxes}` : ''}`} />
        <Tile label={t('ov.capacity')} value={cap == null ? `${up} / ${hn.length}` : pct(cap)} sub={`${up} / ${hn.length} ${t('ov.nodes')}`} />
        <Tile label={t('ov.pending')} value={reqs.data?.requests.length ?? '-'} tone={(reqs.data?.requests.length ?? 0) > 0 ? 'warning' : undefined} sub={<Link href="/network/">{t('nav.network')} →</Link>} />
      </div>
      <Panel title={t('ov.sandboxes')} actions={<Link href="/sandboxes/" className="text-ui-sm">{t('common.all')} →</Link>}>
        <Table cols={[
          { h: 'ID', cell: s => <Link href={`/sandboxes/detail/?id=${encodeURIComponent(s.id)}`} className="font-mono">{s.id}</Link> },
          { h: t('sb.col.status'), cell: s => <StatusBadge status={s.status} /> },
          { h: t('sb.col.class'), cell: s => s.class ?? '-' }, { h: t('sb.col.node'), cell: s => s.node ?? '-' }, { h: t('sb.col.age'), cell: s => age(s.created) },
        ]} rows={mine.slice(0, 6)} keyOf={s => s.id} empty={t('sb.none')} />
      </Panel>
      <Panel title={t('ov.recent')} actions={<Link href="/audit/" className="text-ui-sm">{t('nav.audit')} →</Link>}>
        <ul className="space-y-0.5 text-ui-sm">
          {(au.data?.events ?? []).map((e, i) => <li key={i} className="flex gap-2"><span className="w-40 shrink-0 text-fg-subtlest">{when(e.ts)}</span><span className="font-medium">{e.op}</span><span className="font-mono text-fg-subtle">{e.sandbox ?? ''}</span><span className="text-fg-subtle">{e.by ? `by ${e.by}` : ''}</span></li>)}
          {au.data && au.data.events.length === 0 && <li className="text-fg-subtle">{t('common.empty')}</li>}
        </ul>
      </Panel>
      {role === 'operator' && (nodes.data?.nodes ?? []).some(n => !n.alive) && <Badge tone="danger">{(nodes.data?.nodes ?? []).filter(n => !n.alive).map(n => n.name).join(', ')} offline</Badge>}
    </>
  )
}
