// 评测: tasksets, versions, batches, batch matrix, run detail.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { useStore } from '../../store'
import { GET, POST } from '../../api/client'
import { Button, ErrorLine, Input, KV, Panel, Select, Table, age, num, usd, when } from '../../ui'
import { BatchBadge, Short } from '../../ui/domain'
import type { BatchRow, TaskSetRow, Version } from '../../api/types'
import { navigate } from '../../layout/Shell'

function EvalPage() {
  const s = useStore(); const live = s.live
  useEffect(() => { const f = () => { void s.load.tasksets(); void s.load.versions(); void s.load.batches() }; f(); const i = setInterval(f, live ? 30000 : 8000); return () => clearInterval(i) }, [live])
  const [kind, setKind] = useState('baseline'), [ts, setTs] = useState(''), [ver, setVer] = useState(''), [k, setK] = useState('3'), [base, setBase] = useState(''), [msg, setMsg] = useState('')
  const tasksets = s.tasksets.data ?? [], versions = s.versions.data ?? [], batches = s.batches.data ?? []
  const loading = { ts: s.tasksets.data == null, v: s.versions.data == null, b: s.batches.data == null }
  const submit = async () => {
    try { const r = await POST<{ batch_id: string }>('/eval/batches', { kind, taskset: ts || tasksets[0]?.name, version: ver || versions[0]?.id, k: Number(k) || 3, base_batch: base || undefined, cascade: kind === 'compare' }); setMsg('批次 ' + r.batch_id); void s.load.batches() } catch (e) { setMsg((e as Error).message) }
  }
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-ui-xl font-semibold">评测</h1>
      {s.batches.error && <ErrorLine>{s.batches.error}</ErrorLine>}
      <Panel title="任务集"><Table cols={[{ h: '名称', cell: (t: TaskSetRow) => t.name }, { h: '版本', cell: (t: TaskSetRow) => t.version }, { h: '任务数', cell: (t: TaskSetRow) => t.n_tasks }, { h: '分组', cell: (t: TaskSetRow) => Object.entries(t.groups || {}).map(([a, b]) => `${a}:${b}`).join(' ') }, { h: '已检查', cell: (t: TaskSetRow) => t.checked ? <span className="text-success">是</span> : <span className="text-warning">否</span> },
        { h: '', cell: (t: TaskSetRow) => <Button size="sm" onClick={() => POST(`/eval/tasksets/${t.name}/check`, {}).then(() => s.load.batches())}>导入检查</Button> }]} rows={tasksets} loading={loading.ts} keyOf={t => t.name} /></Panel>
      <Panel title="版本"><Table cols={[{ h: 'ID', cell: (v: Version) => <Short id={v.id} n={10} /> }, { h: '标签', cell: (v: Version) => v.label }, { h: '父版本', cell: (v: Version) => <Short id={v.parent} n={10} /> }, { h: '层', cell: (v: Version) => (v.layers || []).join(', ') || '(默认)', wrap: true }, { h: '备注', cell: (v: Version) => (v.note || '').slice(0, 120), wrap: true, w: '30%' }, { h: '创建', cell: (v: Version) => age(v.created) }]} rows={versions} loading={loading.v} keyOf={v => v.id} /></Panel>
      <Panel title="新批次">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col text-ui-sm text-fg-subtle">类型<Select value={kind} onChange={e => setKind(e.target.value)}>{['baseline', 'compare', 'check'].map(x => <option key={x}>{x}</option>)}</Select></label>
          <label className="flex flex-col text-ui-sm text-fg-subtle">任务集<Select value={ts} onChange={e => setTs(e.target.value)}>{tasksets.map(t => <option key={t.name} value={t.name}>{t.name}</option>)}</Select></label>
          <label className="flex flex-col text-ui-sm text-fg-subtle">版本<Select value={ver} onChange={e => setVer(e.target.value)}>{versions.map(v => <option key={v.id} value={v.id}>{v.label} ({v.id.slice(2, 10)})</option>)}</Select></label>
          <label className="flex flex-col text-ui-sm text-fg-subtle">k<Input className="w-14" value={k} onChange={e => setK(e.target.value)} /></label>
          {kind === 'compare' && <label className="flex flex-col text-ui-sm text-fg-subtle">基线批次<Select value={base} onChange={e => setBase(e.target.value)}><option value="">(选)</option>{batches.filter(b => b.kind === 'baseline' && b.status === 'done').map(b => <option key={b.id} value={b.id}>{b.id} {b.taskset}</option>)}</Select></label>}
          <Button kind="primary" onClick={submit}>提交</Button>{msg && <span className="text-ui-sm text-fg-subtle">{msg}</span>}
        </div>
      </Panel>
      <Panel title="批次"><Table cols={[{ h: 'ID', cell: (b: BatchRow) => <a href={`#/batches/${b.id}`}>{b.id}</a> }, { h: '类型', cell: (b: BatchRow) => b.kind }, { h: '任务集', cell: (b: BatchRow) => b.taskset }, { h: '版本', cell: (b: BatchRow) => <Short id={b.version} n={10} /> }, { h: 'k', cell: (b: BatchRow) => b.k }, { h: '状态', cell: (b: BatchRow) => <BatchBadge s={b.status} /> },
        { h: '进度', cell: (b: BatchRow) => `${b.progress?.done ?? 0}/${b.progress?.total ?? 0}${b.progress?.infra_fail ? ` (基础设施失败 ${b.progress.infra_fail})` : ''}` }, { h: '标签', cell: (b: BatchRow) => b.label || '-', wrap: true }, { h: '创建', cell: (b: BatchRow) => age(b.created) }]}
        rows={[...batches].reverse()} loading={loading.b} keyOf={b => b.id} onRow={b => navigate(`#/batches/${b.id}`)} /></Panel>
    </div>
  )
}

function BatchPage({ params: [id] }: { params: string[] }) {
  const b = useStore(s => s.batch[id]), load = useStore(s => s.load)
  useEffect(() => { void load.batch(id); const i = setInterval(() => void load.batch(id), 5000); return () => clearInterval(i) }, [id])
  const d = b?.data
  if (!d) return b?.error ? <ErrorLine>{b.error}</ErrorLine> : <p className="text-ui-base text-fg-subtle">加载中</p>
  const st = d.stats || {}; const tasks = Object.keys(d.matrix || {}).sort(); const kmax = Math.max(1, ...tasks.map(t => d.matrix[t].length))
  const cellCls = (s: number | null) => s == null ? 'text-fg-subtlest' : s >= 1 ? 'text-success' : s <= 0 ? 'text-destructive' : 'text-warning'
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex items-center gap-2"><h1 className="text-ui-xl font-semibold">批次 {d.id}</h1><BatchBadge s={d.status} /><span className="text-ui-sm text-fg-subtle">{d.kind} · {d.taskset} · <Short id={d.version} n={10} /> · k={d.k}</span></header>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        {[['均值', num(st.mean)], ['avg@k', num(st.avg_k)], ['噪声带 δ', num(st.delta)], ['配对差', st.paired ? `${num(st.paired.diff)} [${num(st.paired.ci95[0])}, ${num(st.paired.ci95[1])}]` : '-'], ['接受', st.accept == null ? '-' : st.accept ? '是' : `否${st.reason ? ' · ' + st.reason : ''}`]].map(([k, v]) =>
          <div key={String(k)} className="rounded-lg border border-border bg-panel p-2"><div className="text-ui-lg font-medium">{v}</div><div className="text-ui-xs text-fg-subtle">{k}</div></div>)}
      </div>
      <Panel title="矩阵(任务 × 次)">
        <div className="overflow-x-auto"><table className="text-ui-sm"><thead><tr><th className="px-2 text-left text-fg-subtle">任务</th>{Array.from({ length: kmax }, (_, i) => <th key={i} className="px-2 text-fg-subtle">#{i + 1}</th>)}<th className="px-2 text-fg-subtle">费用</th></tr></thead>
          <tbody>{tasks.map(t => <tr key={t} className="border-t border-border"><td className="px-2 font-mono">{t}</td>{Array.from({ length: kmax }, (_, i) => { const c = d.matrix[t][i]; return <td key={i} className={`px-2 text-center ${c ? cellCls(c.score) : ''}`}>{c ? <a href={`#/runs/${c.run_id}`} className={cellCls(c.score)}>{c.infra_fail ? '!' : c.score == null ? '·' : c.score.toFixed(2)}</a> : ''}</td> })}<td className="px-2 text-fg-subtle">{usd(d.matrix[t].reduce((a, c) => a + (c.cost || 0), 0))}</td></tr>)}</tbody></table></div>
      </Panel>
    </div>
  )
}

function RunPage({ params: [id] }: { params: string[] }) {
  const [r, setR] = useState<Record<string, unknown> | null>(null); const [err, setErr] = useState('')
  useEffect(() => { GET<Record<string, unknown>>(`/eval/runs/${id}`).then(setR).catch(e => setErr((e as Error).message)) }, [id])
  if (!r) return err ? <ErrorLine>{err}</ErrorLine> : <p className="text-ui-base text-fg-subtle">加载中</p>
  const traj = r.traj as { messages?: { role: string; content: unknown }[]; info?: { exit_status?: string } } | null; const verify = r.verify as { exit: number; stdout_tail: string; score: number } | undefined
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-ui-xl font-semibold">Run {id}</h1>
      <Panel><KV rows={[['任务', String(r.task)], ['版本', <Short id={String(r.version)} n={10} />], ['分数', num(r.score)], ['通过', String(r.pass)], ['退出码', String(r.agent_exit)], ['费用', usd(r.cost)], ['token', `${r.tokens_in}/${r.tokens_out}`], ['耗时', num(r.elapsed_s, 1) + 's'], ['作弊信号', JSON.stringify(r.cheat)], ['批次', <a href={`#/batches/${r.batch}`}>{String(r.batch)}</a>], ['创建', when(r.created as number)]]} /></Panel>
      <Panel title={`轨迹 ${traj?.info?.exit_status || ''}`}><div className="space-y-2">{(traj?.messages || []).map((m, i) => <div key={i} className="rounded border border-border p-2"><div className="text-ui-xs text-fg-subtle">{m.role}</div><pre className="out max-h-64 overflow-auto">{typeof m.content === 'string' ? m.content : JSON.stringify(m.content, null, 1)}</pre></div>)}</div></Panel>
      <Panel title={`验收 exit ${verify?.exit ?? '-'} score ${verify?.score ?? '-'}`}><pre className="out max-h-80 overflow-auto">{verify?.stdout_tail}</pre></Panel>
    </div>
  )
}

export const evaluation: Plugin = {
  id: 'eval',
  activate(ctx) {
    ctx.nav({ id: 'eval', label: '评测', href: '#/eval', order: 30 })
    ctx.route({ id: 'eval', pattern: /^#\/eval$/, component: EvalPage, nav: 'eval' })
    ctx.route({ id: 'batch', pattern: /^#\/batches\/([^/?]+)$/, component: BatchPage, nav: 'eval' })
    ctx.route({ id: 'run', pattern: /^#\/runs\/([^/?]+)$/, component: RunPage, nav: 'eval' })
  },
}
