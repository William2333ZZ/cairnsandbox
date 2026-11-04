'use client'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { DEL, POST, PUT } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { useI18n } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import { when } from '@/lib/format'
import { Badge, Button, CodeBlock, ErrorLine, Input, Panel, Table } from '@/components/ui'

type Hook = { id: string; url: string; events: string[]; disabled: boolean; failures: number; last_status: number; last_at: number }
type Quota = { max_sandboxes: number; max_storage_gib: number; daily_usd: number }

export default function Settings() {
  const { t } = useI18n(); const { project } = useAuth(); const qc = useQueryClient()
  const q = useGet<Quota>(project ? `/projects/${project}/quota` : null); const [d, setD] = useState<Quota | null>(null), [msg, setMsg] = useState(''), [err, setErr] = useState('')
  useEffect(() => { if (q.data) setD(q.data) }, [q.data])
  const wh = useGet<{ webhooks: Hook[]; events: string[] }>(project ? `/projects/${project}/webhooks` : null), [url, setUrl] = useState(''), [evs, setEvs] = useState<string[]>([]), [secret, setSecret] = useState('')
  const whAct = async (fn: () => Promise<unknown>) => { try { await fn(); setErr(''); void qc.invalidateQueries({ queryKey: [`/projects/${project}/webhooks`] }) } catch (e) { setErr((e as Error).message) } }
  const f = (k: keyof Quota, label: string) => <label className="flex items-center gap-2 text-ui-base"><span className="w-40 text-fg-subtle">{label}</span><Input type="number" className="w-28" value={String(d?.[k] ?? 0)} onChange={e => setD({ ...(d as Quota), [k]: Number(e.target.value) })} /></label>
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('nav.settings')} · {project}</h1>
      {(err || q.error) && <ErrorLine>{err || (q.error as Error).message}</ErrorLine>}
      <Panel title={t('ov.quota')} actions={<Button size="sm" kind="primary" onClick={async () => { try { await PUT(`/projects/${project}/quota`, d); setMsg(t('common.saved')); setErr(''); void qc.invalidateQueries({ queryKey: [`/projects/${project}/quota`] }) } catch (e) { setErr((e as Error).message) } }}>{t('common.save')}</Button>}>
        {d && <div className="space-y-2">{f('max_sandboxes', t('quota.sandboxes'))}{f('max_storage_gib', t('quota.storage'))}{f('daily_usd', t('quota.daily'))}{msg && <span className="text-ui-sm text-success">{msg}</span>}</div>}
      </Panel>
      <Panel title={t('wh.title')}>
        <p className="mb-2 text-ui-sm text-fg-subtle">{t('wh.hint')}</p>
        {secret && <div className="mb-3"><CodeBlock label={t('wh.secret')} code={secret} /></div>}
        <div className="mb-3 space-y-2">
          <div className="flex gap-2"><Input className="min-w-0 flex-1" placeholder="https://hooks.example.com/cairn" value={url} onChange={e => setUrl(e.target.value)} aria-label="URL" />
            <Button kind="primary" disabled={!url.trim()} onClick={() => void whAct(async () => { const r = await POST<{ secret: string }>(`/projects/${project}/webhooks`, { url, events: evs }); setSecret(r.secret); setUrl('') })}>{t('wh.add')}</Button></div>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-ui-sm">{(wh.data?.events ?? []).filter(e => e !== 'ping').map(e => <label key={e} className="flex items-center gap-1"><input type="checkbox" checked={evs.includes(e)} onChange={() => setEvs(evs.includes(e) ? evs.filter(x => x !== e) : [...evs, e])} />{e}</label>)}<span className="text-fg-subtle">{t('wh.allIfNone')}</span></div>
        </div>
        <Table cols={[
          { h: 'url', cell: (h: Hook) => <span className="font-mono">{h.url}</span> }, { h: 'events', cell: h => h.events.length ? h.events.join(', ') : t('common.all') },
          { h: 'status', cell: h => h.disabled ? <Badge tone="danger">{t('wh.disabled')}</Badge> : h.last_status ? <Badge tone={h.last_status < 300 ? 'success' : 'warning'}>{h.last_status} · {when(h.last_at)}</Badge> : '-' },
          { h: '', cell: h => <span className="flex gap-1"><Button size="sm" onClick={() => void whAct(() => POST(`/projects/${project}/webhooks/${h.id}/test`))}>{t('wh.test')}</Button>{h.disabled && <Button size="sm" onClick={() => void whAct(() => POST(`/projects/${project}/webhooks/${h.id}/enable`))}>{t('wh.enable')}</Button>}<Button size="sm" kind="danger" onClick={() => void whAct(() => DEL(`/projects/${project}/webhooks/${h.id}`))}>{t('common.delete')}</Button></span> },
        ]} rows={wh.data?.webhooks ?? null} keyOf={h => h.id} empty={t('common.empty')} />
      </Panel>
    </>
  )
}
