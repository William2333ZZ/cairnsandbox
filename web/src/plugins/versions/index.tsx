// 版本树: CairnSandbox versions as a tree (parent edges), with each node's latest batch mean and the archive verdict; plus the
// evolve loop's iteration log. Registered as its own plugin so the eval page stays a list and this stays a tree.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { GET, POST } from '../../api/client'
import { Badge, ErrorLine, Panel, Table, age, num, usd, when, type Tone } from '../../ui'
import { Short } from '../../ui/domain'

interface TreeNode { id: string; label: string; parent?: string | null; created: number; layers: string[]; note: string; status: 'stable' | 'candidate' | 'retired' | 'superseded'; mean?: number | null; accept?: boolean | null; reason?: string | null; batch?: string | null; taskset?: string | null; archive: { accepted?: boolean | null; note?: string; iteration?: string } }
interface Iteration { id: string; parent: string; version?: string | null; result?: string; diff_lines?: number; changes?: string; critic?: { ok: boolean; reasons: string[]; changed_lines: number }; propose?: { cost?: number; tokens_in?: number; tokens_out?: number; exit?: number }; compare?: string; started?: number; ended?: number; taskset?: string; k?: number; manifest?: { changes: { why?: string; component?: string }[] }; attribution?: { fixed: string[]; broke: string[]; lost_stable: string[]; predicted_fixes: string[]; hit_fixes: string[]; unpredicted_broke: string[]; solved_by_parent: number } }
interface EvolveState { spent_usd: number; nodes: unknown[]; iterations: Iteration[] }

const STATUS: Record<TreeNode['status'], Tone> = { stable: 'success', candidate: 'neutral', retired: 'neutral', superseded: 'neutral' }

interface AskStats { useful: number; unnecessary: number; unanswered: number; missed: number; precision?: number | null; recall?: number | null; f1?: number | null }
interface AgentView { ask?: AskStats; version?: { id: string; label?: string; layers: string[]; at?: number } | null; pinned: boolean; previous?: { id: string; label?: string } | null; updates: { id: string; label?: string; at: number; changes?: string; taskset?: string; stats?: { mean?: number; parent_mean?: number; cost?: number; parent_cost?: number } }[] }
function AgentVersion() {
  const [a, setA] = useState<AgentView | null>(null); const [err, setErr] = useState('')
  const load = async () => { try { setA(await GET<AgentView>('/intake/agent')); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load() }, [])
  const set = async (body: Record<string, unknown>) => { try { await POST('/intake/agent/version', body); await load() } catch (e) { setErr((e as Error).message) } }
  return (
    <Panel title="我的 agent 用的版本">
      {err && <ErrorLine>{err}</ErrorLine>}
      {a && <div className="flex flex-col gap-2 text-ui-base">
        <div className="flex flex-wrap items-center gap-3"><span className="font-medium text-fg">{a.version ? (a.version.label || a.version.id) : '部署默认(run-node 的层)'}</span>{a.version && <span className="font-mono text-ui-sm text-fg-subtlest">{a.version.layers.join(' + ')}</span>}<Badge tone={a.pinned ? 'warning' : 'success'}>{a.pinned ? '已钉住,不自动更新' : '跟随稳定版'}</Badge></div>
        <div className="flex flex-wrap gap-2">
          {a.previous && <button onClick={() => set({ version: 'previous', pin: true })} className="h-7 rounded-md border border-border px-2 text-ui-sm text-fg hover:bg-hover">退回到 {a.previous.label || a.previous.id}</button>}
          {a.pinned ? <button onClick={() => set({ version: a.version, pin: false })} className="h-7 rounded-md border border-border px-2 text-ui-sm text-fg hover:bg-hover">解除钉住</button> : a.version && <button onClick={() => set({ version: a.version, pin: true })} className="h-7 rounded-md border border-border px-2 text-ui-sm text-fg hover:bg-hover">钉住这一版</button>}
          {a.version && <button onClick={() => set({ version: null, pin: false })} className="h-7 rounded-md px-2 text-ui-sm text-fg-subtle hover:bg-hover hover:text-fg">回到部署默认</button>}
        </div>
        {a.ask && (a.ask.useful + a.ask.unnecessary + a.ask.missed > 0) && <div className="text-ui-sm text-fg-subtle" title="用户回答了并给出实质内容的问题算有用;回答“你定”算不该问;没问过、后来被纠正的算该问没问。">该问就问:有用 {a.ask.useful} · 不该问 {a.ask.unnecessary} · 该问没问 {a.ask.missed}{a.ask.f1 != null ? ` · F1 ${num(a.ask.f1)}` : ''}</div>}
        {a.updates.length ? <div className="mt-1 flex flex-col gap-1 text-ui-sm text-fg-subtle">{[...a.updates].reverse().slice(0, 8).map(u => <div key={u.id + u.at}><span className="text-fg">{u.label || u.id}</span> · {when(u.at)}{u.stats?.mean != null ? ` · ${u.taskset} 均值 ${num(u.stats.parent_mean)} → ${num(u.stats.mean)}` : ''}{u.changes ? ` · ${u.changes.slice(0, 80)}` : ''}</div>)}</div> : <p className="text-ui-sm text-fg-subtlest">还没有收到过更新。</p>}
      </div>}
    </Panel>
  )
}

function VersionsPage() {
  const [tree, setTree] = useState<TreeNode[] | null>(null); const [ev, setEv] = useState<EvolveState | null>(null); const [err, setErr] = useState('')
  useEffect(() => {
    const f = () => Promise.all([GET<{ nodes: TreeNode[] }>('/eval/versions/tree'), GET<EvolveState>('/eval/evolve')]).then(([t, e]) => { setTree(t.nodes); setEv(e); setErr('') }).catch(e => setErr((e as Error).message))
    void f(); const i = setInterval(f, 10000); return () => clearInterval(i)
  }, [])
  const byParent = new Map<string | null, TreeNode[]>()
  for (const n of tree ?? []) { const k = n.parent && tree?.some(x => x.id === n.parent) ? n.parent : null; byParent.set(k, [...(byParent.get(k) ?? []), n]) }
  const render = (parent: string | null, depth: number): React.ReactNode[] => (byParent.get(parent) ?? []).sort((a, b) => a.created - b.created).flatMap(n => [
    <div key={n.id} className="flex items-center gap-2 border-b border-border py-1 text-ui-base" style={{ paddingLeft: depth * 20 }}>
      <span className="text-fg-subtlest">{depth ? '└' : '•'}</span>
      <a href={n.batch ? `#/batches/${n.batch}` : '#/eval'}><Short id={n.id} n={10} /></a>
      <span className="font-medium">{n.label}</span>
      <Badge tone={STATUS[n.status]}>{n.status}</Badge>
      <span className="text-fg-subtle">{n.mean != null ? `均值 ${num(n.mean)}` : '未评'}{n.taskset ? ` · ${n.taskset}` : ''}</span>
      {n.accept != null && <Badge tone={n.accept ? 'success' : 'warning'}>{n.accept ? '接受' : `未接受${n.reason ? ' · ' + n.reason : ''}`}</Badge>}
      <span className="flex-1 truncate text-fg-subtlest" title={n.note}>{n.note}</span>
      <span className="text-ui-xs text-fg-subtlest">{age(n.created)}</span>
    </div>,
    ...render(n.id, depth + 1),
  ])
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-ui-xl font-semibold">版本</h1>
      <AgentVersion />
      {err && <ErrorLine>{err}</ErrorLine>}
      <Panel title="CairnSandbox 版本(父 → 子;stable = 当前种子或被接受;retired = 比较后未接受;superseded = 被新种子取代的旧种子)">
        {tree == null ? <p className="text-ui-base text-fg-subtlest">加载中</p> : tree.length ? <div>{render(null, 0)}</div> : <p className="text-ui-base text-fg-subtle">还没有版本。</p>}
      </Panel>
      <Panel title={<>自动迭代 {ev ? <span className="font-normal text-fg-subtle">· 已花 {usd(ev.spent_usd)}</span> : null}</>}>
        <Table cols={[{ h: '迭代', cell: (i: Iteration) => <Short id={i.id} /> }, { h: '父代', cell: (i: Iteration) => <Short id={i.parent} n={10} /> }, { h: '候选', cell: (i: Iteration) => i.version ? <Short id={i.version} n={10} /> : '-' },
          { h: '结果', cell: (i: Iteration) => <span className={i.result?.startsWith('accepted') ? 'text-success' : i.result?.startsWith('rejected') ? 'text-destructive' : ''}>{i.result || '进行中'}</span>, wrap: true, w: '30%' },
          { h: 'diff 行', cell: (i: Iteration) => i.diff_lines ?? '-' }, { h: '提案费用', cell: (i: Iteration) => usd(i.propose?.cost) }, { h: '比较批次', cell: (i: Iteration) => i.compare ? <a href={`#/batches/${i.compare}`}>{i.compare}</a> : '-' },
          { h: '预计 → 实际', cell: (i: Iteration) => i.attribution ? <span title={`预计修好 ${i.attribution.predicted_fixes.join(', ') || '无'};实际修好 ${i.attribution.fixed.join(', ') || '无'};变差 ${i.attribution.broke.join(', ') || '无'}`}>{i.attribution.hit_fixes.length}/{i.attribution.predicted_fixes.length} 中 · 修 {i.attribution.fixed.length} · 坏 {i.attribution.broke.length}{i.attribution.lost_stable.length ? <span className="text-destructive"> · 丢稳定 {i.attribution.lost_stable.length}</span> : null}</span> : '-' },
          { h: '改动说明', cell: (i: Iteration) => <span className="line-clamp-3">{i.manifest?.changes?.[0]?.why ? `${i.changes?.slice(0, 80) || ''} 〔因为 ${i.manifest.changes[0].why}〕` : (i.changes || '')}</span>, wrap: true, w: '30%' }, { h: '时间', cell: (i: Iteration) => when(i.started) }]}
          rows={ev ? [...ev.iterations].reverse() : null} keyOf={i => i.id} empty="还没有迭代。" />
      </Panel>
    </div>
  )
}

export const versions: Plugin = {
  id: 'versions', requires: ['eval'],
  activate(ctx) {
    ctx.nav({ id: 'versions', label: '版本', href: '#/versions', order: 31 })
    ctx.route({ id: 'versions', pattern: /^#\/versions$/, component: VersionsPage, nav: 'versions' })
  },
}
