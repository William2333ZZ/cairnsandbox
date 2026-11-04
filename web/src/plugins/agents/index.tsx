// 多个 agent (docs/001 第 10 节): a user has main and may add named agents with a role (修 bug 的、看告警的). The switcher in
// the sidebar picks which one the workbench talks to (X-Agent); "@name …" in the conversation addresses one directly.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { DEL, GET, POST, agent, setAgent } from '../../api/client'
import { Button, ErrorLine, Input, TextArea } from '../../ui'

interface AgentRow { name: string; role?: string; created?: number | null }
const load = () => GET<{ agents: AgentRow[]; current: string }>('/intake/agents')

function Switcher() {
  const [rows, setRows] = useState<AgentRow[]>([])
  useEffect(() => { load().then(r => setRows(r.agents)).catch(() => setRows([])) }, [])
  if (rows.length < 2) return null
  return (
    <div className="px-2 pb-2">
      <select className="h-7 w-full rounded-md border border-border bg-transparent px-1 text-ui-sm text-fg" value={agent()} title="现在跟哪个 agent 说话" onChange={e => { setAgent(e.target.value); location.reload() }}>
        {rows.map(a => <option key={a.name} value={a.name}>{a.name === 'main' ? '主 agent' : a.name}</option>)}
      </select>
    </div>
  )
}

function AgentsPage() {
  const [rows, setRows] = useState<AgentRow[]>([]); const [err, setErr] = useState(''); const [name, setName] = useState(''); const [role, setRole] = useState('')
  const refresh = () => load().then(r => setRows(r.agents)).catch(e => setErr((e as Error).message))
  useEffect(() => { void refresh() }, [])
  const add = async () => { try { await POST('/intake/agents', { name: name.trim(), role: role.trim() }); setName(''); setRole(''); setErr(''); await refresh() } catch (e) { setErr((e as Error).message) } }
  const setRoleOf = async (n: string, r: string) => { try { await POST('/intake/agents', { name: n, role: r }); await refresh() } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-6 py-6">
      <div><h1 className="text-ui-xl font-medium text-fg">agent</h1><p className="mt-1 text-ui-base text-fg-subtle">你的主 agent 之外,可以按岗位再开几个:各自有自己的事、例行和记忆,共用机器与仓库。对话里写 "@名字 …" 直接交给它。</p></div>
      {err && <ErrorLine>{err}</ErrorLine>}
      <div className="flex flex-col gap-2">
        {rows.map(a => <AgentCard key={a.name} a={a} onRole={r => setRoleOf(a.name, r)} onDelete={async () => { if (window.confirm(`删除 agent ${a.name}?它的事和记忆会留在库里但不再可见。`)) { await DEL(`/intake/agents/${a.name}`); if (agent() === a.name) setAgent('main'); await refresh() } }} />)}
      </div>
      <div className="rounded-xl border border-border bg-panel px-4 py-3">
        <div className="text-ui-base font-medium text-fg">再开一个</div>
        <div className="mt-2 flex flex-col gap-2"><Input className="w-56" value={name} onChange={e => setName(e.target.value)} placeholder="名字(小写,如 fix)" /><TextArea rows={2} value={role} onChange={e => setRole(e.target.value)} placeholder="岗位:它只做什么、怎么做(会作为信息给它)" /><div><Button kind="primary" disabled={!name.trim()} onClick={add}>新建</Button></div></div>
      </div>
    </div>
  )
}

function AgentCard({ a, onRole, onDelete }: { a: AgentRow; onRole: (r: string) => void; onDelete: () => void }) {
  const [edit, setEdit] = useState(false); const [r, setR] = useState(a.role || '')
  return (
    <div className="flex items-start gap-3 rounded-xl border border-border bg-panel px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-ui-base text-fg"><span className="font-medium">{a.name === 'main' ? '主 agent' : a.name}</span>{agent() === a.name && <span className="text-ui-sm text-fg-subtle">正在跟它说话</span>}</div>
        {edit ? <div className="mt-1 flex flex-col gap-1"><TextArea rows={2} value={r} onChange={e => setR(e.target.value)} /><div className="flex gap-2"><Button size="sm" kind="primary" onClick={() => { onRole(r.trim()); setEdit(false) }}>存</Button><Button size="sm" onClick={() => setEdit(false)}>算了</Button></div></div>
          : <div className="mt-0.5 text-ui-sm text-fg-subtle">{a.role || '没有写岗位'} <button className="text-fg-subtlest hover:text-fg" onClick={() => setEdit(true)}>改</button></div>}
      </div>
      {a.name !== 'main' && <button onClick={onDelete} className="shrink-0 text-ui-sm text-fg-subtle hover:text-destructive">删除</button>}
    </div>
  )
}

export const agents: Plugin = {
  id: 'agents',
  activate(ctx) {
    ctx.slot({ slot: 'sidebar-head', id: 'agent-switcher', component: Switcher as React.ComponentType<Record<string, unknown>>, order: 1 })
    ctx.nav({ id: 'agents', label: 'agent', href: '#/agents', order: 3 })
    ctx.route({ id: 'agents', pattern: /^#\/agents$/, component: AgentsPage, nav: 'agents' })
  },
}
