// 云: the sandbox cloud's own objects (docs/002 沙箱侧的领域): accounts, projects, members and quotas (1); bills and prices (7); volumes (5); templates,
// layers and builds (4). Nodes keep their own page (3). Everything here is a thin view over CONTRACT §1; the shapes are the server's.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { DEL, GET, POST, PUT, api, me, project, setProject } from '../../api/client'
import { Badge, Button, ErrorLine, Input, Panel, Select, Table, age, usd, when } from '../../ui'

type Me = { user: string | null; admin: boolean; auth: boolean; account?: string; role?: string; projects?: string[] }
type Project = { id: string; account: string; name: string; created: number }
type Member = { id: string; user: string; name: string; role: string; created: number; expires?: number }
type AuditEvent = { op: string; ts: number; project?: string; sandbox?: string; by?: string; replica?: string; [k: string]: unknown }
type Quota = { project: string; max_sandboxes: number; max_storage_gib: number; daily_usd: number }
type Line = { kind: string; amount: number; unit: string; cost_usd: number; events: number }
type Statement = { project: string; from: number; to: number; lines: Line[]; total_usd: number; sandboxes: number }
type Settled = Statement & { id: string; created: number }
type Volume = { id: string; project: string; node: string; name: string; size_mib: number; created: number; attached?: Record<string, string>; allocated_mib?: number }
type Template = { key: string; name?: string; node: string; template?: string; vcpu?: number; mem?: number; from?: string; created?: number; mem_mib?: number; upper_mib?: number }
type Layer = { id: string; name: string; node: string; bytes?: number; from?: string }
type Build = { id: string; project: string; kind: string; input: Record<string, unknown>; node: string; status: string; result?: Record<string, unknown>; error?: string; created: number; finished?: number }

const TABS = [['overview', '概览'], ['projects', '项目与成员'], ['billing', '账单'], ['volumes', '卷'], ['images', '模板与构建'], ['audit', '审计']] as const
type Tab = (typeof TABS)[number][0]

function useLoad<T>(f: () => Promise<T>, deps: unknown[] = []) {
  const [d, setD] = useState<T | null>(null); const [err, setErr] = useState('')
  const load = async () => { try { setD(await f()); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load() }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  return { d, err, load, setErr }
}

function QuotaRow({ p, canEdit }: { p: string; canEdit: boolean }) {
  const q = useLoad(() => GET<Quota>(`/projects/${p}/quota`), [p]); const [draft, setDraft] = useState<Quota | null>(null)
  const cur = draft ?? q.d
  if (!cur) return null
  const n = (k: keyof Quota) => <Input type="number" className="w-24" value={String(cur[k])} disabled={!canEdit} onChange={e => setDraft({ ...cur, [k]: Number(e.target.value) })} />
  return (
    <div className="flex flex-wrap items-center gap-3 text-ui-base">
      <span className="text-fg-subtle">配额(0 = 不限)</span><label className="flex items-center gap-1">沙箱 {n('max_sandboxes')}</label><label className="flex items-center gap-1">存储 GiB {n('max_storage_gib')}</label><label className="flex items-center gap-1">每日 $ {n('daily_usd')}</label>
      {draft && canEdit && <Button size="sm" kind="primary" onClick={async () => { try { await PUT(`/projects/${p}/quota`, draft); setDraft(null); await q.load() } catch (e) { q.setErr((e as Error).message) } }}>保存</Button>}
      {q.err && <ErrorLine>{q.err}</ErrorLine>}
    </div>
  )
}

type NetPolicy = { mode?: string; allow_hosts?: string[]; limits?: { requests_per_min?: number; max_bytes?: number } }

function NetworkDefault({ p, canEdit }: { p: string; canEdit: boolean }) {
  const n = useLoad(() => GET<{ network: NetPolicy | null }>(`/projects/${p}/network`), [p]); const [draft, setDraft] = useState<NetPolicy | null>(null)
  const cur = draft ?? n.d?.network ?? null
  const save = async (net: NetPolicy | null) => { try { if (net) await PUT(`/projects/${p}/network`, net); else await DEL(`/projects/${p}/network`); setDraft(null); await n.load() } catch (e) { n.setErr((e as Error).message) } }
  return (
    <div className="mt-1 flex flex-wrap items-center gap-3 text-ui-base">
      <span className="text-fg-subtle">默认网络(创建时没写就用它)</span>
      {cur ? <>
        <Select value={cur.mode || 'closed'} disabled={!canEdit} onChange={e => setDraft({ ...cur, mode: e.target.value })}><option value="closed">closed</option><option value="allowlist">allowlist</option><option value="audited">audited</option></Select>
        <Input className="w-56" value={(cur.allow_hosts || []).join(',')} disabled={!canEdit} placeholder="放行的域名,逗号分隔" onChange={e => setDraft({ ...cur, allow_hosts: e.target.value.split(',').map(x => x.trim()).filter(Boolean) })} />
        <label className="flex items-center gap-1">请求/分 <Input type="number" className="w-20" value={String(cur.limits?.requests_per_min ?? 0)} disabled={!canEdit} onChange={e => setDraft({ ...cur, limits: { ...(cur.limits || {}), requests_per_min: Number(e.target.value) } })} /></label>
        {draft && canEdit && <Button size="sm" kind="primary" onClick={() => save(draft)}>保存</Button>}
        {canEdit && n.d?.network && <button className="text-ui-sm text-fg-subtle hover:text-destructive" onClick={() => save(null)}>删掉默认</button>}
      </> : <>
        <span className="text-fg-subtlest">没有(每台沙箱自己带,不带则 closed)</span>
        {canEdit && <Button size="sm" onClick={() => setDraft({ mode: 'closed', allow_hosts: [], limits: {} })}>设一个</Button>}
      </>}
      {n.err && <ErrorLine>{n.err}</ErrorLine>}
    </div>
  )
}

function Members({ p, canEdit }: { p: string; canEdit: boolean }) {
  const m = useLoad(() => GET<{ members: Member[] }>(`/projects/${p}/members`), [p]); const [u, setU] = useState(''); const [role, setRole] = useState('member'); const [issued, setIssued] = useState<{ user: string; token: string } | null>(null)
  const add = async () => { try { const r = await POST<{ user: string; token: string }>(`/projects/${p}/members`, { user: u.trim(), role }); setIssued(r); setU(''); await m.load() } catch (e) { m.setErr((e as Error).message) } }
  return (
    <div className="mt-2">
      <div className="mb-1 text-ui-caption text-fg-subtle">成员(整个账户共用)</div>
      {m.d?.members.map(x => <div key={x.id} className="flex items-center gap-3 py-0.5 text-ui-base"><span className="font-mono">{x.user}</span><Badge tone={x.role === 'owner' ? 'brand' : 'neutral'}>{x.role}</Badge><span className="text-fg-subtle">{x.name}</span><div className="flex-1" />
        {canEdit && <button className="text-ui-sm text-fg-subtle hover:text-fg" title="发一个新令牌,旧的立刻失效" onClick={async () => { if (window.confirm(`给 ${x.user} 换令牌?旧令牌立刻失效`)) { try { const ttl = window.prompt('有效期(秒;空 = 不过期)', ''); const r = await POST<{ user: string; token: string }>(`/projects/${p}/members/${x.user}/rotate`, ttl ? { ttl_s: Number(ttl) } : {}); setIssued(r); await m.load() } catch (e) { m.setErr((e as Error).message) } } }}>换令牌</button>}
        {canEdit && <button className="text-ui-sm text-fg-subtle hover:text-destructive" onClick={async () => { if (window.confirm(`移除成员 ${x.user}?`)) { await DEL(`/projects/${p}/members/${x.user}`); await m.load() } }}>移除</button>}</div>)}
      {canEdit && <div className="mt-1 flex gap-2"><Input className="w-40" value={u} onChange={e => setU(e.target.value)} placeholder="用户名(小写)" /><Select value={role} onChange={e => setRole(e.target.value)}><option value="member">member</option><option value="owner">owner</option></Select><Button size="sm" kind="primary" disabled={!u.trim()} onClick={add}>加入</Button></div>}
      {issued && <div className="mt-1 rounded border border-border bg-surface px-2 py-1 text-ui-sm">{issued.user} 的令牌(只显示这一次):<code className="ml-1 select-all">{issued.token}</code></div>}
      {m.err && <ErrorLine>{m.err}</ErrorLine>}
    </div>
  )
}

type NetRequest = { id: string; sandbox: string; project: string; by: string; network: Record<string, unknown>; created: number; status: string; decided_by?: string }

function NetworkRequests({ canDecide }: { canDecide: boolean }) {
  const r = useLoad(() => GET<{ requests: NetRequest[] }>('/network/requests'), [])
  const decide = async (id: string, what: 'approve' | 'deny') => { try { await POST(`/network/requests/${id}/${what}`); await r.load() } catch (e) { r.setErr((e as Error).message) } }
  if (!r.d || r.d.requests.length === 0) return null
  return (
    <Panel title={`等你批的出口放宽(${r.d.requests.length})`}>
      <p className="mb-2 text-ui-sm text-fg-subtle">成员把一台沙箱的出口放宽(更松的模式、新域名、新 allow 规则、更高限额)要 owner 点头;收紧不用。批准才推到节点。</p>
      {r.err && <ErrorLine>{r.err}</ErrorLine>}
      <Table cols={[{ h: '沙箱', cell: (x: NetRequest) => <a className="font-mono" href={`#/sandboxes/${x.sandbox}`}>{x.sandbox}</a> }, { h: '项目', cell: x => x.project }, { h: '谁', cell: x => x.by }, { h: '要什么', cell: x => <code className="text-ui-sm">{JSON.stringify(x.network).slice(0, 120)}</code> }, { h: '', cell: x => age(x.created) },
        { h: '', cell: x => canDecide ? <span className="flex gap-1"><Button size="sm" kind="primary" onClick={() => decide(x.id, 'approve')}>批准</Button><Button size="sm" onClick={() => decide(x.id, 'deny')}>拒绝</Button></span> : <Badge>等 owner</Badge> }]} rows={r.d.requests} keyOf={x => x.id} empty="" />
    </Panel>
  )
}

function ProjectsTab({ who }: { who: Me }) {
  const acct = who.account || 'default'; const canEdit = who.admin || who.role === 'owner'
  const ps = useLoad(() => GET<{ projects: Project[] }>(`/accounts/${acct}/projects`), [acct]); const [name, setName] = useState('')
  const accounts = useLoad(() => who.admin ? GET<{ accounts: { id: string; name: string; created: number }[] }>('/accounts') : Promise.resolve({ accounts: [] }), [who.admin]); const [newAcct, setNewAcct] = useState('')
  return (
    <div className="space-y-4">
      <NetworkRequests canDecide={canEdit} />
      {who.admin && <Panel title="账户(部署者)">
        <div className="flex flex-wrap gap-2">{accounts.d?.accounts.map(a => <Badge key={a.id} tone={a.id === acct ? 'brand' : 'neutral'}>{a.id}</Badge>)}</div>
        <div className="mt-2 flex gap-2"><Input className="w-48" value={newAcct} onChange={e => setNewAcct(e.target.value)} placeholder="账户 id(小写)" /><Button size="sm" kind="primary" disabled={!newAcct.trim()} onClick={async () => { try { await POST('/accounts', { id: newAcct.trim() }); setNewAcct(''); await accounts.load() } catch (e) { accounts.setErr((e as Error).message) } }}>新建账户</Button></div>
        {accounts.err && <ErrorLine>{accounts.err}</ErrorLine>}
      </Panel>}
      <Panel title={`账户 ${acct} 的项目`} actions={canEdit && <div className="flex gap-2"><Input className="w-40" value={name} onChange={e => setName(e.target.value)} placeholder="项目名" /><Button size="sm" kind="primary" disabled={!name.trim()} onClick={async () => { try { await POST(`/accounts/${acct}/projects`, { name: name.trim() }); setName(''); await ps.load() } catch (e) { ps.setErr((e as Error).message) } }}>新建项目</Button></div>}>
        <p className="mb-2 text-ui-sm text-fg-subtle">沙箱、卷、构建都归一个项目;配额按项目。你是 {who.role || (who.admin ? 'admin' : '-')}。</p>
        {ps.err && <ErrorLine>{ps.err}</ErrorLine>}
        {ps.d?.projects.map(p => <div key={p.id} className="mb-3 border-b border-border pb-3 last:border-0"><div className="mb-1 flex items-center gap-2 text-ui-base font-medium"><span className="font-mono">{p.id}</span><span className="text-fg-subtle">{p.name}</span><span className="text-ui-sm text-fg-subtlest">{when(p.created)}</span></div><QuotaRow p={p.id} canEdit={canEdit} /><NetworkDefault p={p.id} canEdit={canEdit} /><Members p={p.id} canEdit={canEdit} /></div>)}
      </Panel>
    </div>
  )
}

function BillingTab({ who }: { who: Me }) {
  const projects = who.projects || ['default']; const [p, setP] = useState(projects[0] || 'default')
  const days = 30; const from = Math.floor(Date.now() / 1000) - days * 86400
  const today = useLoad(() => GET<{ spent_usd: number }>(`/metering/projects/${p}/today`), [p])
  const st = useLoad(() => GET<Statement>(`/metering/projects/${p}/statement?from=${from}`), [p])
  const prices = useLoad(() => GET<{ version: number; per_unit: Record<string, number> }>('/metering/prices'), []); const [pd, setPd] = useState<Record<string, number> | null>(null)
  const settled = useLoad(() => GET<{ statements: Settled[] }>(`/metering/projects/${p}/statements`), [p]); const canSettle = who.admin || who.role === 'owner'
  const settle = async () => { try { const last = settled.d?.statements[0]; await POST(`/metering/projects/${p}/statements`, { from: last ? last.to : 0, to: Math.floor(Date.now() / 1000) }); await settled.load() } catch (e) { settled.setErr((e as Error).message) } }
  return (
    <div className="space-y-4">
      <Panel title="账单" actions={<Select value={p} onChange={e => setP(e.target.value)}>{projects.map(x => <option key={x} value={x}>{x}</option>)}</Select>}>
        <div className="mb-2 flex gap-4 text-ui-base"><span>今天 <b>{usd(today.d?.spent_usd)}</b></span><span>近 {days} 天 <b>{usd(st.d?.total_usd)}</b></span><span className="text-fg-subtle">{st.d?.sandboxes ?? 0} 台沙箱</span>{who.admin && <Button size="sm" onClick={async () => { await POST('/metering/tick'); await today.load(); await st.load() }}>现在计一次</Button>}</div>
        {(today.err || st.err) && <ErrorLine>{today.err || st.err}</ErrorLine>}
        <Table cols={[{ h: '用量', cell: (l: Line) => l.kind }, { h: '量', cell: l => l.amount.toLocaleString(undefined, { maximumFractionDigits: 0 }) }, { h: '条数', cell: l => l.events }, { h: '费用', cell: l => usd(l.cost_usd) }]} rows={st.d?.lines} keyOf={l => l.kind} empty="这段时间没有用量" />
      </Panel>
      <Panel title="已结算" actions={<div className="flex gap-2">{canSettle && <Button size="sm" onClick={settle}>结算到现在</Button>}<Button size="sm" onClick={async () => { try { const b = await api<ArrayBuffer>('GET', `/metering/projects/${p}/statement?from=${from}&format=csv`, undefined, { raw: true }); const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([b], { type: 'text/csv' })); a.download = `${p}-statement.csv`; a.click(); URL.revokeObjectURL(a.href) } catch (e) { settled.setErr((e as Error).message) } }}>近 {days} 天 CSV</Button></div>}>
        <p className="mb-2 text-ui-sm text-fg-subtle">结算把一段时间的账单冻成一行,之后价格或用量再变也不改它;下一次从上一次的结尾接着算。</p>
        {settled.err && <ErrorLine>{settled.err}</ErrorLine>}
        <Table cols={[{ h: 'id', cell: (x: Settled) => <span className="font-mono">{x.id}</span> }, { h: '从', cell: x => when(x.from) }, { h: '到', cell: x => when(x.to) }, { h: '沙箱', cell: x => x.sandboxes }, { h: '费用', cell: x => usd(x.total_usd) }, { h: '结算于', cell: x => age(x.created) }]} rows={settled.d?.statements} keyOf={x => x.id} empty="还没有结算过" />
      </Panel>
      <Panel title="价目表" actions={who.admin && pd && <Button size="sm" kind="primary" onClick={async () => { try { await PUT('/metering/prices', pd); setPd(null); await prices.load() } catch (e) { prices.setErr((e as Error).message) } }}>保存为新版本</Button>}>
        <p className="mb-2 text-ui-sm text-fg-subtle">每单位美元;版本 {prices.d?.version ?? '-'}。模型 token 由模型代理按模型定价,不在这里。</p>
        {prices.err && <ErrorLine>{prices.err}</ErrorLine>}
        <div className="flex flex-wrap gap-3">{Object.entries(pd ?? prices.d?.per_unit ?? {}).map(([k, v]) => <label key={k} className="flex items-center gap-1 text-ui-base">{k}<Input className="w-36" value={String(v)} disabled={!who.admin} onChange={e => setPd({ ...(pd ?? prices.d?.per_unit ?? {}), [k]: Number(e.target.value) })} /></label>)}</div>
      </Panel>
    </div>
  )
}

function VolumesTab({ who }: { who: Me }) {
  const v = useLoad(() => GET<{ volumes: Volume[] }>('/volumes'), []); const [name, setName] = useState(''); const [size, setSize] = useState('1024'); const [p, setP] = useState((who.projects || ['default'])[0] || 'default')
  return (
    <Panel title="卷" actions={<div className="flex gap-2"><Select value={p} onChange={e => setP(e.target.value)}>{(who.projects || ['default']).map(x => <option key={x} value={x}>{x}</option>)}</Select><Input className="w-32" value={name} onChange={e => setName(e.target.value)} placeholder="名字" /><Input className="w-20" value={size} onChange={e => setSize(e.target.value)} placeholder="MiB" /><Button size="sm" kind="primary" disabled={!name.trim()} onClick={async () => { try { await POST('/volumes', { name: name.trim(), size_mib: Number(size), project: p }); setName(''); await v.load() } catch (e) { v.setErr((e as Error).message) } }}>新建</Button></div>}>
      <p className="mb-2 text-ui-sm text-fg-subtle">独立生命周期的 ext4 镜像,住在一个节点上;创建沙箱时附上(一个写者或多个读者)。挂着的卷不能删。</p>
      {v.err && <ErrorLine>{v.err}</ErrorLine>}
      <Table cols={[{ h: 'id', cell: (x: Volume) => <span className="font-mono">{x.id}</span> }, { h: '名字', cell: x => x.name }, { h: '项目', cell: x => x.project }, { h: '节点', cell: x => x.node }, { h: '大小 MiB', cell: x => `${x.allocated_mib ?? '-'} / ${x.size_mib}` },
        { h: '挂在', cell: x => Object.keys(x.attached || {}).length ? Object.entries(x.attached || {}).map(([s, m]) => `${s} (${m})`).join(', ') : <span className="text-fg-subtlest">无</span> }, { h: '', cell: x => <button className="text-ui-sm text-fg-subtle hover:text-destructive" onClick={async () => { if (window.confirm(`删除卷 ${x.name}?`)) { try { await DEL(`/volumes/${x.id}`); await v.load() } catch (e) { v.setErr((e as Error).message) } } }}>删除</button> }]} rows={v.d?.volumes} keyOf={x => x.id} empty="还没有卷" />
    </Panel>
  )
}

function ImagesTab({ who }: { who: Me }) {
  const t = useLoad(() => GET<{ templates: Template[] }>('/templates'), []); const l = useLoad(() => GET<{ layers: Layer[] }>('/layers'), []); const b = useLoad(() => GET<{ builds: Build[] }>('/builds'), [])
  const [kind, setKind] = useState<'oci' | 'commit'>('oci'); const [image, setImage] = useState(''); const [name, setName] = useState(''); const [sandbox, setSandbox] = useState('')
  useEffect(() => { const i = setInterval(() => { if (b.d?.builds.some(x => x.status === 'running')) { void b.load(); void t.load(); void l.load() } }, 3000); return () => clearInterval(i) }, [b.d]) // eslint-disable-line react-hooks/exhaustive-deps
  const start = async () => { try { await POST('/builds', kind === 'oci' ? { kind, image: image.trim(), name: name.trim() || undefined } : { kind, sandbox: sandbox.trim(), name: name.trim() }); setImage(''); setName(''); setSandbox(''); await b.load() } catch (e) { b.setErr((e as Error).message) } }
  return (
    <div className="space-y-4">
      <Panel title="构建" actions={<div className="flex flex-wrap gap-2"><Select value={kind} onChange={e => setKind(e.target.value as 'oci' | 'commit')}><option value="oci">OCI 镜像 → 层</option><option value="commit">沙箱 → 模板</option></Select>{kind === 'oci' ? <Input className="w-56" value={image} onChange={e => setImage(e.target.value)} placeholder="python:3.12-slim" /> : <Input className="w-40" value={sandbox} onChange={e => setSandbox(e.target.value)} placeholder="沙箱 id" />}<Input className="w-32" value={name} onChange={e => setName(e.target.value)} placeholder={kind === 'oci' ? '层名(可选)' : '模板名'} /><Button size="sm" kind="primary" disabled={kind === 'oci' ? !image.trim() : !(sandbox.trim() && name.trim())} onClick={start}>开始</Button></div>}>
        <p className="mb-2 text-ui-sm text-fg-subtle">OCI 镜像的根文件系统叠在我们的基础之上成为一层;运行中的沙箱此刻的内存与磁盘成为一个有名字的模板,新沙箱从它热启动。节点连不上仓库时,用 `docker save` 的 tar 经 POST /builds/archive 上传。</p>
        {b.err && <ErrorLine>{b.err}</ErrorLine>}
        <Table cols={[{ h: 'id', cell: (x: Build) => <span className="font-mono">{x.id}</span> }, { h: '种类', cell: x => x.kind }, { h: '输入', cell: x => String(x.input.image ?? x.input.sandbox ?? x.input.name ?? '') }, { h: '节点', cell: x => x.node }, { h: '状态', cell: x => <Badge tone={x.status === 'done' ? 'success' : x.status === 'failed' ? 'destructive' : 'waiting'}>{x.status}</Badge> }, { h: '结果', cell: x => x.error || String(x.result?.id ?? x.result?.key ?? '') }, { h: '', cell: x => age(x.created) }]} rows={b.d?.builds} keyOf={x => x.id} empty="还没有构建" />
      </Panel>
      {t.d && t.d.templates.length > 0 && (() => { const nodes = [...new Set(t.d.templates.map(x => x.node))]; const keys = [...new Set(t.d.templates.map(x => x.key))]; const has = new Set(t.d.templates.map(x => `${x.node}:${x.key}`)); return nodes.length > 1 ? <Panel title="模板在哪些节点上(分发矩阵)"><table className="text-ui-sm"><thead><tr><th className="pr-3 text-left text-fg-subtle">模板</th>{nodes.map(n => <th key={n} className="px-2 text-fg-subtle">{n}</th>)}</tr></thead><tbody>{keys.map(k => <tr key={k}><td className="pr-3 font-mono">{k}</td>{nodes.map(n => <td key={n} className="px-2 text-center">{has.has(`${n}:${k}`) ? <span className="text-success">●</span> : <span className="text-fg-subtlest">○</span>}</td>)}</tr>)}</tbody></table><p className="mt-1 text-ui-sm text-fg-subtle">空心的用 `POST /templates/everywhere` 补齐;层用 `POST /layers/&lt;id&gt;/everywhere`。</p></Panel> : null })()}
      <Panel title="模板"><Table cols={[{ h: '名字 / 键', cell: (x: Template) => <span className="font-mono">{x.key}</span> }, { h: '节点', cell: x => x.node }, { h: '配置', cell: x => `${x.template ?? '-'} ${x.vcpu ?? '-'}c ${x.mem ?? '-'}MiB` }, { h: '来源', cell: x => x.from || '构建' }, { h: '内存 / 盘 MiB', cell: x => `${x.mem_mib ?? '-'} / ${x.upper_mib ?? '-'}` }, { h: '', cell: x => age(x.created) }]} rows={t.d?.templates} keyOf={x => `${x.node}:${x.key}`} empty="还没有模板快照" />{t.err && <ErrorLine>{t.err}</ErrorLine>}</Panel>
      <Panel title="层"><Table cols={[{ h: 'id', cell: (x: Layer) => <span className="font-mono">{x.id}</span> }, { h: '名字', cell: x => x.name }, { h: '节点', cell: x => x.node }, { h: 'MiB', cell: x => x.bytes ? (x.bytes / 1048576).toFixed(0) : '-' }, { h: '来源', cell: x => x.from || '-' }]} rows={l.d?.layers} keyOf={x => `${x.node}:${x.id}`} empty="还没有层" />{l.err && <ErrorLine>{l.err}</ErrorLine>}</Panel>
      {!who.admin && <p className="text-ui-sm text-fg-subtle">模板与层是节点上的共享制品,成员可以用,构建归自己的项目。</p>}
    </div>
  )
}

type Health = { ok: boolean; nodes: Record<string, { alive: boolean; running?: number; total?: number; mem_free_mib?: number; last_err?: string | null }> }
type SbRow = { id: string; status: string; project?: string; node?: string; class?: string; priority?: string; created?: number }

function OverviewTab({ who }: { who: Me }) {
  const p = project() || (who.projects || ['default'])[0] || 'default'
  const h = useLoad(() => GET<Health>('/health'), []); const sbs = useLoad(() => GET<{ sandboxes: SbRow[] }>('/sandboxes'), [p])
  const today = useLoad(() => GET<{ spent_usd: number }>(`/metering/projects/${p}/today`), [p]); const reqs = useLoad(() => GET<{ requests: NetRequest[] }>('/network/requests'), [])
  const au = useLoad(() => GET<{ events: AuditEvent[] }>(`/audit?n=12&project=${encodeURIComponent(p)}`), [p])
  const mine = (sbs.d?.sandboxes || []).filter(x => !x.project || x.project === p); const by = (k: keyof SbRow) => { const m: Record<string, number> = {}; mine.forEach(x => { const v = String(x[k] ?? '-'); m[v] = (m[v] || 0) + 1 }); return m }
  const nodes = Object.entries(h.d?.nodes || {}); const alive = nodes.filter(([, n]) => n.alive).length
  const tile = (label: string, value: React.ReactNode, sub?: string) => <div className="rounded-lg border border-border bg-panel px-3 py-2"><div className="text-ui-caption text-fg-subtle">{label}</div><div className="text-ui-xl font-semibold">{value}</div>{sub && <div className="text-ui-sm text-fg-subtlest">{sub}</div>}</div>
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {tile('节点', `${alive} / ${nodes.length}`, nodes.map(([n, x]) => `${n}${x.alive ? '' : ' ✗'}`).join(' · '))}
        {tile(`项目 ${p} 的沙箱`, mine.length, Object.entries(by('status')).map(([k, v]) => `${k} ${v}`).join(' · ') || '无')}
        {tile('今天花费', usd(today.d?.spent_usd), Object.entries(by('priority')).map(([k, v]) => `${k} ${v}`).join(' · '))}
        {tile('等你批的放宽', reqs.d?.requests.length ?? '-', who.role === 'owner' || who.admin ? '项目与成员页里批' : '只有 owner 能批')}
      </div>
      {(h.err || sbs.err || today.err) && <ErrorLine>{h.err || sbs.err || today.err}</ErrorLine>}
      <Panel title="沙箱" actions={<a className="text-ui-sm text-fg-subtle hover:text-fg" href="#/sandboxes">全部 →</a>}>
        <Table cols={[{ h: 'id', cell: (x: SbRow) => <a className="font-mono" href={`#/sandboxes/${x.id}`}>{x.id}</a> }, { h: '状态', cell: x => <Badge tone={x.status === 'running' ? 'success' : x.status === 'paused' ? 'neutral' : 'destructive'}>{x.status}</Badge> }, { h: '类别', cell: x => `${x.class || '-'} · ${x.priority || '-'}` }, { h: '节点', cell: x => x.node || '-' }, { h: '', cell: x => age(x.created) }]} rows={mine.slice(0, 8)} keyOf={x => x.id} empty="这个项目现在没有沙箱" />
      </Panel>
      <Panel title="最近的事" actions={<a className="text-ui-sm text-fg-subtle hover:text-fg" href="#/cloud/audit">审计 →</a>}>
        {au.err && <ErrorLine>{au.err}</ErrorLine>}
        <ul className="text-ui-sm">{(au.d?.events || []).map((e, i) => <li key={i} className="flex gap-2 py-0.5"><span className="w-28 shrink-0 text-fg-subtlest">{when(e.ts)}</span><span className="font-medium">{e.op}</span><span className="font-mono text-fg-subtle">{e.sandbox || ''}</span><span className="text-fg-subtle">{e.by ? `by ${e.by}` : ''}</span></li>)}</ul>
        {au.d && au.d.events.length === 0 && <p className="text-ui-sm text-fg-subtlest">还没有记录</p>}
      </Panel>
    </div>
  )
}

function AuditTab({ who }: { who: Me }) {
  const [p, setP] = useState(project()); const [n, setN] = useState('200'); const [op, setOp] = useState('')
  const au = useLoad(() => GET<{ events: AuditEvent[] }>(`/audit?n=${Number(n) || 200}${p ? `&project=${encodeURIComponent(p)}` : ''}`), [p, n])
  const rows = (au.d?.events || []).filter(e => !op || String(e.op).includes(op))
  return (
    <Panel title="审计" actions={<div className="flex gap-2"><Select value={p} onChange={e => setP(e.target.value)}><option value="">{who.admin ? '所有项目' : '本账户'}</option>{(who.projects || []).map(x => <option key={x} value={x}>{x}</option>)}</Select><Input className="w-32" value={op} onChange={e => setOp(e.target.value)} placeholder="op 包含…" /><Select value={n} onChange={e => setN(e.target.value)}>{['50', '200', '1000'].map(x => <option key={x} value={x}>{x} 条</option>)}</Select>{who.admin && <Button size="sm" onClick={async () => { try { const v = await GET<{ ok: boolean; lines: number; first_bad: number }>('/audit/verify'); window.alert(v.ok ? `哈希链完整:${v.lines} 行` : `第 ${v.first_bad} 行起链断了`) } catch (e) { au.setErr((e as Error).message) } }}>核对哈希链</Button>}</div>}>
      <p className="mb-2 text-ui-sm text-fg-subtle">控制面做过的每件事一行,只追加,成哈希链;成员只见本账户项目的。节点自己的审计在节点上。</p>
      {au.err && <ErrorLine>{au.err}</ErrorLine>}
      <Table cols={[{ h: '时间', cell: (e: AuditEvent) => when(e.ts) }, { h: 'op', cell: e => <span className="font-medium">{e.op}</span> }, { h: '项目', cell: e => String(e.project || e.for_project || '-') }, { h: '沙箱', cell: e => <span className="font-mono">{e.sandbox || ''}</span> }, { h: '谁', cell: e => String(e.by || '') }, { h: '详情', cell: e => <code className="text-ui-sm text-fg-subtle">{JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !['op', 'ts', 'project', 'sandbox', 'by', 'replica', 'h', 'prev'].includes(k)))).slice(0, 140)}</code> }]} rows={rows} keyOf={(e, i) => `${e.h || i}`} empty="没有记录" />
    </Panel>
  )
}

function ProjectSwitcher({ who }: { who: Me }) {
  const [p, setP] = useState(project()); const list = who.projects || []
  useEffect(() => { const f = () => setP(project()); window.addEventListener('cp-project', f); return () => window.removeEventListener('cp-project', f) }, [])
  if (list.length <= 1 && !p) return null
  return <label className="flex items-center gap-1 text-ui-sm text-fg-subtle">项目<Select value={p} onChange={e => { setProject(e.target.value); window.location.reload() }}><option value="">默认({list[0] || 'default'})</option>{list.filter(x => x !== list[0] || p === x).map(x => <option key={x} value={x}>{x}</option>)}</Select></label>
}

function CloudPage({ params }: { params: string[] }) {
  const tab = (TABS.find(t => t[0] === params[0])?.[0] ?? 'overview') as Tab
  const who = useLoad(() => me() as Promise<Me>, [])
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex flex-wrap items-center gap-3"><h1 className="text-ui-xl font-semibold">控制台</h1><nav className="flex gap-1">{TABS.map(([id, label]) => <a key={id} href={`#/cloud/${id}`} className={`rounded px-2 py-0.5 text-ui-base ${tab === id ? 'bg-accent text-fg' : 'text-fg-subtle hover:text-fg'}`}>{label}</a>)}</nav><div className="flex-1" />{who.d && <ProjectSwitcher who={who.d} />}{who.d && <span className="text-ui-sm text-fg-subtlest">{who.d.user || 'admin'} · {who.d.account || 'default'} · {who.d.role || (who.d.admin ? 'admin' : '-')}</span>}</div>
      {who.err && <ErrorLine>{who.err}</ErrorLine>}
      {who.d && (tab === 'overview' ? <OverviewTab who={who.d} /> : tab === 'projects' ? <ProjectsTab who={who.d} /> : tab === 'billing' ? <BillingTab who={who.d} /> : tab === 'volumes' ? <VolumesTab who={who.d} /> : tab === 'audit' ? <AuditTab who={who.d} /> : <ImagesTab who={who.d} />)}
    </div>
  )
}

export const cloud: Plugin = {
  id: 'cloud',
  activate(ctx) {
    ctx.nav({ id: 'cloud', label: '控制台', href: '#/cloud', order: 15 })
    ctx.route({ id: 'cloud', pattern: /^#\/cloud(?:\/([a-z]+))?$/, component: CloudPage, nav: 'cloud' })
  },
}
void api
