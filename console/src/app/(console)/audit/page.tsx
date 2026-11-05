'use client'
import { useState } from 'react'
import { GET } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { when } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import type { AuditEvent } from '@/lib/types'
import { Button, ErrorLine, Input, Panel, Select, Table } from '@/components/ui'

export default function Audit() {
  const { t } = useI18n(); const { role, project, projects } = useAuth()
  const [p, setP] = useState(project), [op, setOp] = useState(''), [n, setN] = useState('200'), [msg, setMsg] = useState('')
  const au = useGet<{ events: AuditEvent[] }>(`/audit?n=${n}${p ? `&project=${encodeURIComponent(p)}` : ''}`, { every: 20000 })
  const rows = (au.data?.events ?? []).filter(e => !op || e.op.includes(op))
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('audit.title')}</h1>
      {(au.error) && <ErrorLine>{(au.error as Error).message}</ErrorLine>}
      <Panel actions={<div className="flex items-center gap-2">
        <Select value={p} onChange={e => setP(e.target.value)} aria-label="project"><option value="">{role === 'operator' ? t('common.all') : '-'}</option>{projects.map(x => <option key={x} value={x}>{x}</option>)}</Select>
        <Input className="w-36" placeholder="op" value={op} onChange={e => setOp(e.target.value)} aria-label="op" />
        <Select value={n} onChange={e => setN(e.target.value)} aria-label="count">{['50', '200', '1000'].map(x => <option key={x}>{x}</option>)}</Select>
        {role === 'operator' && <Button size="sm" onClick={async () => { try { const v = await GET<{ ok: boolean; lines: number; first_bad: number }>('/audit/verify'); setMsg(v.ok ? `OK · ${v.lines}` : `broken at line ${v.first_bad}`) } catch (e) { setMsg((e as Error).message) } }}>{t('audit.verify')}</Button>}
        {msg && <span className="text-ui-sm text-fg-subtle">{msg}</span>}
      </div>}>
        <Table cols={[
          { h: 'time', w: '170px', cell: e => <span className="whitespace-nowrap">{when(e.ts)}</span> }, { h: 'op', cell: e => <span className="font-medium">{e.op}</span> }, { h: 'project', cell: e => String(e.project ?? e.for_project ?? '-') },
          { h: 'sandbox', cell: e => <span className="whitespace-nowrap font-mono">{e.sandbox ?? ''}</span> }, { h: 'by', cell: e => e.by ?? '' },
          { h: '', cell: e => <code className="inline-block max-w-[28rem] truncate align-middle text-ui-sm text-fg-subtle" title={JSON.stringify(e)}>{JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !['op', 'ts', 'project', 'for_project', 'sandbox', 'by', 'replica', 'h', 'prev'].includes(k)))).slice(0, 140)}</code> },
        ]} rows={au.data ? rows : null} keyOf={e => `${e.h ?? ''}${e.ts}${e.op}`} empty={t('common.empty')} />
      </Panel>
    </>
  )
}
