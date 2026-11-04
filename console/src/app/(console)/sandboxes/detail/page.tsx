'use client'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError, DEL, GET, POST, headers, sandboxPath } from '@/lib/api'
import { LineChart, rate } from '@/components/charts'
import { useAuth } from '@/lib/auth'
import { age, bytes, mib, when } from '@/lib/format'
import { useI18n, word } from '@/lib/i18n'
import { useGet } from '@/lib/query'
import type { AuditEvent, Checkpoint, Decision, Sandbox } from '@/lib/types'
import { Badge, Button, CodeBlock, ErrorLine, Input, Panel, StatusBadge, Table, cx } from '@/components/ui'

const TABS = ['overview', 'metrics', 'terminal', 'network', 'checkpoints', 'ports', 'events'] as const
type Tab = (typeof TABS)[number]

function KV({ rows }: { rows: [string, React.ReactNode][] }) {
  const { lang } = useI18n()
  return <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-ui-base">{rows.map(([k, v]) => <div key={k} className="contents"><dt className="text-fg-subtle">{word(lang, k) as React.ReactNode}</dt><dd className="min-w-0 break-words">{v}</dd></div>)}</dl>
}

function Detail() {
  const sp = useSearchParams(); const id = sp.get('id') ?? ''; const tab = (TABS.find(x => x === sp.get('tab')) ?? 'overview') as Tab
  const { t } = useI18n(); const { project } = useAuth(); const router = useRouter(); const qc = useQueryClient()
  const sb = useGet<Sandbox>(id ? sandboxPath(id) : null, { every: 10000 })
  const [err, setErr] = useState('')
  const refresh = () => void qc.invalidateQueries({ predicate: x => String(x.queryKey[0]).startsWith('/sandboxes') })
  const run = async (f: () => Promise<unknown>) => { try { await f(); setErr('') } catch (e) { setErr(e instanceof ApiError && e.status === 202 ? 'waiting for an owner to approve' : (e as Error).message) } refresh() }
  if (!id) return <ErrorLine>missing id</ErrorLine>
  const s = sb.data
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  const equiv = `from cairnbox import Client\nc = Client("${origin}", token="…", project="${project || 'default'}")\nsb = c.sandboxes.get("${id}")\n` + ({
    overview: 'print(sb.info)', metrics: 'print(sb.usage())', terminal: 'print(sb.commands.run("uname -a").stdout)', network: 'print(sb.network.get())\nprint(sb.network.decisions())',
    checkpoints: 'sb.checkpoints.create("before")\nsb.checkpoints.rollback("before")\nclone = sb.fork(checkpoint="before")[0]', ports: `print(sb.ports.url(8080))`, events: 'events, last = sb.events()',
  } as Record<Tab, string>)[tab]
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <Link href="/sandboxes/" className="text-ui-sm text-fg-subtle">← {t('sb.title')}</Link>
        <h1 className="font-mono text-ui-xl font-medium">{id}</h1>
        {s && <StatusBadge status={s.status} />}
        {s && <span className="text-ui-sm text-fg-subtle">{s.node} · {s.class}{s.durable ? ' · durable' : ''}</span>}
        <div className="flex-1" />
        {s?.status === 'running' && <Button size="sm" onClick={() => run(() => POST(sandboxPath(id, '/pause')))}>{t('common.pause')}</Button>}
        {s?.status === 'paused' && <Button size="sm" onClick={() => run(() => POST(sandboxPath(id, '/resume')))}>{t('common.resume')}</Button>}
        <Button size="sm" kind="danger" onClick={async () => { if (window.confirm(`${t('common.delete')} ${id}?`)) { await run(() => DEL(sandboxPath(id))); router.push('/sandboxes/') } }}>{t('common.delete')}</Button>
      </div>
      {(sb.error || err) && <ErrorLine>{err || (sb.error as Error).message}</ErrorLine>}
      <div className="flex gap-1 border-b border-border text-ui-base">
        {TABS.map(x => <Link key={x} href={`/sandboxes/detail/?id=${encodeURIComponent(id)}&tab=${x}`} className={cx('px-3 py-1.5 text-fg no-underline hover:no-underline', tab === x ? 'border-b-2 border-brand font-medium' : 'text-fg-subtle')}>{t(`sb.tab.${x}`)}</Link>)}
      </div>
      {s && tab === 'overview' && <Panel><KV rows={[
        [t('sb.col.template'), s.template ?? '-'], ['vCPU / ' + t('sb.col.mem'), `${s.vcpu ?? '-'} · ${mib(s.mem)}`], ['disk', `${mib(s.disk)} (${mib(s.disk_used_mib)})`], [t('sb.col.node'), `${s.node ?? '-'}${s.ip ? ` · ${s.ip}` : ''}`],
        ['boot', s.boot_mode ?? '-'], ['layers', (s.layers ?? []).join(', ') || '-'], ['env', (s.env_keys ?? []).join(', ') || '-'], ['TTL', `idle ${s.ttl_idle_s ?? '-'} s · max ${s.ttl_max_s ?? '-'} s · retain ${s.retain_paused_s ? `${s.retain_paused_s} s` : '∞'}`],
        ['labels', Object.entries(s.labels ?? {}).map(([k, v]) => `${k}=${v}`).join(' ') || '-'], [t('sb.col.age'), `${age(s.created)} (${when(s.created)})`],
      ]} /></Panel>}
      {s && tab === 'metrics' && <MetricsTab sb={s} />}
      {s && tab === 'terminal' && <Terminal id={id} disabled={s.status !== 'running' && s.status !== 'paused'} />}
      {s && tab === 'network' && <NetworkTab sb={s} />}
      {s && tab === 'checkpoints' && <CheckpointsTab id={id} run={run} router={router} />}
      {s && tab === 'ports' && <PortsTab sb={s} />}
      {s && tab === 'events' && <EventsTab id={id} />}
      <CodeBlock label={t('sb.equiv')} code={equiv} />
    </>
  )
}

function Terminal({ id, disabled }: { id: string; disabled: boolean }) {
  const [cmd, setCmd] = useState(''), [log, setLog] = useState<{ cmd: string; out: string; code?: number }[]>([]), [busy, setBusy] = useState(false)
  const go = async () => {
    const c = cmd.trim(); if (!c) return; setBusy(true); setCmd('')
    try { const r = await POST<{ exit: number; stdout: string; stderr: string }>(sandboxPath(id, '/exec'), { cmd: c, timeout: 60 }); setLog(l => [...l, { cmd: c, out: r.stdout + r.stderr, code: r.exit }]) } catch (e) { setLog(l => [...l, { cmd: c, out: (e as Error).message }]) }
    setBusy(false)
  }
  return (
    <Panel>
      <div className="max-h-96 space-y-2 overflow-y-auto">{log.map((l, i) => <div key={i}><pre className="code text-fg-subtle">$ {l.cmd}</pre><pre className="code">{l.out}{l.code ? `\n[exit ${l.code}]` : ''}</pre></div>)}</div>
      <form className="mt-2 flex gap-2" onSubmit={e => { e.preventDefault(); void go() }}><Input className="flex-1 font-mono" placeholder="$ " value={cmd} onChange={e => setCmd(e.target.value)} disabled={disabled || busy} aria-label="command" /><Button type="submit" disabled={disabled || busy}>↵</Button></form>
    </Panel>
  )
}

type MetricPoint = { t: number; cpu_pct: number | null; mem_mib: number; egress_out?: number; egress_in?: number }
function MetricsTab({ sb }: { sb: Sandbox }) {
  const { t } = useI18n()
  const m = useGet<{ points: MetricPoint[]; interval_s: number }>(sandboxPath(sb.id, '/metrics'), { every: 5000, retry: false })
  const pts = m.data?.points ?? []
  if (m.error) return <Panel><p className="text-ui-sm text-fg-subtle">{t('sb.metrics.none')}</p></Panel>
  return (
    <div className="space-y-3">
      <div className="grid gap-3 md:grid-cols-2">
        <LineChart title={t('sb.metrics.cpu')} unit="%" points={pts.map(p => ({ t: p.t, v: p.cpu_pct }))} max={(sb.vcpu ?? 1) * 100} />
        <LineChart title={t('sb.metrics.mem')} unit=" MiB" points={pts.map(p => ({ t: p.t, v: p.mem_mib }))} max={sb.mem} />
        <LineChart title={t('sb.metrics.egress')} points={rate(pts.map(p => ({ t: p.t, v: p.egress_out })))} fmt={v => `${bytes(v)}/s`} />
      </div>
      <Panel><KV rows={[['disk', `${mib(sb.disk_used_mib)} / ${mib(sb.disk)}`], ['egress out / in', `${bytes(sb.egress?.out)} / ${bytes(sb.egress?.in)}`], [t('sb.metrics.window'), `${pts.length} × ${m.data?.interval_s ?? 5} s`]]} /></Panel>
    </div>
  )
}

function NetworkTab({ sb }: { sb: Sandbox }) {
  const { t } = useI18n(); const d = useGet<{ decisions: Decision[] }>(sandboxPath(sb.id, '/egress'), { every: 10000 })
  const n = sb.network ?? {}
  return (
    <>
      <Panel><dl className="space-y-1 text-ui-base">
        <div>{t('w.mode')} <Badge>{n.mode ?? 'closed'}</Badge> {n.limits?.requests_per_min ? <span className="text-fg-subtle">≤ {n.limits.requests_per_min}/min</span> : null}</div>
        <div className="text-fg-subtle">{(n.allow_hosts ?? []).join(', ') || t('common.none')}</div>
      </dl></Panel>
      <Panel title={t('sb.decisions')}>
        <Table cols={[
          { h: '', w: '70px', cell: x => <Badge tone={x.allowed ? 'success' : 'danger'}>{x.allowed ? t('sb.allowed') : t('sb.denied')}</Badge> },
          { h: 'host', cell: x => <span className="font-mono">{x.host}{x.port ? `:${x.port}` : ''}{x.path ?? ''}</span> }, { h: 'method', cell: x => x.method ?? '' },
          { h: 'rule', cell: x => x.rule ?? '-' }, { h: 'injected', cell: x => (x.injected ?? []).join(', ') },
        ]} rows={[...(d.data?.decisions ?? [])].reverse()} keyOf={x => JSON.stringify(x) + Math.random()} empty={t('common.empty')} />
      </Panel>
    </>
  )
}

function CheckpointsTab({ id, run, router }: { id: string; run: (f: () => Promise<unknown>) => Promise<void>; router: ReturnType<typeof useRouter> }) {
  const { t } = useI18n(); const l = useGet<{ checkpoints: Checkpoint[] }>(sandboxPath(id, '/checkpoints')); const [name, setName] = useState('')
  return (
    <Panel title={t('sb.tab.checkpoints')} actions={<form className="flex gap-1" onSubmit={e => { e.preventDefault(); void run(() => POST(sandboxPath(id, '/checkpoints'), name.trim() ? { name: name.trim() } : {}).then(() => setName(''))) }}><Input className="w-40 py-0.5 text-ui-sm" placeholder="name" value={name} onChange={e => setName(e.target.value)} aria-label="checkpoint name" /><Button size="sm" kind="primary" type="submit">+</Button></form>}>
      <Table cols={[
        { h: 'name', cell: c => <span className="font-mono">{c.name}</span> }, { h: 'note', cell: c => c.note ?? '' }, { h: 'MiB', cell: c => c.upper_mib ?? '-' }, { h: '', cell: c => age(c.created) },
        { h: '', cell: c => <span className="flex gap-1">
          <Button size="sm" onClick={() => { if (window.confirm(`${t('sb.rollback.warn')}\n\n${c.name}`)) void run(() => POST(sandboxPath(id, '/rollback'), { checkpoint: c.name })) }}>rollback</Button>
          <Button size="sm" onClick={() => void run(async () => { const r = await POST<{ clones?: { id: string }[] }>(sandboxPath(id, '/fork'), { n: 1, checkpoint: c.name, class: 'persistent' }); const f = r.clones?.[0]; if (f) router.push(`/sandboxes/detail/?id=${encodeURIComponent(f.id)}`) })}>fork</Button>
          <Button size="sm" kind="danger" onClick={() => void run(() => DEL(sandboxPath(id, `/checkpoints/${c.name}`)))}>×</Button></span> },
      ]} rows={l.data?.checkpoints ?? null} keyOf={c => c.name} empty={t('common.empty')} />
    </Panel>
  )
}

function PortsTab({ sb }: { sb: Sandbox }) {
  const { t } = useI18n(); const [port, setPort] = useState('8080'); const [out, setOut] = useState('')
  const url = `${typeof window !== 'undefined' ? window.location.origin : ''}${sandboxPath(sb.id, `/ports/${port}/`)}`
  return (
    <>
      <Panel title="ports"><div className="flex flex-wrap items-center gap-2"><Input className="w-24" value={port} onChange={e => setPort(e.target.value)} aria-label="port" /><code className="inline select-all">{url}</code>
        <Button size="sm" onClick={async () => { try { const r = await fetch(url, { headers: headers() }); setOut(`${r.status} ${(await r.text()).slice(0, 300)}`) } catch (e) { setOut((e as Error).message) } }}>GET /</Button></div>
        {out && <pre className="code mt-2 text-fg-subtle">{out}</pre>}</Panel>
      <Panel title={t('nav.volumes')}>{(sb.volumes ?? []).length === 0 ? <span className="text-fg-subtle">{t('common.none')}</span> : <ul className="font-mono text-ui-base">{sb.volumes!.map(v => <li key={v.id}>{v.id} → {v.mount}{v.ro ? ' (ro)' : ''}</li>)}</ul>}</Panel>
    </>
  )
}

function EventsTab({ id }: { id: string }) {
  const { t } = useI18n(); const ev = useGet<{ events: { seq: number; ts: number; type: string; data: unknown }[] }>(sandboxPath(id, '/events?since=0&max=100'), { every: 10000 })
  const au = useGet<{ events: AuditEvent[] }>('/audit?n=500')
  const mine = (au.data?.events ?? []).filter(e => e.sandbox === id)
  return (
    <>
      <Panel title={t('sb.tab.events')}>{(ev.data?.events ?? []).length === 0 ? <span className="text-fg-subtle">{t('common.empty')}</span> : <ul className="space-y-0.5 text-ui-sm">{ev.data!.events.map(e => <li key={e.seq} className="flex gap-2"><span className="w-40 shrink-0 text-fg-subtlest">{when(e.ts)}</span><span className="font-medium">{e.type}</span><code className="inline min-w-0 truncate">{JSON.stringify(e.data)}</code></li>)}</ul>}</Panel>
      <Panel title={t('nav.audit')}><ul className="space-y-0.5 text-ui-sm">{mine.map((e, i) => <li key={i} className="flex gap-2"><span className="w-40 shrink-0 text-fg-subtlest">{when(e.ts)}</span><span className="font-medium">{e.op}</span><span className="text-fg-subtle">{e.by ? `by ${e.by}` : ''}</span></li>)}{mine.length === 0 && <li className="text-fg-subtle">{t('common.empty')}</li>}</ul></Panel>
    </>
  )
}

export default function Page() { return <Suspense fallback={null}><Detail /></Suspense> }
void GET
