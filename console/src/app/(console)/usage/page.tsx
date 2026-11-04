'use client'
import { useMemo } from 'react'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { canManage } from '@/lib/roles'
import { usd, when } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import { BarChart } from '@/components/charts'
import { Button, ErrorLine, Panel, Table, Tile } from '@/components/ui'

type Line = { kind: string; amount: number; unit: string; cost_usd: number; events: number }
type Row = { key: string; cost_usd: number; kinds: Record<string, number> }
type Statement = { project: string; from: number; to: number; lines: Line[]; total_usd: number; sandboxes: number }

export default function Usage() {
  const { t } = useI18n(); const { project, me, role } = useAuth()
  const bal = useGet<{ mode: string; balance_usd: number; credits_usd: number }>(me?.account && canManage(role) && role !== 'operator' ? `/accounts/${me.account}/balance` : null)
  const from = useMemo(() => Math.floor(Date.now() / 1000) - 30 * 86400, [])
  const st = useGet<Statement>(project ? `/metering/projects/${project}/statement?from=${from}` : null), today = useGet<{ spent_usd: number }>(project ? `/metering/projects/${project}/today` : null)
  const st2 = useGet<{ statements: { id: string; from: number; to: number; total_usd: number }[] }>(project ? `/metering/projects/${project}/statements` : null)
  const days = useGet<{ rows: Row[] }>(project ? `/metering/projects/${project}/breakdown?by=day&from=${from}` : null), bySb = useGet<{ rows: Row[] }>(project ? `/metering/projects/${project}/breakdown?by=sandbox&from=${from}` : null)
  const bars = useMemo(() => { const m = new Map((days.data?.rows ?? []).map(r => [Number(r.key), r.cost_usd])); const d0 = Math.floor(from / 86400) * 86400; return Array.from({ length: 31 }, (_, i) => { const t = d0 + i * 86400; return { label: new Date(t * 1000).toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' }), v: m.get(t) ?? 0 } }) }, [days.data, from])
  const csv = async () => { const b = await api<ArrayBuffer>('GET', `/metering/projects/${project}/statement?from=${from}&format=csv`, undefined, { raw: true }); const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([b], { type: 'text/csv' })); a.download = `${project}-statement.csv`; a.click(); URL.revokeObjectURL(a.href) }
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('nav.usage')}</h1>
      {(st.error || today.error) && <ErrorLine>{((st.error || today.error) as Error).message}</ErrorLine>}
      <div className="grid gap-3 sm:grid-cols-3"><Tile label={t('ov.spend')} value={usd(today.data?.spent_usd)} /><Tile label={t('w.30d')} value={usd(st.data?.total_usd)} sub={`${st.data?.sandboxes ?? 0} ${t('sb.title')}`} />{bal.data?.mode === 'prepaid' && <Tile label={t('usage.balance')} value={usd(bal.data.balance_usd)} sub={t('usage.balance.sub', { credits: usd(bal.data.credits_usd) })} tone={bal.data.balance_usd <= 0 ? 'warning' : undefined} />}</div>
      <BarChart title={t('usage.daily')} bars={bars} fmt={v => usd(v)} />
      <Panel title={t('usage.topsb')}><Table cols={[{ h: 'sandbox', cell: (r: Row) => r.key === 'other' ? t('usage.other') : <span className="font-mono">{r.key}</span> }, { h: 'cost', cell: r => usd(r.cost_usd) }, { h: '', cell: r => Object.entries(r.kinds).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${usd(v)}`).join(' · ') }]} rows={bySb.data?.rows ?? null} keyOf={r => r.key} empty={t('common.empty')} /></Panel>
      <Panel title={t('w.30d')} actions={<Button size="sm" onClick={() => void csv()}>CSV</Button>}>
        <Table cols={[{ h: 'kind', cell: (l: Line) => l.kind }, { h: 'amount', cell: l => Math.round(l.amount).toLocaleString() }, { h: 'events', cell: l => l.events }, { h: 'cost', cell: l => usd(l.cost_usd) }]} rows={st.data?.lines ?? null} keyOf={l => l.kind} empty={t('common.empty')} />
      </Panel>
      <Panel title="statements"><Table cols={[{ h: 'id', cell: x => <span className="font-mono">{x.id}</span> }, { h: 'from', cell: x => when(x.from) }, { h: 'to', cell: x => when(x.to) }, { h: 'total', cell: x => usd(x.total_usd) }]} rows={st2.data?.statements ?? null} keyOf={x => x.id} empty={t('common.empty')} /></Panel>
    </>
  )
}
