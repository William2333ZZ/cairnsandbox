'use client'
import Link from 'next/link'
import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { DEL, POST, sandboxPath } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { age, mib } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import type { Sandbox } from '@/lib/types'
import { Button, ErrorLine, Input, Panel, Pill, StatusBadge, Table } from '@/components/ui'

const STATES = ['running', 'paused', 'unreachable', 'failed']

export default function Sandboxes() {
  const { t } = useI18n(); const { project, role } = useAuth(); const qc = useQueryClient()
  const list = useGet<{ sandboxes: Sandbox[] }>('/sandboxes', { every: 20000 })
  const [q, setQ] = useState(''), [st, setSt] = useState<string>(''), [cls, setCls] = useState<string>(''), [sel, setSel] = useState<Set<string>>(new Set()), [err, setErr] = useState('')
  const rows = useMemo(() => (list.data?.sandboxes ?? []).filter(s => (!project || !s.project || s.project === project) && (!st || s.status === st) && (!cls || s.class === cls)
    && (!q || [s.id, s.template, s.node, ...Object.entries(s.metadata ?? {}).flat()].some(v => String(v ?? '').toLowerCase().includes(q.toLowerCase())))), [list.data, project, st, cls, q])
  const act = async (what: 'pause' | 'resume' | 'delete') => {
    if (what === 'delete' && !window.confirm(t('sb.deleteConfirm'))) return
    try { await Promise.all([...sel].map(id => what === 'delete' ? DEL(sandboxPath(id)) : POST(sandboxPath(id, `/${what}`)))); setSel(new Set()); setErr('') } catch (e) { setErr((e as Error).message) }
    void qc.invalidateQueries({ predicate: x => String(x.queryKey[0]).startsWith('/sandboxes') })
  }
  const toggle = (id: string) => setSel(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('sb.title')}</h1>
      {(list.error || err) && <ErrorLine>{err || (list.error as Error).message}</ErrorLine>}
      <Panel>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Input className="w-72" placeholder={t('sb.search')} value={q} onChange={e => setQ(e.target.value)} aria-label={t('sb.search')} />
          <span className="text-ui-sm text-fg-subtle">{t('sb.filter.status')}</span>
          <Pill active={!st} onClick={() => setSt('')}>{t('common.all')}</Pill>
          {STATES.map(s => <Pill key={s} active={st === s} onClick={() => setSt(st === s ? '' : s)}>{t(`status.${s}`)}</Pill>)}
          <span className="ml-2 text-ui-sm text-fg-subtle">{t('sb.filter.class')}</span>
          {['persistent', 'ephemeral'].map(c => <Pill key={c} active={cls === c} onClick={() => setCls(cls === c ? '' : c)}>{c}</Pill>)}
        </div>
        {sel.size > 0 && (
          <div className="mb-2 flex items-center gap-2 rounded-lg bg-surface px-2 py-1 text-ui-sm"><span>{sel.size} {t('sb.selected')}</span>
            <Button size="sm" onClick={() => act('pause')}>{t('common.pause')}</Button><Button size="sm" onClick={() => act('resume')}>{t('common.resume')}</Button><Button size="sm" kind="danger" onClick={() => act('delete')}>{t('common.delete')}</Button>
          </div>
        )}
        <Table cols={[
          { h: <input type="checkbox" aria-label="all" checked={rows.length > 0 && rows.every(r => sel.has(r.id))} onChange={e => setSel(e.target.checked ? new Set(rows.map(r => r.id)) : new Set())} />, w: '28px', cell: s => <input type="checkbox" aria-label={s.id} checked={sel.has(s.id)} onChange={() => toggle(s.id)} /> },
          { h: t('sb.col.id'), cell: s => <Link href={`/sandboxes/detail/?id=${encodeURIComponent(s.id)}`} className="font-mono">{s.id}</Link> },
          { h: t('sb.col.status'), cell: s => <StatusBadge status={s.status} /> },
          { h: t('sb.col.template'), cell: s => s.template ?? '-' }, { h: t('sb.col.class'), cell: s => s.class ?? '-' },
          { h: t('sb.col.node'), cell: s => s.node ?? '-' }, { h: t('sb.col.mem'), cell: s => mib(s.mem) }, { h: t('sb.col.age'), cell: s => age(s.created) },
          ...(role === 'operator' ? [{ h: t('sb.col.project'), cell: (s: Sandbox) => s.project ?? '-' }] : []),
        ]} rows={list.data ? rows : null} keyOf={s => s.id} empty={t('sb.none')} />
      </Panel>
    </>
  )
}
