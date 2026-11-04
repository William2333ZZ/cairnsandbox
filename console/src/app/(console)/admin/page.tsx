'use client'
// 平台运维页(仅 operator):账户(停用 / 充值 / 计费方式)、邀请码、滥用信号。数据与动作都是已有的部署者接口,这里只是把它们放在一处。
import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, POST, PUT } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { usd, when } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import { Badge, Button, CodeBlock, EmptyState, ErrorLine, Input, Panel, Table } from '@/components/ui'

type Account = { id: string; name: string; created: number }
type Susp = { account: string; reason: string; by: string; at: number }
type Invite = { id: string; note: string; credit_usd: number; max_sandboxes: number; daily_usd: number; created: number; expires: number; used_by: string }
type Signal = { sandbox: string; project: string; kind: string; value: number; limit: number; window_s: number; at: number }
type Balance = { mode: string; balance_usd: number }

function AccountRow({ a, susp, refresh, fail }: { a: Account; susp?: Susp; refresh: () => void; fail: (m: string) => void }) {
  const { t } = useI18n()
  const bal = useGet<Balance>(`/accounts/${a.id}/balance`)
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); refresh(); void bal.refetch() } catch (e) { fail((e as Error).message) } }
  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-2 py-1.5 font-mono">{a.id}</td>
      <td className="px-2">{susp ? <Badge tone="danger">{t('admin.suspended')}: {susp.reason}</Badge> : <Badge tone="success">ok</Badge>}</td>
      <td className="px-2">{bal.data ? <><Badge>{bal.data.mode}</Badge> {bal.data.mode === 'prepaid' && <span className={bal.data.balance_usd <= 0 ? 'text-destructive' : ''}>{usd(bal.data.balance_usd)}</span>}</> : '…'}</td>
      <td className="px-2"><span className="flex flex-wrap gap-1">
        {susp
          ? <Button size="sm" onClick={() => act(() => POST(`/accounts/${a.id}/unsuspend`, {}))}>{t('admin.unsuspend')}</Button>
          : <Button size="sm" kind="danger" onClick={() => { const r = window.prompt(t('admin.suspend.reason')); if (r) void act(() => POST(`/accounts/${a.id}/suspend`, { reason: r })) }}>{t('admin.suspend')}</Button>}
        <Button size="sm" onClick={() => { const v = window.prompt(t('admin.credit.ask')); const n = Number(v); if (v && n) void act(() => POST(`/accounts/${a.id}/credit`, { usd: n, note: 'console' })) }}>{t('admin.credit')}</Button>
        <Button size="sm" onClick={() => void act(() => PUT(`/accounts/${a.id}/billing`, { mode: bal.data?.mode === 'prepaid' ? 'postpaid' : 'prepaid' }))}>{bal.data?.mode === 'prepaid' ? t('admin.toPostpaid') : t('admin.toPrepaid')}</Button>
        {a.id !== 'default' && <Button size="sm" kind="danger" onClick={() => { if (window.prompt(t('admin.delete.ask', { id: a.id })) === a.id) void act(() => api('DELETE', `/accounts/${a.id}?force=1`)) }}>{t('admin.delete')}</Button>}
      </span></td>
    </tr>
  )
}

export default function Admin() {
  const { t } = useI18n(); const { role } = useAuth(); const qc = useQueryClient(); const op = role === 'operator'
  const accts = useGet<{ accounts: Account[] }>(op ? '/accounts' : null), susp = useGet<{ suspensions: Susp[] }>(op ? '/suspensions' : null, { every: 15000 })
  const inv = useGet<{ invites: Invite[] }>(op ? '/invites' : null), sig = useGet<{ enabled: boolean; signals: Signal[] }>(op ? '/abuse/signals' : null, { every: 30000 })
  const [err, setErr] = useState(''), [code, setCode] = useState(''), [f, setF] = useState({ note: '', credit_usd: '5', max_sandboxes: '3', daily_usd: '2', days: '7' })
  if (!op) return <EmptyState title={t('nav.admin')} body={t('w.operator only')} />
  const mk = async () => {
    try {
      const r = await POST<{ code: string }>('/invites', { note: f.note, credit_usd: Number(f.credit_usd), max_sandboxes: Number(f.max_sandboxes), daily_usd: Number(f.daily_usd), ttl_s: Number(f.days) * 86400 })
      setCode(r.code); setErr(''); void qc.invalidateQueries({ queryKey: ['/invites'] })
    } catch (e) { setErr((e as Error).message) }
  }
  const byAccount = new Map((susp.data?.suspensions ?? []).map(s => [s.account, s]))
  const field = (k: keyof typeof f, label: string, w = 'w-24') => <label className="text-ui-sm text-fg-subtle">{label}<Input className={`block ${w}`} value={f[k]} onChange={e => setF({ ...f, [k]: e.target.value })} /></label>
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('nav.admin')}</h1>
      {(err || accts.error) && <ErrorLine>{err || (accts.error as Error).message}</ErrorLine>}
      <Panel title={t('admin.accounts')}>
        <div className="overflow-x-auto"><table className="w-full text-ui-base">
          <thead><tr className="border-b border-border text-left text-ui-caption text-fg-subtle"><th className="px-2 py-1 font-normal">{t('admin.account')}</th><th className="px-2 font-normal">{t('admin.state')}</th><th className="px-2 font-normal">{t('admin.billing')}</th><th /></tr></thead>
          <tbody>{(accts.data?.accounts ?? []).map(a => <AccountRow key={a.id} a={a} susp={byAccount.get(a.id)} refresh={() => { void qc.invalidateQueries({ queryKey: ['/suspensions'] }) }} fail={setErr} />)}</tbody>
        </table></div>
      </Panel>
      <Panel title={t('admin.invites')}>
        {code && <div className="mb-3"><CodeBlock label={t('admin.invite.once')} code={code} /></div>}
        <div className="mb-3 flex flex-wrap items-end gap-2">
          {field('note', t('admin.invite.note'), 'w-44')}{field('credit_usd', t('admin.invite.credit'))}{field('max_sandboxes', t('admin.invite.maxsb'))}{field('daily_usd', t('admin.invite.daily'))}{field('days', t('admin.invite.days'), 'w-16')}
          <Button kind="primary" onClick={() => void mk()}>{t('admin.invite.make')}</Button>
        </div>
        <Table cols={[
          { h: 'note', cell: (i: Invite) => i.note || '-' }, { h: 'credit', cell: i => usd(i.credit_usd) }, { h: 'created', cell: i => when(i.created) }, { h: 'expires', cell: i => i.expires ? when(i.expires) : '-' },
          { h: 'used by', cell: i => i.used_by ? <Badge>{i.used_by}</Badge> : <Badge tone="info">{t('admin.invite.unused')}</Badge> },
        ]} rows={inv.data?.invites ?? null} keyOf={i => i.id} empty={t('common.empty')} />
      </Panel>
      <Panel title={t('admin.abuse')}>
        {sig.data && !sig.data.enabled && <p className="text-ui-sm text-fg-subtle">{t('admin.abuse.off')}</p>}
        <Table cols={[
          { h: 'sandbox', cell: (s: Signal) => <span className="font-mono">{s.sandbox}</span> }, { h: 'project', cell: s => s.project }, { h: 'kind', cell: s => <Badge tone="warning">{s.kind}</Badge> },
          { h: 'value', cell: s => s.kind === 'cpu' ? `${s.value.toFixed(0)}% (≥ ${s.limit}%)` : `${Math.round(s.value)} B/s` }, { h: 'created', cell: s => when(s.at) },
        ]} rows={sig.data?.signals ?? null} keyOf={s => `${s.sandbox}/${s.kind}/${s.at}`} empty={t('admin.abuse.none')} />
      </Panel>
    </>
  )
}
