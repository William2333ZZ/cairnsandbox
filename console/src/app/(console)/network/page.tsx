'use client'
import Link from 'next/link'
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { POST } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { age } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { canManage } from '@/lib/roles'
import { useGet } from '@/lib/query'
import type { NetPolicy, NetRequest } from '@/lib/types'
import { Badge, Button, ErrorLine, Panel, Table } from '@/components/ui'

export default function Network() {
  const { t } = useI18n(); const { project, role } = useAuth(); const qc = useQueryClient()
  const reqs = useGet<{ requests: NetRequest[] }>('/network/requests', { every: 10000 })
  const def = useGet<{ network: NetPolicy | null }>(project ? `/projects/${project}/network` : null)
  const [err, setErr] = useState('')
  const decide = async (id: string, what: 'approve' | 'deny') => { try { await POST(`/network/requests/${id}/${what}`); setErr('') } catch (e) { setErr((e as Error).message) } void qc.invalidateQueries({ queryKey: ['/network/requests'] }) }
  const n = def.data?.network
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('nav.network')}</h1>
      {(err || reqs.error) && <ErrorLine>{err || (reqs.error as Error).message}</ErrorLine>}
      <Panel title={t('net.queue')}>
        <Table cols={[
          { h: 'sandbox', cell: r => <Link href={`/sandboxes/detail/?id=${encodeURIComponent(r.sandbox)}&tab=network`} className="font-mono">{r.sandbox}</Link> }, { h: 'project', cell: r => r.project }, { h: 'by', cell: r => r.by },
          { h: '', cell: r => <code className="inline text-ui-sm">{JSON.stringify(r.network).slice(0, 140)}</code> }, { h: '', cell: r => age(r.created) },
          { h: '', cell: r => canManage(role) ? <span className="flex gap-1"><Button size="sm" kind="primary" onClick={() => decide(r.id, 'approve')}>{t('net.approve')}</Button><Button size="sm" onClick={() => decide(r.id, 'deny')}>{t('net.deny')}</Button></span> : <Badge>owner</Badge> },
        ]} rows={reqs.data?.requests ?? null} keyOf={r => r.id} empty={t('net.none')} />
      </Panel>
      <Panel title={`${project} · ${t('w.default network')}`}>
        {n ? <div className="text-ui-base">{t('w.mode')} <Badge>{n.mode ?? 'closed'}</Badge> <span className="text-fg-subtle">{(n.allow_hosts ?? []).join(', ')}</span>{n.limits?.requests_per_min ? <span className="text-fg-subtle"> · ≤ {n.limits.requests_per_min}/min</span> : null}</div> : <span className="text-fg-subtle">{t('common.none')}</span>}
      </Panel>
    </>
  )
}
