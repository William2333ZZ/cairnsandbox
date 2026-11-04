// 工作台主界面 (docs/008), built on ZCode's conversation logic (see turns.ts): a job is a list of turns; each turn is the
// user's inputs, one work segment whose process rows fold under "已工作 N 秒", the final text outside the fold, and a
// tail (reports that need the person, replies, time). The sidebar slot is the task list (groups: 需要你 / 在做 / 今天 /
// 更早; attention badge, status dot, unread). The composer keeps a draft per key and turns into stop / steer while running.
import React, { useEffect, useMemo, useRef, useState } from 'react'
import type { Plugin } from '../../kernel'
import { useStore } from '../../store'
import { DEL, GET, POST } from '../../api/client'
import { ErrorLine, age, cx, usd, when } from '../../ui'
import type { ConvRow, Report, Review, TicketRow } from '../../api/types'
import { useHash } from '../../layout/Shell'
import { buildTurns, formatDuration, historyDefaultOpen, type ConvItem, type Turn, type WorkRow, type WorkSegment } from './turns'

interface Conversation { ticket: TicketRow; items: ConvItem[] }
interface Live { running: boolean; sandbox?: string | null; messages: { role: string; content: unknown; extra?: Record<string, unknown> }[]; exit_status?: string; calls?: number; cost?: number }

const STAGE_LABEL: Record<string, string> = { 'person/delivery': '输入未送达', 'person/approve': '等你同意', 'person/answer': '等你回答', 'person/review': '等你审阅', 'person/blocked': '受阻', 'person/failed': '失败', working: '在做', 'working/queued': '排队', idle: '等事件', ended: '已结束' }
const stageLabel = (s?: string) => (s && STAGE_LABEL[s]) || s || ''
const Chevron = ({ open }: { open: boolean }) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className={cx('size-4 shrink-0 text-fg-subtlest transition-transform', open && 'rotate-90')}><path d="m9 6 6 6-6 6" /></svg>
const Spinner = () => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" className="size-3.5 animate-spin text-fg-subtle"><path d="M21 12a9 9 0 1 1-6.2-8.6" /></svg>

// ---- per-viewer conveniences: what was seen, and drafts
const seenKey = (id: string) => `seen:${id}`
const seenAt = (id: string): number => { try { return Number(localStorage.getItem(seenKey(id)) || 0) } catch { return 0 } }
const markSeen = (id: string) => { try { localStorage.setItem(seenKey(id), String(Date.now() / 1000)) } catch { /* ignore */ } }
const draftGet = (k: string) => { try { return localStorage.getItem(`draft:${k}`) || '' } catch { return '' } }
const draftSet = (k: string, v: string) => { try { v ? localStorage.setItem(`draft:${k}`, v) : localStorage.removeItem(`draft:${k}`) } catch { /* ignore */ } }

// ---- sidebar slot: the task list (ZCode project-sidebar/grouped: rows h-7, attention badge or status dot on the right)
function Indicator({ t }: { t: TicketRow }) {
  if (t.running) return <Spinner />
  if (t.stage === 'person/failed' || t.stage === 'person/blocked') return <span className="size-1.5 rounded-full bg-destructive" />
  if (t.state === 'open' && t.updated > seenAt(t.id)) return <span className="size-1.5 rounded-full bg-link" title="有新进展" />
  return null
}
function TaskList({ collapsed }: { collapsed?: boolean }) {
  const tickets = useStore(s => s.tickets), load = useStore(s => s.load), live = useStore(s => s.live)
  const hash = useHash(); const selected = (hash.match(/^#\/w\/([^/?]+)/) || [])[1] || null
  const [more, setMore] = useState(false)
  useEffect(() => { void load.tickets(); const i = setInterval(() => void load.tickets(), live ? 30000 : 6000); return () => clearInterval(i) }, [live])
  const all = tickets.data ?? []
  const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0); const today = dayStart.getTime() / 1000
  const groups: [string, TicketRow[]][] = [
    ['需要你', all.filter(t => t.stage.startsWith('person/') && t.stage !== 'person/review')],
    ['在做', all.filter(t => t.state === 'open' && !t.stage.startsWith('person/'))],
    ['今天', all.filter(t => (t.stage === 'person/review' || t.state === 'ended') && t.updated >= today)],
    ['更早', all.filter(t => (t.stage === 'person/review' || t.state === 'ended') && t.updated < today)],
  ]
  const Row = ({ t }: { t: TicketRow }) => (
    <a href={`#/w/${t.id}`} title={t.title} className={cx('flex h-7 items-center gap-2 rounded-md px-2 text-ui-base hover:bg-hover', t.id === selected && 'bg-selected')}>
      <span className="min-w-0 flex-1 truncate text-fg">{t.title || t.short}</span>
      <span className="flex shrink-0 items-center gap-1.5 text-ui-sm text-fg-subtle">
        {t.stage.startsWith('person/') && t.stage !== 'person/review' && t.stage !== 'person/failed' && t.stage !== 'person/blocked' ? <span className="rounded-full bg-waiting-surface px-2 text-ui-xs font-medium leading-5 text-waiting">{stageLabel(t.stage)}</span> : null}
        <Indicator t={t} />
        {t.pr?.number ? <span className="text-fg-subtlest">#{t.pr.number}</span> : <span className="text-fg-subtlest">{age(t.updated)}</span>}
      </span>
    </a>
  )
  if (collapsed) return <div className="flex flex-col items-center gap-1 py-1">{all.slice(0, 8).map(t => <a key={t.id} href={`#/w/${t.id}`} title={t.title} className={cx('grid size-8 place-items-center rounded-md hover:bg-hover', t.id === selected && 'bg-selected')}><Indicator t={t} /><span className="sr-only">{t.title}</span></a>)}</div>
  return (
    <div className="flex flex-col gap-3 px-2">
      {tickets.error && <p className="px-2 text-ui-sm text-destructive">{tickets.error}</p>}
      {tickets.data && !all.length && <p className="px-2 py-2 text-ui-sm text-fg-subtlest">还没有任务。</p>}
      {groups.map(([name, rows]) => rows.length ? (
        <div key={name}>
          <h3 className="px-2 py-1 text-ui-sm font-medium text-fg-subtlest">{name}</h3>
          {(name === '更早' && !more ? rows.slice(0, 8) : rows).map(t => <Row key={t.id} t={t} />)}
          {name === '更早' && rows.length > 8 && !more && <button className="h-7 w-full px-2 text-left text-ui-sm text-fg-subtlest hover:text-fg" onClick={() => setMore(true)}>展开其余 {rows.length - 8} 条</button>}
        </div>) : null)}
    </div>
  )
}

// ---- composer (ZCode ConversationComposer: draft per session, send ↔ stop, steer while busy, error above the shell)
function Composer({ draftKey, placeholder, onSend, onStop, running, disabled, hint, autoFocus, actions, error }: {
  draftKey: string; placeholder: string; onSend: (t: string) => Promise<void>; onStop?: () => void; running?: boolean; disabled?: boolean; hint?: React.ReactNode; autoFocus?: boolean; actions?: React.ReactNode; error?: string | null
}) {
  const [text, setText] = useState(() => draftGet(draftKey)); const [busy, setBusy] = useState(false); const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { setText(draftGet(draftKey)) }, [draftKey])
  useEffect(() => { if (autoFocus) ref.current?.focus() }, [autoFocus])
  useEffect(() => { const el = ref.current; if (!el) return; el.style.height = '0px'; el.style.height = Math.min(220, el.scrollHeight) + 'px' }, [text])
  const canSend = !disabled && !busy && text.trim().length > 0
  const send = async () => { if (!canSend) return; setBusy(true); try { await onSend(text.trim()); setText(''); draftSet(draftKey, '') } finally { setBusy(false) } }
  return (
    <div className="mx-auto w-full max-w-4xl px-4 pb-4 pt-2">
      {error && <div role="alert" className="mb-2 rounded-lg border border-warning/30 bg-warning-surface px-3 py-2 text-ui-base text-fg">{error}</div>}
      <div className={cx('rounded-2xl border border-border bg-panel shadow-composer', disabled && 'opacity-60')}>
        <textarea ref={ref} rows={1} value={text} disabled={disabled} onChange={e => { setText(e.target.value); draftSet(draftKey, e.target.value) }} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send() } }} placeholder={placeholder}
          className="block min-h-[2.5rem] w-full resize-none bg-transparent px-4 pt-3 text-ui-base leading-6 text-fg placeholder:text-fg-subtlest" />
        <div className="flex items-center gap-1 px-2 pb-2 pt-1">
          <span className="flex items-center gap-1 px-2 text-ui-sm text-fg-subtle">{hint}</span>
          {actions}
          <div className="flex-1" />
          <span className="hidden px-2 text-ui-sm text-fg-subtlest sm:inline">{running ? '发送 = 插话,它下一步看到' : 'Enter 发送'}</span>
          {running && onStop && <button onClick={onStop} title="停止" className="grid size-8 place-items-center rounded-full border border-border text-fg hover:bg-hover"><svg viewBox="0 0 24 24" fill="currentColor" className="size-3"><rect x="5" y="5" width="14" height="14" rx="2" /></svg></button>}
          <button onClick={send} disabled={!canSend} title={running ? '插话' : '发送'} className="grid size-8 place-items-center rounded-full bg-brand text-fg-inverse hover:opacity-85 disabled:opacity-30"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="size-4"><path d="M12 19V5M5 12l7-7 7 7" /></svg></button>
        </div>
      </div>
    </div>
  )
}

// ---- approval card (docs/005 批准): the draft, 同意 / 否决; the server refuses a second decision
function ApprovalCard({ id, kind, draft, state, error, review }: { id: string; kind: string; draft: string; state?: string; error?: string | null; review?: Review | null }) {
  const act = useStore(s => s.act); const [busy, setBusy] = useState(false); const [done, setDone] = useState<string | null>(state && state !== 'pending' ? state : null)
  const label = ({ merge: '合并', send: '向外发消息', budget: '超出预算', routine: '例行' } as Record<string, string>)[kind] || kind
  const [asking, setAsking] = useState(false); const [why, setWhy] = useState(''); const [sure, setSure] = useState(false); const [fail, setFail] = useState('')
  const blocked = review?.verdict === 'block'
  const go = async (d: 'approve' | 'deny') => { setBusy(true); try { const r = await act.decide(id, d, d === 'deny' ? why.trim() : undefined, d === 'approve' && blocked); if (r.state === 'pending') setFail(r.error || '没有成功,稍后可以再试'); else { setDone(r.state); setFail('') } } catch (e) { setDone((e as Error).message) } finally { setBusy(false) } }
  return (
    <div className="max-w-2xl rounded-xl border border-waiting/40 bg-waiting-surface px-4 py-3 text-ui-base">
      <div className="mb-1 flex items-center gap-2 text-ui-sm"><span className="font-medium text-fg">要你同意 · {label}</span>{done && <span className="text-fg-subtle">{({ approved: '已同意', denied: '已否决', expired: '已失效', decided: '已处理' } as Record<string, string>)[done] || done}</span>}</div>
      <div className="whitespace-pre-wrap break-words text-fg">{draft}</div>
      {(fail || error) && <div className="mt-1 text-ui-sm text-destructive">{fail || error}</div>}
      {review && review.reasons.length > 0 && <ul className="mt-2 flex flex-col gap-1 text-ui-sm">{review.reasons.map((r, i) => <li key={i} className={cx('flex gap-1.5', r.level === 'block' ? 'text-destructive' : 'text-fg-subtle')}><span className="shrink-0 font-medium">{r.level === 'block' ? '拦下' : '注意'}</span><span className="min-w-0 break-words">{r.why}</span></li>)}</ul>}
      {!done && !asking && <div className="mt-2 flex gap-2"><button disabled={busy} onClick={() => blocked && !sure ? setSure(true) : go('approve')} className={cx('h-8 rounded-lg px-3 text-ui-base disabled:opacity-30', blocked ? 'border border-destructive text-destructive hover:bg-hover' : 'bg-brand text-fg-inverse')}>{blocked ? (sure ? '确认,仍然同意' : '审查拦下了,仍要同意…') : '同意'}</button><button disabled={busy} onClick={() => setAsking(true)} className="h-8 rounded-lg border border-border px-3 text-ui-base text-fg hover:bg-hover disabled:opacity-30">否决</button></div>}
      {!done && asking && <div className="mt-2 flex flex-col gap-2"><input autoFocus value={why} onChange={e => setWhy(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void go('deny') }} placeholder="为什么?它会记住,下次不再这样(可以不写)" className="h-8 rounded-lg border border-border bg-panel px-3 text-ui-base text-fg outline-none focus:border-border-strong" /><div className="flex gap-2"><button disabled={busy} onClick={() => go('deny')} className="h-8 rounded-lg bg-brand px-3 text-ui-base text-fg-inverse disabled:opacity-30">确认否决</button><button disabled={busy} onClick={() => setAsking(false)} className="h-8 rounded-lg border border-border px-3 text-ui-base text-fg hover:bg-hover">取消</button></div></div>}
    </div>
  )
}

// ---- rows shared by the home and the job
function Bubble({ text, meta }: { text: string; meta?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="max-w-xl rounded-xl rounded-tr-xs border border-border bg-bubble px-4 py-3 text-ui-base text-fg"><div className="whitespace-pre-wrap break-words">{text}</div></div>
      {meta && <div className="text-ui-sm text-fg-subtlest">{meta}</div>}
    </div>
  )
}
function Prose({ text }: { text: string }) { return <div className="whitespace-pre-wrap break-words text-ui-base leading-6 text-fg">{text}</div> }
function Tail({ at, children }: { at?: number; children?: React.ReactNode }) {
  return <div className="flex items-center gap-3 text-ui-sm text-fg-subtlest">{children}{at ? <span className="select-none">{when(at)}</span> : null}</div>
}

// ---- work rows (ZCode ToolSummaryRow + ExecuteCommand/ExecuteOutput: one summary line, expands to the output)
function CommandRow({ r }: { r: WorkRow }) {
  const [open, setOpen] = useState(false)
  const first = (r.command || '').split('\n')[0]
  return (
    <div className="flex flex-col gap-1">
      <button onClick={() => setOpen(o => !o)} className="group flex w-full items-start gap-2 text-left text-ui-base">
        <span className="mt-0.5 w-4 shrink-0 text-center font-mono text-code text-fg-subtlest">$</span>
        <span className={cx('min-w-0 flex-1 font-mono text-code leading-5 text-fg', !open && 'truncate')}>{open ? r.command : first}{!open && (r.command || '').includes('\n') ? ' …' : ''}</span>
        <span className="flex shrink-0 items-center gap-2 text-ui-sm text-fg-subtlest">{r.returncode != null && r.returncode !== 0 && <span className="text-destructive">退出码 {r.returncode}</span>}{r.returncode == null && r.text === '' ? <Spinner /> : null}<Chevron open={open} /></span>
      </button>
      {open && r.text && <pre className={cx('out ml-6 max-h-[20lh] overflow-auto rounded-lg px-3 py-2 text-fg-subtle', r.failed ? 'bg-destructive-surface' : 'bg-surface')}>{r.text}</pre>}
      {!open && r.text && <pre className={cx('out ml-6 max-h-[3lh] overflow-hidden rounded-lg px-3 py-1.5 text-fg-subtle', r.failed ? 'bg-destructive-surface' : 'bg-surface')}>{r.text.split('\n').slice(0, 3).join('\n')}</pre>}
    </div>
  )
}
function HistoryRows({ rows }: { rows: WorkRow[] }) {
  return <div className="flex flex-col gap-2 pl-1">
    {rows.map(r => r.kind === 'command' ? <CommandRow key={r.key} r={r} /> : r.kind === 'thought' ? <div key={r.key} className="whitespace-pre-wrap break-words text-ui-base leading-6 text-fg-subtle">{r.text}</div> : r.kind === 'output' ? <pre key={r.key} className={cx('out max-h-[6lh] overflow-hidden rounded-lg px-3 py-1.5 text-fg-subtle', r.failed ? 'bg-destructive-surface' : 'bg-surface')}>{r.text}</pre> : <div key={r.key} className="text-ui-sm text-fg-subtlest">harness 结束:{r.text}</div>)}
  </div>
}

function WorkSegmentView({ seg, isLast, ticket }: { seg: WorkSegment; isLast: boolean; ticket: TicketRow }) {
  const [open, setOpen] = useState(() => historyDefaultOpen(seg, isLast))
  useEffect(() => { if (seg.state === 'running') setOpen(true) }, [seg.state])
  const dur = formatDuration(seg.durationMs)
  const label = seg.state === 'running' ? `工作中${dur ? ' ' + dur : ''}` : seg.state === 'interrupted' ? '已停止' : dur ? `已工作 ${dur}` : '已工作'
  const needsPerson = seg.reports.filter(r => r.state === 'waiting' && r.attention || r.state === 'blocked' || r.state === 'failed')
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-2 border-b border-border pb-2">
          <button onClick={() => setOpen(o => !o)} className="inline-flex max-w-full items-center gap-2 text-left text-ui-base text-fg-subtle hover:text-fg">
            <span className={cx('truncate', seg.state === 'running' && 'font-medium text-fg')}>{label}</span><Chevron open={open} />
          </button>
          <span className="flex shrink-0 items-center gap-2 text-ui-sm text-fg-subtlest">{seg.recovery && <span>续做</span>}{seg.cost ? <span>{usd(seg.cost)}</span> : null}{seg.sandbox && <span>{seg.sandbox}</span>}{seg.error && seg.state !== 'interrupted' && <span className="text-destructive">{seg.error}</span>}</span>
        </div>
        {open && (seg.history.length ? <HistoryRows rows={seg.history} /> : <div className="text-ui-sm text-fg-subtlest">{seg.state === 'running' ? '准备机器…' : '没有过程记录'}</div>)}
        {open && seg.state === 'running' && <div className="text-ui-base text-fg-subtlest">…</div>}
      </div>
      {seg.finalText && <Prose text={seg.finalText} />}
      {needsPerson.map((r, i) => <ReportCard key={i} r={r} ticket={ticket} />)}
    </div>
  )
}

function ReportCard({ r, ticket }: { r: Report; ticket: TicketRow }) {
  const act = useStore(s => s.act); const [ans, setAns] = useState('')
  const tone = r.state === 'waiting' ? 'border-waiting/40 bg-waiting-surface' : 'border-destructive/40 bg-destructive-surface'
  const label = ({ waiting: '要你回答', blocked: '受阻', failed: '失败' } as Record<string, string>)[r.state] || r.state
  const answerable = r.state === 'waiting' && ticket.stage === 'person/answer'
  return <div className={cx('max-w-2xl rounded-xl border px-4 py-3', tone)}>
    <div className="text-ui-sm font-medium text-fg">{label}</div>
    {r.attention ? <div className="mt-1 text-ui-base text-fg">{r.attention.question}</div> : r.text ? <div className="mt-1 whitespace-pre-wrap break-words text-ui-base text-fg">{r.text}</div> : null}
    {answerable && <div className="mt-2 flex gap-2"><input className="h-8 flex-1 rounded-lg border border-border bg-input px-2.5 text-ui-base placeholder:text-fg-subtlest" value={ans} onChange={e => setAns(e.target.value)} placeholder="回答" onKeyDown={e => { if (e.key === 'Enter' && ans.trim()) { void act.replyTicket(ticket.id, ans.trim()); setAns('') } }} /><button disabled={!ans.trim()} onClick={() => { void act.replyTicket(ticket.id, ans.trim()); setAns('') }} className="h-8 rounded-lg bg-brand px-3 text-ui-base text-fg-inverse disabled:opacity-30">发送</button></div>}
  </div>
}

function TicketApprovals({ ticket }: { ticket: TicketRow }) {
  const approvals = useStore(s => s.approvals), load = useStore(s => s.load)
  useEffect(() => { void load.approvals(); const i = setInterval(() => void load.approvals(), 5000); return () => clearInterval(i) }, [ticket.id])
  const mine = (approvals.data ?? []).filter(a => a.ticket === ticket.id)
  return <>{mine.map(a => <ApprovalCard key={a.id} id={a.id} kind={a.kind} draft={a.draft} error={a.error} review={a.review} />)}</>
}

function TurnView({ turn, isLast, ticket }: { turn: Turn; isLast: boolean; ticket: TicketRow }) {
  const seg = turn.segment
  return (
    <div className="flex flex-col gap-4">
      {turn.inputs.map((it, i) => <Bubble key={i} text={it.text || ''} meta={<>{it.actor === 'system' ? '平台' : it.actor}{it.input_kind === 'trigger' && ticket.source !== 'workbench' ? ` · ${ticket.source}` : ''} · {when(it.ts)}</>} />)}
      {seg && <WorkSegmentView seg={seg} isLast={isLast} ticket={ticket} />}
      {isLast && <TicketApprovals ticket={ticket} />}
      {turn.replies.filter(r => (r.say || r.purpose) !== 'ack' && (r.say || r.purpose) !== 'acknowledgement').map((r, i) => <div key={i} className="text-ui-base leading-6 text-fg-subtle">{r.text}</div>)}
      {seg && seg.state !== 'running' && <Tail at={turn.endedAt ? turn.endedAt / 1000 : undefined}>{ticket.pr?.number && seg.finalText ? <a href={ticket.pr.url || '#'} target="_blank" rel="noreferrer">PR #{ticket.pr.number}</a> : null}<button className="hover:text-fg" onClick={() => { void navigator.clipboard?.writeText(seg.finalText || '') }}>复制</button></Tail>}
    </div>
  )
}

// ---- one job
function Job({ id, side }: { id: string; side?: 'left' | 'right' }) {
  const [conv, setConv] = useState<Conversation | null>(null); const [liveD, setLiveD] = useState<Live | null>(null); const [err, setErr] = useState('')
  const [now, setNow] = useState(Date.now())
  const act = useStore(s => s.act); const follow = useRef(true); const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    let alive = true; setConv(null); setLiveD(null)
    const tick = async () => {
      if (!alive) return
      try { const c = await GET<Conversation>(`/intake/tickets/${id}/conversation`); if (alive) { setConv(c); setErr('') }
        if (c.ticket.running) { const l = await GET<Live>(`/intake/tickets/${id}/live`); if (alive) setLiveD(l) } else setLiveD(null)
      } catch (e) { if (alive) setErr((e as Error).message) }
      if (alive) setTimeout(tick, 3000)
    }
    void tick(); return () => { alive = false }
  }, [id])
  useEffect(() => { const i = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(i) }, [])
  useEffect(() => { if (conv) markSeen(id) }, [conv?.ticket.updated, id])
  useEffect(() => { if (follow.current && box.current) box.current.scrollTop = box.current.scrollHeight }, [conv, liveD])
  const turns = useMemo(() => {
    if (!conv) return []
    const items = conv.items.map(it => it.kind === 'assignment' && it.state === 'running' && liveD?.messages ? { ...it, traj: { messages: liveD.messages }, cost: liveD.cost ?? it.cost } : it)
    return buildTurns(items as ConvItem[], { running: conv.ticket.running, nowMs: now })
  }, [conv, liveD, now])
  if (!conv) return <div className="p-6">{err ? <ErrorLine>{err}</ErrorLine> : <p className="text-ui-base text-fg-subtlest">加载中</p>}</div>
  const t = conv.ticket; const running = t.running; const stopped = t.state === 'ended' && t.ended_by === 'person'
  const sb = liveD?.sandbox || [...conv.items].reverse().find(i => i.kind === 'assignment' && i.sandbox)?.sandbox
  const link = (rail: string, extra = '') => `#/w/${id}?rail=${rail}&sandbox=${sb}${extra}`
  const lastErr = t.last_report && (t.last_report.state === 'failed' || t.last_report.state === 'blocked') ? `${stageLabel(t.stage)}:${t.last_report.text || ''}` : null
  return (
    <div className="flex h-full flex-col">
      <header className={cx('flex h-12 shrink-0 items-center gap-3 px-6', side && 'border-b border-border')}>
        <span className="min-w-0 flex-1 truncate text-ui-base font-medium text-fg" title={t.title}>{t.title}</span>
        {side !== 'right' ? <SplitPicker current={id} /> : <a href={`#/w/${location.hash.match(/^#\/w\/([^/?]+)/)?.[1] || id}`} className="shrink-0 rounded-md px-2 py-1 text-ui-sm text-fg-subtle hover:bg-hover hover:text-fg" title="关闭这一栏">关闭</a>}
        <span className="shrink-0 whitespace-nowrap text-ui-sm text-fg-subtle">{stageLabel(t.stage)}</span>
        <span className="shrink-0 whitespace-nowrap font-mono text-ui-sm text-fg-subtlest">{t.branch}</span>
        {t.pr?.number && <a className="shrink-0 text-ui-sm" href={t.pr.url || '#'} target="_blank" rel="noreferrer">PR #{t.pr.number}</a>}
        {sb && <div className="flex shrink-0 items-center gap-0.5 whitespace-nowrap">{[['diff', 'diff', '&base=main'], ['files', '文件', '&path=/work/repo'], ['terminal', '终端', ''], ['events', '事件', '']].map(([r, l, x]) => <a key={r} href={link(r, x)} className="rounded-md px-2 py-1 text-ui-sm text-fg-subtle hover:bg-hover hover:text-fg">{l}</a>)}</div>}
        <a href={`#/tickets/${id}`} className="shrink-0 whitespace-nowrap rounded-md px-2 py-1 text-ui-sm text-fg-subtle hover:bg-hover hover:text-fg">详情</a>
      </header>
      <div ref={box} onScroll={e => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60 }} className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 pb-6 pt-2">
          {turns.map((turn, i) => <TurnView key={turn.key} turn={turn} isLast={i === turns.length - 1} ticket={t} />)}
        </div>
      </div>
      <Composer draftKey={id} running={running} disabled={stopped} error={lastErr} onStop={running ? () => { if (window.confirm('停止这件事?')) void act.stopTicket(id) } : undefined}
        placeholder={stopped ? '这件事已被你停止' : t.attention ? `回答它:${t.attention.question}` : running ? '插话' : '继续这件事'} hint={<span className="font-mono">{t.short}</span>}
        onSend={async text => { await act.replyTicket(id, text) }}
        actions={t.state === 'open' && !running ? <button onClick={() => act.retryTicket(id)} className="h-7 rounded-md px-2 text-ui-sm text-fg-subtle hover:bg-hover hover:text-fg">重试</button> : null} />
    </div>
  )
}

// ---- the conversation home: every job's turns in one stream, newest last (docs/005 对话)
const SAY_LABEL: Record<string, string> = { ready: '做完了', stuck: '卡住了', question: '要你回答', approval: '要你同意', digest: '进展', update: '更新了' }
function Say({ r }: { r: ConvRow }) {
  const k = r.say || 'ack'
  if (k === 'ack') return null
  if (k === 'update') return <UpdateCard r={r} />
  if (k === 'ready' || k === 'digest') return <div className="flex flex-col gap-2"><Prose text={r.text} /><Tail at={r.ts}>{r.ticket && <a href={`#/w/${r.ticket}`}>{r.short}</a>}{r.pr?.number && <a href={r.pr.url || '#'} target="_blank" rel="noreferrer">PR #{r.pr.number}</a>}</Tail></div>
  if (k === 'approval' && r.meta?.approval) return <ApprovalLive id={String(r.meta.approval)} kind={String(r.meta.kind || '')} draft={r.text.replace(/^要你同意:/, '').replace(/ This is by an automated agent.*$/, '').replace(/\n\[(拦下|注意)\][^\n]*/g, '')} />
  const tone = k === 'stuck' ? 'border-destructive/40 bg-destructive-surface' : 'border-waiting/40 bg-waiting-surface'
  return <div className={cx('max-w-2xl rounded-xl border px-4 py-3 text-ui-base', tone)}><div className="mb-1 flex items-center gap-2 text-ui-sm"><span className="font-medium text-fg">{SAY_LABEL[k]}</span>{r.ticket && <a href={`#/w/${r.ticket}`}>{r.short}</a>}</div><div className="whitespace-pre-wrap break-words text-fg">{r.text}</div></div>
}
/** A system note. A correction that became a rule is shown as what it is -- something the agent will now do differently -- with a way to take it back right here (001 第 6 阶段). */
function Note({ r }: { r: ConvRow }) {
  const [gone, setGone] = useState(false); const [err, setErr] = useState('')
  const rule = r.meta?.rule as string | undefined
  const forget = async () => { try { await DEL(`/intake/memory/${rule}`); setGone(true) } catch (e) { setErr((e as Error).message) } }
  if (rule) return (
    <div className="flex max-w-2xl items-start gap-2 text-ui-sm text-fg-subtle">
      <span className="mt-0.5 shrink-0 rounded-full border border-border px-2 text-ui-xs text-fg-subtle">{gone ? '已忘' : '记住了'}</span>
      <span className={cx('min-w-0 break-words', gone && 'line-through opacity-60')}>{r.text.replace(/^记住了:/, '')}</span>
      {!gone && <button onClick={forget} className="shrink-0 text-fg-subtlest hover:text-destructive" title="从记忆里删掉这一条">忘了这条</button>}
      {err && <span className="text-destructive">{err}</span>}
    </div>
  )
  if (r.meta?.handoff) return <div className="flex max-w-2xl items-start gap-2 text-ui-sm text-fg-subtle"><span className="mt-0.5 shrink-0 rounded-full border border-border px-2 text-ui-xs">交接</span><span className="min-w-0 whitespace-pre-wrap break-words">{r.text}</span></div>
  return <div className="text-ui-sm text-fg-subtlest">{r.text}</div>
}

/** 001 第 10 阶段: one sentence about the promotion, and a way back. */
function UpdateCard({ r }: { r: ConvRow }) {
  const [msg, setMsg] = useState('')
  const back = async () => { try { const v = await POST<{ version?: { label?: string; id?: string } | null; changed: boolean }>('/intake/agent/version', { version: 'previous', pin: true }); setMsg(v.changed ? `已退回到 ${v.version?.label || v.version?.id || '上一版'},并钉住` : '没有可退回的版本') } catch (e) { setMsg((e as Error).message) } }
  return (
    <div className="max-w-2xl rounded-xl border border-border bg-surface px-4 py-3 text-ui-base">
      <div className="mb-1 flex items-center gap-2 text-ui-sm"><span className="font-medium text-fg">更新了</span><a href="#/versions" className="text-ui-sm">版本</a><span className="ml-auto text-fg-subtlest">{when(r.ts)}</span></div>
      <div className="whitespace-pre-wrap break-words text-fg">{r.text.replace(/回复「退回」可换回上一版。$/, '')}</div>
      <div className="mt-2 flex items-center gap-3">{r.meta?.applied !== false && r.meta?.previous ? <button onClick={back} className="h-7 rounded-md border border-border px-2 text-ui-sm text-fg hover:bg-hover">退回上一版</button> : null}{msg && <span className="text-ui-sm text-fg-subtle">{msg}</span>}</div>
    </div>
  )
}

/** The card reads its current state from the pending list so a decision made elsewhere shows here too. */
function ApprovalLive({ id, kind, draft }: { id: string; kind: string; draft: string }) {
  const approvals = useStore(s => s.approvals), load = useStore(s => s.load)
  useEffect(() => { void load.approvals(); const i = setInterval(() => void load.approvals(), 5000); return () => clearInterval(i) }, [])
  const pending = approvals.data?.some(a => a.id === id)
  const mine = approvals.data?.find(a => a.id === id)
  return <ApprovalCard id={id} kind={kind} draft={mine?.draft ?? draft} review={mine?.review} error={mine?.error} state={approvals.data && !pending ? 'decided' : undefined} />
}

function Home() {
  const [rows, setRows] = useState<ConvRow[] | null>(null); const [err, setErr] = useState('')
  const box = useRef<HTMLDivElement>(null); const follow = useRef(true); const lastEvent = useStore(s => s.lastEvent); const hash = useHash()
  const [spend, setSpend] = useState<{ spent_today?: number; daily_budget_usd?: number } | null>(null)
  const load = async () => { try { const r = await GET<{ items: ConvRow[] }>('/intake/conversation?limit=300'); setRows(r.items); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load(); const i = setInterval(() => void load(), 4000); return () => clearInterval(i) }, [])
  useEffect(() => { const f = () => GET<{ spent_today?: number; daily_budget_usd?: number }>('/intake/health').then(setSpend).catch(() => setSpend(null)); void f(); const i = setInterval(f, 30000); return () => clearInterval(i) }, [])
  useEffect(() => { void load() }, [lastEvent])
  useEffect(() => { if (follow.current && box.current) box.current.scrollTop = box.current.scrollHeight }, [rows])
  const send = async (text: string) => { try { await POST('/intake/conversation', { text, actor: 'workbench' }); await load() } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="flex h-full flex-col">
      <div ref={box} onScroll={e => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60 }} className="min-h-0 flex-1 overflow-auto">
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 px-4 py-6">
          {err && <ErrorLine>{err}</ErrorLine>}
          {rows && !rows.length && <div className="py-24 text-center"><div className="text-ui-xl font-medium text-fg">这是你的 agent</div><div className="mt-2 text-ui-base text-fg-subtle">告诉它一件事。它在做完、卡住、要你决定时才会说话。</div></div>}
          {rows?.map(r => r.kind === 'message' ? <Bubble key={r.id} text={r.text} meta={<>{r.actor === 'system' ? '平台' : r.actor}{r.origin?.source && r.origin.source !== 'workbench' ? ` · ${r.origin.source}` : ''}{r.ticket ? <> · 对 <a href={`#/w/${r.ticket}`}>{r.short}</a></> : null} · {when(r.ts)}</>} />
            : r.kind === 'say' ? <Say key={r.id} r={r} />
            : r.kind === 'work' ? <a key={r.id} href={`#/w/${r.ticket}`} className="inline-flex max-w-full items-center gap-2 text-ui-base text-fg-subtle hover:text-fg"><span className="truncate">{r.stage && !['ended', 'person/review'].includes(r.stage) ? '工作中' : '已工作'} · {(r.text || '').slice(0, 48)}{(r.text || '').length > 48 ? '…' : ''}</span><span className="text-ui-sm text-fg-subtlest">{stageLabel(r.stage)}</span><Chevron open={false} /></a>
            : <Note key={r.id} r={r} />)}
        </div>
      </div>
      <Composer draftKey="home" placeholder="告诉它一件事" hint={<span>新任务{spend?.daily_budget_usd ? <span className={cx('ml-2', (spend.spent_today ?? 0) >= spend.daily_budget_usd ? 'text-destructive' : 'text-fg-subtlest')} title="它自己发起的事(CI、例行)今天最多花这么多;你开口要的事不受这个限制">今天 ${(spend.spent_today ?? 0).toFixed(2)} / ${spend.daily_budget_usd.toFixed(2)}</span> : null}</span>} autoFocus={hash.includes('new=1')} onSend={send} />
    </div>
  )
}

/** 分屏 (docs/008, ZCode's split panes, two leaves): `#/w/<a>?split=<b>` shows two jobs side by side, each with its own composer. */
function SplitPicker({ current }: { current: string }) {
  const tickets = useStore(s => s.tickets)
  const others = (tickets.data ?? []).filter(t => t.id !== current).slice(0, 30)
  if (!others.length) return null
  return (
    <select className="h-7 shrink-0 rounded-md border border-border bg-transparent px-1 text-ui-sm text-fg-subtle hover:bg-hover" value="" title="并排看另一件事" onChange={e => { if (e.target.value) location.hash = `#/w/${current}?split=${e.target.value}` }}>
      <option value="">并排…</option>
      {others.map(t => <option key={t.id} value={t.id}>{(t.title || t.short).slice(0, 48)}</option>)}
    </select>
  )
}

function SessionPage({ params: [id] }: { params: string[] }) {
  const hash = useHash(); const split = new URLSearchParams(hash.split('?')[1] || '').get('split')
  if (!id) return <Home />
  if (split && split !== id) return (
    <div className="flex h-full min-w-0">
      <div className="flex min-w-0 flex-1 flex-col border-r border-border"><Job key={id} id={id} side="left" /></div>
      <div className="flex min-w-0 flex-1 flex-col"><Job key={split} id={split} side="right" /></div>
    </div>
  )
  return <Job id={id} />
}

export const session: Plugin = {
  id: 'session', requires: ['rails'],
  activate(ctx) {
    ctx.nav({ id: 'session', label: '对话', href: '#/w', order: -10, badge: s => (s as { tickets: { data: TicketRow[] | null } }).tickets.data?.filter(t => t.running).length || null })
    ctx.slot({ slot: 'sidebar', id: 'tasks', component: TaskList as React.ComponentType<Record<string, unknown>>, order: 10 })
    ctx.route({ id: 'session', pattern: /^#\/w(?:\/([^/?]+))?$/, component: SessionPage, nav: 'session' })
    ctx.route({ id: 'home', pattern: /^#\/?$/, component: SessionPage, nav: 'session' })
  },
}
