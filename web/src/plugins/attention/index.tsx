// 待处理: the first screen (docs/008 question 1 and 2).
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { useStore } from '../../store'
import { Button, ErrorLine, Panel, Table, TextArea, Input, age } from '../../ui'
import { StageBadge, ReportBadge, Short } from '../../ui/domain'
import type { TicketRow } from '../../api/types'
import { navigate } from '../../layout/Shell'

export const TICKET_COLS = [
  { h: '阶段', cell: (t: TicketRow) => <StageBadge t={t} /> },
  { h: '工单', cell: (t: TicketRow) => <a href={`#/tickets/${t.id}`}><Short id={t.id} /></a> },
  { h: '标题', cell: (t: TicketRow) => <span className="line-clamp-2">{t.title || '-'}</span>, wrap: true, w: '40%' },
  { h: '来源', cell: (t: TicketRow) => t.source },
  { h: '请求者', cell: (t: TicketRow) => t.actor || '-' },
  { h: '最近报告', cell: (t: TicketRow) => <ReportBadge s={t.last_report?.state} /> },
  { h: 'PR', cell: (t: TicketRow) => t.pr?.number ? <a href={t.pr.url || '#'} target="_blank" rel="noreferrer">#{t.pr.number}</a> : '-' },
  { h: '更新', cell: (t: TicketRow) => age(t.updated) },
]

function AttentionPage() {
  const a = useStore(s => s.attention), load = useStore(s => s.load), act = useStore(s => s.act), live = useStore(s => s.live)
  const [text, setText] = useState(''), [budget, setBudget] = useState('1.0'), [busy, setBusy] = useState(false), [err, setErr] = useState<string | null>(null)
  useEffect(() => { void load.attention(); const id = setInterval(() => void load.attention(), live ? 30000 : 5000); return () => clearInterval(id) }, [live])
  const d = a.data
  const submit = async () => {
    if (!text.trim()) return; setBusy(true); setErr(null)
    try { await act.request(text.trim(), Number(budget) || 1); setText('') } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <h1 className="text-ui-xl font-semibold">待处理</h1>
      {a.error && <ErrorLine>接单层不可达:{a.error}</ErrorLine>}
      <Panel title="需要你">
        {!d && !a.error && <p className="py-3 text-ui-base text-fg-subtlest">加载中</p>}
        {d && (d.needs_person.length ? <Table cols={TICKET_COLS} rows={d.needs_person} keyOf={t => t.id} onRow={t => navigate(`#/tickets/${t.id}`)} /> : <p className="text-ui-base text-fg-subtle">现在没有需要你的事。</p>)}
      </Panel>
      <Panel title="系统问题">
        {d && (() => {
          const items: React.ReactNode[] = []
          d.problems.forEach(p => items.push(<li key={'p' + p.name}><b>{p.name}</b> {p.error} <span className="text-ui-xs text-fg-subtle">{age(p.since)} 前</span></li>))
          d.unavailable.forEach(x => items.push(<li key={x.id}><b>Eval 未完成</b> <a href={`#/threads/${encodeURIComponent(x.thread)}`}>{x.thread}</a> {x.reason}</li>))
          if (d.paused) items.push(<li key="paused"><b className="text-warning">处理已暂停</b> <Button size="sm" onClick={() => act.pause(false)}>继续</Button></li>)
          return items.length ? <ul className="list-disc space-y-1 pl-5 text-ui-base">{items}</ul> : <p className="text-ui-base text-fg-subtle">系统健康。</p>
        })()}
      </Panel>
      <Panel title="新请求">
        <TextArea rows={3} value={text} onChange={e => setText(e.target.value)} placeholder="直接向 agent 提一个请求。它会在一台沙箱里做,结果回到这里。" />
        <div className="mt-2 flex items-center gap-3">
          <label className="flex items-center gap-1 text-ui-sm text-fg-subtle">预算 USD <Input className="w-16" value={budget} onChange={e => setBudget(e.target.value)} /></label>
          <Button kind="primary" disabled={busy || !text.trim()} onClick={submit}>提交</Button>
          {err && <span className="text-ui-sm text-destructive">{err}</span>}
        </div>
      </Panel>
      {d && <p className="text-ui-xs text-fg-subtle">24 小时:Trigger {d.counts.triggers_24h} · Decision {d.counts.decisions_24h}(本会接单 {d.counts.would_accept_24h})· 开着的工单 {d.counts.tickets_open} · 执行中 {d.counts.running}/{d.counts.slots} · 策略 {d.policy_version} · 模式 {d.mode}</p>}
    </div>
  )
}

export const attention: Plugin = {
  id: 'attention',
  activate(ctx) {
    ctx.nav({ id: 'attention', label: '待处理', href: '#/attention', order: 0, badge: s => (s as { attention: { data: { needs_person: unknown[] } | null } }).attention.data?.needs_person.length ?? null })
    ctx.route({ id: 'attention', pattern: /^#\/attention$/, component: AttentionPage, nav: 'attention' })
  },
}
