'use client'
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { DEL } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { age, mib } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { canManage } from '@/lib/roles'
import { useGet } from '@/lib/query'
import { Button, ErrorLine, Panel, Table } from '@/components/ui'

type Volume = { id: string; project: string; node: string; name: string; size_mib: number; created: number; attached?: Record<string, string>; allocated_mib?: number }

export default function Volumes() {
  const { t } = useI18n(); const { project, role } = useAuth(); const qc = useQueryClient(); const [err, setErr] = useState('')
  const v = useGet<{ volumes: Volume[] }>('/volumes', { every: 15000 })
  const rows = (v.data?.volumes ?? []).filter(x => !project || x.project === project)
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('nav.volumes')}</h1>
      {(err || v.error) && <ErrorLine>{err || (v.error as Error).message}</ErrorLine>}
      <Panel><Table cols={[
        { h: 'id', cell: x => <span className="font-mono">{x.id}</span> }, { h: 'name', cell: x => x.name }, { h: 'node', cell: x => x.node }, { h: 'size', cell: x => `${mib(x.allocated_mib)} / ${mib(x.size_mib)}` },
        { h: 'attached', cell: x => Object.entries(x.attached ?? {}).map(([s, m]) => `${s} (${m})`).join(', ') || '-' }, { h: '', cell: x => age(x.created) },
        { h: '', cell: x => canManage(role) ? <Button size="sm" kind="danger" onClick={async () => { if (window.confirm(`${t('common.delete')} ${x.name}?`)) { try { await DEL(`/volumes/${x.id}`); setErr('') } catch (e) { setErr((e as Error).message) } void qc.invalidateQueries({ queryKey: ['/volumes'] }) } }}>{t('common.delete')}</Button> : null },
      ]} rows={v.data ? rows : null} keyOf={x => x.id} empty={t('common.empty')} /></Panel>
    </>
  )
}
