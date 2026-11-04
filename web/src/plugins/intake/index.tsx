// 接单: Sources, tickets, threads, decisions, policy; ticket and thread detail pages (docs/008 question 3).
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { useStore } from '../../store'
import { Button, Confirm, ErrorLine, Input, KV, Panel, Select, Table, TextArea, age, num, when } from '../../ui'
import { DecisionBadge, ReportBadge, Short, StageBadge } from '../../ui/domain'
import type { Decision, Thread, TicketRow, Trigger } from '../../api/types'
import { navigate } from '../../layout/Shell'
import { TICKET_COLS } from '../attention'

const DECISION_COLS = [
  { h: '时间', cell: (x: Decision) => when(x.created) },
  { h: 'Thread', cell: (x: Decision) => <a href={`#/threads/${encodeURIComponent(x.thread)}`}>{x.thread}</a> },
  { h: '结果', cell: (x: Decision) => <DecisionBadge s={x.outcome} /> },
  { h: '强度', cell: (x: Decision) => x.strength || '-' }, { h: '依据', cell: (x: Decision) => x.basis || '-' },
  { h: '理由', cell: (x: Decision) => <span>{x.would_accept ? '[本会接单] ' : ''}{x.reason || ''}</span>, wrap: true, w: '30%' },
  { h: '规则', cell: (x: Decision) => x.matched_rules?.join(' ') || '-' },
  { h: '工单', cell: (x: Decision) => x.ticket ? <a href={`#/tickets/${x.ticket}`}><Short id={x.ticket} /></a> : '-' },
]

function IntakePage() {
  const s = useStore(); const live = s.live
  useEffect(() => { const f = () => { void s.load.attention(); void s.load.tickets(); void s.load.threads(); void s.load.decisions() }; f(); const id = setInterval(f, live ? 30000 : 8000); return () => clearInterval(id) }, [live])
  const src = s.attention.data?.sources ?? {}
  const [showEnded, setShowEnded] = useState(false); const [mark, setMark] = useState(5)
  const all = s.tickets.data ? s.tickets.data.filter(t => showEnded || t.state === 'open') : null
  // 水位线 (DSH ui-workspace): the first N rows plus every row that is running or waits for a person; "展开其余" raises N by 5
  const tickets = all ? all.filter((t, i) => i < mark || t.running || t.stage.startsWith('person/')) : null
  const hidden = all ? all.length - (tickets?.length ?? 0) : 0
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-ui-xl font-semibold">接单</h1>
      {s.tickets.error && <ErrorLine>{s.tickets.error}</ErrorLine>}
      <Panel title="Source">
        <Table cols={[{ h: 'Source', cell: ([k]: [string, typeof src[string]]) => k }, { h: '状态', cell: ([, v]: [string, typeof src[string]]) => v.enabled ? <span className="text-success">开</span> : <span className="text-fg-subtle">关</span> },
          { h: '详情', cell: ([k, v]: [string, typeof src[string]]) => k === 'gitea' ? `${v.repo || '-'} bot=${v.bot || '-'}${v.cursor ? ' 轮询 ' + age(v.cursor.polled) + ' 前' : ''}${v.error ? ' 错误: ' + v.error : ''}` : '工作台直接请求', wrap: true }]}
          rows={Object.entries(src)} keyOf={([k]) => k} />
      </Panel>
      <Panel title="工单" actions={<label className="flex items-center gap-1 text-ui-sm text-fg-subtle"><input type="checkbox" checked={showEnded} onChange={e => setShowEnded(e.target.checked)} />含已结束</label>}>
        <Table cols={TICKET_COLS} rows={tickets} keyOf={t => t.id} onRow={t => navigate(`#/tickets/${t.id}`)} />
        {hidden > 0 && <Button kind="ghost" size="sm" onClick={() => setMark(m => m + 5)}>展开其余 {hidden} 条</Button>}
        {hidden === 0 && mark > 5 && <Button kind="ghost" size="sm" onClick={() => setMark(5)}>收起</Button>}
      </Panel>
      <Panel title="Thread">
        <Table cols={[{ h: 'Thread', cell: (x: Thread) => <a href={`#/threads/${encodeURIComponent(x.id)}`}>{x.id}</a> }, { h: 'Source', cell: (x: Thread) => x.source }, { h: '标题', cell: (x: Thread) => x.title || '-', wrap: true, w: '35%' },
          { h: '订阅', cell: (x: Thread) => x.subscriptions?.length ?? 0 }, { h: '待评估', cell: (x: Thread) => x.eval?.triggers?.length ?? 0 }, { h: '更新', cell: (x: Thread) => age(x.updated) }]}
          rows={s.threads.data ? s.threads.data.slice(0, 50) : null} keyOf={x => x.id} onRow={x => navigate(`#/threads/${encodeURIComponent(x.id)}`)} />
      </Panel>
      <Panel title="Decision"><Table cols={DECISION_COLS} rows={s.decisions.data ? s.decisions.data.slice(0, 50) : null} keyOf={x => x.id} /></Panel>
      <PolicyPanel />
    </div>
  )
}

function PolicyPanel() {
  const p = useStore(s => s.policy), load = useStore(s => s.load), act = useStore(s => s.act)
  const [mode, setMode] = useState('observe'), [rules, setRules] = useState(''), [allowed, setAllowed] = useState(''), [bots, setBots] = useState(''), [dirty, setDirty] = useState(false), [msg, setMsg] = useState('')
  useEffect(() => { void load.policy() }, [])
  useEffect(() => { if (!p.data || dirty) return; setMode(p.data.mode); setRules(p.data.rules.map(r => `${r.enabled === false ? '#' : ''}${r.name}: ${r.text}`).join('\n')); setAllowed(p.data.sources.gitea?.allowed?.join(', ') ?? ''); setBots(p.data.sources.gitea?.bots?.join(', ') ?? '') }, [p.data])
  const save = async () => {
    const lines = rules.split('\n').map(l => l.trim()).filter(Boolean)
    const badLine = lines.find(l => l.replace(/^#/, '').indexOf(':') < 0)
    if (badLine) { setMsg(`规则要写成 "名字: 正文":${badLine}`); return }
    const rs = lines.map(l => { const en = !l.startsWith('#'); l = l.replace(/^#/, ''); const i = l.indexOf(':'); return { name: l.slice(0, i).trim(), text: l.slice(i + 1).trim(), enabled: en } })
    const split = (v: string) => v.split(/[\n,，\s]+/).map(x => x.trim()).filter(Boolean)
    try { const r = await act.savePolicy({ mode: mode as 'off' | 'observe' | 'auto', rules: rs, sources: { gitea: { allowed: split(allowed), bots: split(bots) } } }); setMsg(r.changed ? `已保存为版本 ${r.version}` : '没有变化'); setDirty(false) } catch (e) { setMsg((e as Error).message) }
  }
  const mark = <T,>(set: (v: T) => void) => (v: T) => { set(v); setDirty(true) }
  return (
    <Panel title={<>策略 <span className="font-mono text-ui-xs">{p.data?.version}</span></>} actions={dirty && <Button kind="primary" size="sm" onClick={save}>保存为新版本</Button>}>
      {p.error && <ErrorLine>{p.error}</ErrorLine>}
      <div className="flex flex-wrap gap-3">
        <label className="flex flex-col text-ui-sm text-fg-subtle">接受模式<Select value={mode} onChange={e => mark(setMode)(e.target.value)}>{['off', 'observe', 'auto'].map(m => <option key={m} value={m}>{m}</option>)}</Select></label>
        <label className="flex flex-col text-ui-sm text-fg-subtle">gitea 白名单<Input className="w-72" value={allowed} onChange={e => mark(setAllowed)(e.target.value)} /></label>
        <label className="flex flex-col text-ui-sm text-fg-subtle">gitea bot 名单<Input className="w-56" value={bots} onChange={e => mark(setBots)(e.target.value)} /></label>
      </div>
      <label className="mt-2 block text-ui-sm text-fg-subtle">规则(每行 名字: 正文;行首 # 表示停用)</label>
      <TextArea rows={5} value={rules} onChange={e => mark(setRules)(e.target.value)} />
      {msg && <p className="mt-1 text-ui-sm text-fg-subtle">{msg}</p>}
    </Panel>
  )
}

function TicketPage({ params: [id] }: { params: string[] }) {
  const t = useStore(s => s.ticket[id]), load = useStore(s => s.load), act = useStore(s => s.act), live = useStore(s => s.live)
  const [ans, setAns] = useState('')
  useEffect(() => { void load.ticket(id); const i = setInterval(() => void load.ticket(id), live ? 20000 : 5000); return () => clearInterval(i) }, [id, live])
  const d = t?.data
  if (!d) return t?.error ? <ErrorLine>{t.error}</ErrorLine> : <p className="text-ui-base text-fg-subtle">加载中</p>
  const m = d.mandate; const running = d.assignments.find(a => a.state === 'running')
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-center gap-2">
        <h1 className="text-ui-xl font-semibold">工单 <Short id={d.id} /></h1><StageBadge t={d} />
        <div className="flex-1" />
        {d.state === 'open' && <Confirm text="停止这个工单?人停的工单不再被唤醒。" onYes={() => act.stopTicket(id)}>停止</Confirm>}
        {d.state === 'open' && !d.running && <Button size="sm" onClick={() => act.retryTicket(id)}>重试</Button>}
        {running?.sandbox && <a className="text-ui-sm" href={`#/tickets/${id}?rail=events&sandbox=${running.sandbox}`}>事件流</a>}
        {running?.sandbox && <a className="text-ui-sm" href={`#/tickets/${id}?rail=terminal&sandbox=${running.sandbox}`}>终端</a>}
        {running?.sandbox && <a className="text-ui-sm" href={`#/tickets/${id}?rail=diff&sandbox=${running.sandbox}&base=main`}>diff</a>}
      </header>
      <p className="text-ui-base">{d.title}</p>
      <Panel title="授权与状态">
        <KV rows={[
          ['状态', `${d.state}${d.ended_by ? ' / ' + d.ended_by : ''}${d.outcome ? ' / ' + d.outcome : ''}`], ['Thread', <a href={`#/threads/${encodeURIComponent(d.thread)}`}>{d.thread}</a>], ['分支', d.branch],
          ['PR', d.pr?.number ? <a href={d.pr.url || '#'} target="_blank" rel="noreferrer">#{d.pr.number} {d.pr.state || ''}</a> : '-'],
          ['授权', `${m.strength} · ${m.basis} · 请求者 ${m.actor} · 预算 ${m.budget_usd} USD · 网络 ${m.network} · 策略 ${m.policy_version}${m.matched_rules?.length ? ' · 规则 ' + m.matched_rules.join(' ') : ''}`],
          ['工作区层', d.workspace_layer || '-'], ['最近报告', d.last_report ? <><ReportBadge s={d.last_report.state} /> {d.last_report.text || ''}</> : '-'], ['待回答', d.attention ? d.attention.question : '-'],
        ]} />
        <div className="mt-3 flex gap-2"><Input className="flex-1" value={ans} onChange={e => setAns(e.target.value)} placeholder={d.attention ? '回答:' + d.attention.question : '给执行者一条消息'} onKeyDown={e => { if (e.key === 'Enter' && ans.trim()) { void act.replyTicket(id, ans.trim()); setAns('') } }} /><Button onClick={() => { if (ans.trim()) { void act.replyTicket(id, ans.trim()); setAns('') } }}>发送</Button></div>
      </Panel>
      <Panel title="输入"><Table cols={[{ h: '时间', cell: i => when(i.ts) }, { h: '类型', cell: i => i.kind }, { h: '来自', cell: i => i.actor || '-' }, { h: '内容', cell: i => (i.text || '').slice(0, 400), wrap: true, w: '55%' }, { h: '已送达', cell: i => i.delivered ? '✓' : '-' }]} rows={d.inputs} /></Panel>
      <Panel title="派工"><Table cols={[{ h: 'ID', cell: a => <Short id={a.id} /> }, { h: '状态', cell: a => a.state }, { h: '沙箱', cell: a => a.sandbox ? <a href={`#/sandboxes/${a.sandbox}`}>{a.sandbox}</a> : '-' }, { h: '报告', cell: a => a.reports?.length ? a.reports.map((r, i) => <span key={i} className="mr-1"><ReportBadge s={r.state} /></span>) : '-' },
        { h: '退出码', cell: a => a.agent_exit ?? '-' }, { h: '费用', cell: a => num(a.cost, 4) }, { h: 'token', cell: a => `${a.tokens_in || 0}/${a.tokens_out || 0}` }, { h: '耗时', cell: a => a.elapsed_s ? num(a.elapsed_s, 1) + 's' : '-' }, { h: '错误', cell: a => a.error || '', wrap: true }, { h: '恢复', cell: a => a.recovery ? '是' : '-' },
        { h: '', cell: a => a.sandbox ? <a className="text-ui-sm" href={`#/tickets/${id}?rail=events&sandbox=${a.sandbox}`}>事件</a> : null }]} rows={d.assignments} keyOf={a => a.id} /></Panel>
      <Panel title="回帖"><Table cols={[{ h: '时间', cell: r => when(r.created) }, { h: '用途', cell: r => r.purpose }, { h: '状态', cell: r => r.state }, { h: '内容', cell: r => r.text, wrap: true, w: '60%' }]} rows={d.replies} /></Panel>
    </div>
  )
}

function ThreadPage({ params: [id] }: { params: string[] }) {
  const t = useStore(s => s.thread[id]), load = useStore(s => s.load), act = useStore(s => s.act)
  useEffect(() => { void load.thread(id); const i = setInterval(() => void load.thread(id), 8000); return () => clearInterval(i) }, [id])
  const d = t?.data
  if (!d) return t?.error ? <ErrorLine>{t.error}</ErrorLine> : <p className="text-ui-base text-fg-subtle">加载中</p>
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-ui-xl font-semibold">Thread <span className="font-mono text-ui-base">{d.id}</span></h1>
      <Panel><KV rows={[['Source', d.source], ['标题', d.title || '-'], ['订阅', (d.subscriptions || []).map(x => <a key={x} className="mr-2" href={`#/tickets/${x}`}><Short id={x} /></a>)], ['待评估', d.eval?.triggers?.length ?? 0]]} /></Panel>
      <Panel title="工单"><Table cols={TICKET_COLS} rows={(d.tickets ?? []) as TicketRow[]} keyOf={x => x.id} onRow={x => navigate(`#/tickets/${x.id}`)} /></Panel>
      <Panel title="Trigger"><Table cols={[{ h: '时间', cell: (x: Trigger) => when(x.created) }, { h: '来自', cell: (x: Trigger) => x.actor + (x.human ? '' : ' (bot)') }, { h: '点名', cell: (x: Trigger) => x.addressed ? '是' : '-' }, { h: '范围', cell: (x: Trigger) => x.scope || '-' },
        { h: '内容', cell: (x: Trigger) => (x.text || '').slice(0, 300), wrap: true, w: '45%' }, { h: '去向', cell: (x: Trigger) => x.disposition || '-' }, { h: '', cell: (x: Trigger) => <Button size="sm" onClick={() => act.reevaluate(x.id).then(() => load.thread(id))}>重新评估</Button> }]} rows={d.triggers ?? []} keyOf={x => x.id} /></Panel>
      <Panel title="Decision"><Table cols={DECISION_COLS} rows={d.decisions ?? []} keyOf={x => x.id} /></Panel>
    </div>
  )
}

export const intake: Plugin = {
  id: 'intake', requires: ['attention'],
  activate(ctx) {
    ctx.nav({ id: 'intake', label: '接单', href: '#/intake', order: 10 })
    ctx.route({ id: 'intake', pattern: /^#\/intake$/, component: IntakePage, nav: 'intake' })
    ctx.route({ id: 'ticket', pattern: /^#\/tickets\/([^/?]+)$/, component: TicketPage, nav: 'intake' })
    ctx.route({ id: 'thread', pattern: /^#\/threads\/([^?]+)$/, component: ThreadPage, nav: 'intake' })
  },
}
