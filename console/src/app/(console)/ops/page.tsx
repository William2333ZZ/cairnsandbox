'use client'
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { POST } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { pct } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import type { NodeRow } from '@/lib/types'
import { Badge, Button, EmptyState, ErrorLine, Panel, Table } from '@/components/ui'

export default function Ops() {
  const { t } = useI18n(); const { role } = useAuth(); const qc = useQueryClient()
  const nodes = useGet<{ nodes: NodeRow[] }>(role === 'operator' ? '/nodes' : null, { every: 10000 }); const [err, setErr] = useState('')
  if (role !== 'operator') return <EmptyState title={t('nav.nodes')} body={t('w.operator only')} />
  const op = async (n: string, what: string) => { if (what === 'drain' && !window.confirm(`${t('ops.drain')} ${n}?`)) return; try { await POST(`/nodes/${n}/${what}`); setErr('') } catch (e) { setErr((e as Error).message) } void qc.invalidateQueries({ queryKey: ['/nodes'] }) }
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('ops.title')}</h1>
      {(err || nodes.error) && <ErrorLine>{err || (nodes.error as Error).message}</ErrorLine>}
      <Panel>
        <Table cols={[
          { h: 'node', cell: n => <span className="font-medium">{n.name}</span> }, { h: '', cell: n => n.alive ? <Badge tone="success">up</Badge> : <Badge tone="danger">down</Badge> }, { h: '', cell: n => n.cordoned ? <Badge tone="warning">cordoned</Badge> : null },
          { h: 'load', cell: n => n.load == null ? '-' : pct(n.load) }, { h: 'vms', cell: n => `${n.health?.vms_running ?? '-'} / ${n.health?.vms_total ?? '-'}` }, { h: 'mem MiB', cell: n => `${n.health?.mem_mib_running ?? '-'} / ${n.health?.mem_budget_mib ?? '-'}` },
          { h: '', cell: n => <span className="flex gap-1">{n.cordoned ? <Button size="sm" onClick={() => op(n.name, 'uncordon')}>{t('ops.uncordon')}</Button> : <Button size="sm" onClick={() => op(n.name, 'cordon')}>{t('ops.cordon')}</Button>}<Button size="sm" onClick={() => op(n.name, 'drain')}>{t('ops.drain')}</Button></span> },
        ]} rows={nodes.data?.nodes ?? null} keyOf={n => n.name} empty={t('common.empty')} />
      </Panel>
    </>
  )
}
