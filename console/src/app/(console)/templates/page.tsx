'use client'
import { useState } from 'react'
import { age, bytes, mib } from '@/lib/format'
import { useI18n } from '@/lib/i18n'
import { useAuth } from '@/lib/auth'
import { useGet } from '@/lib/query'
import { Badge, Button, ErrorLine, Panel, Table } from '@/components/ui'

type Template = { key: string; name?: string; node: string; template?: string; vcpu?: number; mem?: number; from?: string; created?: number; mem_mib?: number; upper_mib?: number }
type Layer = { id: string; name: string; node: string; bytes?: number; from?: string }
type Build = { id: string; kind: string; input: Record<string, unknown>; node: string; status: string; result?: Record<string, unknown>; error?: string; created: number }

function BuildLog({ id, running }: { id: string; running: boolean }) {
  const { t } = useI18n()
  const lg = useGet<{ lines: string[] }>(`/builds/${id}/log`, { every: running ? 2000 : undefined, retry: false })
  return <Panel title={`${t('builds.log')}: ${id}`}>{lg.error ? <p className="text-ui-sm text-fg-subtle">{t('builds.log.none')}</p> : <pre className="max-h-64 overflow-auto whitespace-pre-wrap font-mono text-ui-sm">{(lg.data?.lines ?? []).join('\n') || '…'}</pre>}</Panel>
}

export default function Templates() {
  const { t } = useI18n(), { role } = useAuth()
  const tp = useGet<{ templates: Template[] }>('/templates'), ly = useGet<{ layers: Layer[] }>(role === 'operator' ? '/layers' : null), bd = useGet<{ builds: Build[] }>('/builds', { every: 5000 })
  const [open, setOpen] = useState('')
  const nodes = [...new Set((tp.data?.templates ?? []).map(x => x.node))], keys = [...new Set((tp.data?.templates ?? []).map(x => x.key))], has = new Set((tp.data?.templates ?? []).map(x => `${x.node}:${x.key}`))
  return (
    <>
      <h1 className="text-ui-xl font-medium">{t('nav.templates')}</h1>
      {(tp.error || ly.error || bd.error) && <ErrorLine>{((tp.error || ly.error || bd.error) as Error).message}</ErrorLine>}
      {nodes.length > 1 && <Panel title="distribution"><table className="text-ui-sm"><thead><tr><th className="pr-3 text-left font-normal text-fg-subtle">template</th>{nodes.map(n => <th key={n} className="px-2 font-normal text-fg-subtle">{n}</th>)}</tr></thead>
        <tbody>{keys.map(k => <tr key={k}><td className="pr-3 font-mono">{k}</td>{nodes.map(n => <td key={n} className="px-2 text-center">{has.has(`${n}:${k}`) ? <span className="text-success">●</span> : <span className="text-fg-subtlest">○</span>}</td>)}</tr>)}</tbody></table></Panel>}
      <Panel title={t('nav.templates')}><Table cols={[
        { h: 'key', cell: x => <span className="font-mono">{x.key}</span> }, { h: 'node', cell: x => x.node }, { h: 'spec', cell: x => `${x.template ?? '-'} ${x.vcpu ?? '-'}c ${x.mem ?? '-'}MiB` }, { h: 'from', cell: x => x.from || 'build' },
        { h: 'mem / disk', cell: x => `${mib(x.mem_mib)} / ${mib(x.upper_mib)}` }, { h: '', cell: x => age(x.created) },
      ]} rows={tp.data?.templates ?? null} keyOf={x => `${x.node}:${x.key}`} empty={t('common.empty')} /></Panel>
      {role === 'operator' && <Panel title="layers"><Table cols={[
        { h: 'id', cell: x => <span className="font-mono">{x.id}</span> }, { h: 'name', cell: x => x.name }, { h: 'node', cell: x => x.node }, { h: 'size', cell: x => bytes(x.bytes) }, { h: 'from', cell: x => x.from ?? '-' },
      ]} rows={ly.data?.layers ?? null} keyOf={x => `${x.node}:${x.id}`} empty={t('common.empty')} /></Panel>}
      <Panel title="builds"><Table cols={[
        { h: 'id', cell: x => <span className="font-mono">{x.id}</span> }, { h: 'kind', cell: x => x.kind }, { h: 'input', cell: x => String(x.input.image ?? x.input.sandbox ?? x.input.name ?? '') }, { h: 'node', cell: x => x.node },
        { h: '', cell: x => <Badge tone={x.status === 'done' ? 'success' : x.status === 'failed' ? 'danger' : 'warning'}>{x.status}</Badge> }, { h: 'result', cell: x => x.error || String(x.result?.id ?? x.result?.key ?? '') }, { h: '', cell: x => age(x.created) },
        { h: '', cell: x => x.kind === 'oci' ? <Button size="sm" onClick={() => setOpen(open === x.id ? '' : x.id)}>{t('builds.log')}</Button> : null },
      ]} rows={bd.data?.builds ?? null} keyOf={x => x.id} empty={t('common.empty')} /></Panel>
      {open && <BuildLog id={open} running={(bd.data?.builds ?? []).find(b => b.id === open)?.status === 'running'} />}
    </>
  )
}
