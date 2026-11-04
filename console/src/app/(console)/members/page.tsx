'use client'
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { DEL, POST } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { when } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import { canManage } from '@/lib/roles'
import { Badge, Button, CodeBlock, ErrorLine, Input, Panel, Select, Table } from '@/components/ui'

type Key = { id: string; name: string; role: string; created: number; expires: number; last_used: number }
type Member = { id: string; user: string; name: string; role: string; created: number }

export default function Members() {
  const { t } = useI18n(); const { project, role, me } = useAuth(); const qc = useQueryClient()
  const ks = useGet<{ keys: Key[] }>(project && canManage(role) ? `/projects/${project}/keys` : null)
  const [kname, setKname] = useState(''), [krole, setKrole] = useState('member'), [kttl, setKttl] = useState('0'), [secret, setSecret] = useState<{ name: string; secret: string } | null>(null)
  const mint = async () => { try { const r = await POST<{ key: Key; secret: string }>(`/projects/${project}/keys`, { name: kname, role: krole, ttl_s: Number(kttl) * 86400 }); setSecret({ name: r.key.name, secret: r.secret }); setKname(''); setErr(''); void qc.invalidateQueries({ queryKey: [`/projects/${project}/keys`] }) } catch (e) { setErr((e as Error).message) } }
  const revoke = async (k: Key) => { if (!window.confirm(`${t('keys.revoke')}: ${k.name}`)) return; try { await DEL(`/projects/${project}/keys/${k.id}`); void qc.invalidateQueries({ queryKey: [`/projects/${project}/keys`] }) } catch (e) { setErr((e as Error).message) } }
  const ms = useGet<{ members: Member[] }>(project ? `/projects/${project}/members` : null)
  const [issued, setIssued] = useState<{ user: string; token: string } | null>(null), [err, setErr] = useState('')
  const rotate = async (u: string) => { if (!window.confirm(`${t('members.rotate')}: ${u}`)) return; try { setIssued(await POST(`/projects/${project}/members/${u}/rotate`, {})); setErr('') } catch (e) { setErr((e as Error).message) } }
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('nav.members')}</h1>
      {(err || ms.error) && <ErrorLine>{err || (ms.error as Error).message}</ErrorLine>}
      {issued && <CodeBlock label={`${t('members.rotated')}: ${issued.user}`} code={issued.token} />}
      <Panel title={t('members.title')}>
        <Table cols={[
          { h: 'user', cell: m => <span className="font-mono">{m.user}</span> }, { h: 'role', cell: m => <Badge tone={m.role === 'owner' ? 'info' : 'neutral'}>{m.role}</Badge> }, { h: '', cell: m => m.name }, { h: '', cell: m => when(m.created) },
          { h: '', cell: m => (canManage(role) || m.user === me?.user) ? <Button size="sm" onClick={() => rotate(m.user)}>{t('members.rotate')}</Button> : null },
        ]} rows={ms.data?.members ?? null} keyOf={m => m.id} empty={t('common.empty')} />
      </Panel>
      {canManage(role) && <>
        {secret && <CodeBlock label={`${t('keys.created')}: ${secret.name}`} code={secret.secret} />}
        <Panel title={t('keys.title')}>
          <p className="mb-2 text-ui-sm text-fg-subtle">{t('keys.hint')}</p>
          <div className="mb-3 flex flex-wrap items-end gap-2">
            <label className="text-ui-sm text-fg-subtle">{t('keys.name')}<Input value={kname} onChange={e => setKname(e.target.value)} placeholder="ci" className="block" /></label>
            <label className="text-ui-sm text-fg-subtle">{t('keys.role')}<Select value={krole} onChange={e => setKrole(e.target.value)} className="block"><option value="member">{t('keys.role.member')}</option><option value="viewer">{t('keys.role.viewer')}</option></Select></label>
            <label className="text-ui-sm text-fg-subtle">{t('keys.ttl')}<Input value={kttl} onChange={e => setKttl(e.target.value)} className="block w-20" inputMode="numeric" /></label>
            <Button kind="primary" disabled={!kname.trim()} onClick={() => void mint()}>{t('keys.create')}</Button>
          </div>
          <Table cols={[
            { h: 'name', cell: k => <span className="font-mono">{k.name}</span> }, { h: 'role', cell: k => <Badge>{k.role}</Badge> }, { h: 'created', cell: k => when(k.created) },
            { h: 'expires', cell: k => k.expires ? when(k.expires) : '-' }, { h: 'last used', cell: k => k.last_used ? when(k.last_used) : '-' },
            { h: '', cell: k => <Button size="sm" kind="danger" onClick={() => void revoke(k)}>{t('keys.revoke')}</Button> },
          ]} rows={ks.data?.keys ?? null} keyOf={k => k.id} empty={t('common.empty')} />
        </Panel>
      </>}
    </>
  )
}
