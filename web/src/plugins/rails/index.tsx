// Right-dock panels shared by pages: terminal (PTY session), files, kernel ABI event stream, one-shot exec.
// Opened with `?rail=<id>&sandbox=<id>` on any page.
import { useEffect, useRef, useState } from 'react'
import type { Plugin } from '../../kernel'
import { api, execStream, GET, POST, DEL } from '../../api/client'
import type { Event } from '../../api/types'
import { Button, Input, cx, when } from '../../ui'

const b64e = (s: string) => btoa(String.fromCharCode(...new TextEncoder().encode(s)))
const b64d = (b: string) => { const bin = atob(b); const u = new Uint8Array(bin.length); for (let i = 0; i < u.length; i++) u[i] = bin.charCodeAt(i); return new TextDecoder().decode(u) }
const stripAnsi = (s: string) => s.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '').replace(/\x1b\][^\x07]*\x07/g, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n')

function Terminal({ args }: { args: Record<string, string> }) {
  const sid = args.sandbox; const [session, setSession] = useState<string | null>(null); const [out, setOut] = useState(''); const [cmd, setCmd] = useState(''); const [err, setErr] = useState('')
  const pre = useRef<HTMLPreElement>(null); const follow = useRef(true)
  useEffect(() => {
    let alive = true; let sess: string | null = null
    ;(async () => {
      try {
        const r = await POST<{ session: string }>(`/sandboxes/${sid}/sessions`, { user: 'agent', pty: true })
        if (!alive) { void DEL(`/sandboxes/${sid}/sessions/${r.session}`); return }      // unmounted while opening (StrictMode double-mount, quick close)
        sess = r.session; setSession(r.session)
      } catch (e) { setErr((e as Error).message); return }
      while (alive) {
        try { const r = await POST<{ data_b64: string }>(`/sandboxes/${sid}/sessions/${sess}/read`, { wait: 1 }); if (r.data_b64) setOut(o => (o + stripAnsi(b64d(r.data_b64))).slice(-200000)) } catch { await new Promise(r => setTimeout(r, 1000)) }
      }
    })()
    return () => { alive = false; if (sess) void DEL(`/sandboxes/${sid}/sessions/${sess}`) }
  }, [sid])
  useEffect(() => { if (follow.current && pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [out])
  const send = async () => { if (!session) return; const t = cmd; setCmd(''); await POST(`/sandboxes/${sid}/sessions/${session}/write`, { data_b64: b64e(t + '\n') }) }
  return (
    <div className="flex h-full flex-col">
      {err && <p className="px-3 py-1 text-ui-sm text-destructive">{err}</p>}
      <pre ref={pre} onScroll={e => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40 }} className="out min-h-0 flex-1 overflow-auto bg-bg-alt p-2">{out || (session ? '' : '打开会话中')}</pre>
      <div className="flex gap-1 border-t border-border p-2"><Input className="flex-1 font-mono" value={cmd} onChange={e => setCmd(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void send() }} placeholder="命令,回车发送" /><Button size="sm" onClick={send}>发送</Button></div>
    </div>
  )
}

function Exec({ args }: { args: Record<string, string> }) {
  const sid = args.sandbox; const [cmd, setCmd] = useState('ls -la'); const [user, setUser] = useState('agent'); const [out, setOut] = useState<{ k: string; t: string }[]>([]); const [busy, setBusy] = useState(false); const abort = useRef<AbortController | null>(null)
  useEffect(() => () => abort.current?.abort(), [])                                       // closing the rail stops the stream
  const run = async () => {
    setBusy(true); setOut([]); abort.current = new AbortController(); const ac = abort.current
    try { for await (const ev of execStream(sid, { cmd, user, timeout: 600 }, ac.signal)) { if (ac.signal.aborted) break; if (ev.done) setOut(o => [...o, { k: 'meta', t: `exit ${ev.exit}${ev.timeout ? ' (timeout)' : ''} · ${Number(ev.elapsed_s).toFixed(2)}s` }]); else setOut(o => [...o, { k: ev.event === 'err' || ev.event === 'stderr' ? 'stderr' : 'stdout', t: String(ev.data) }]) } }
    catch (e) { if (!ac.signal.aborted) setOut(o => [...o, { k: 'meta', t: (e as Error).message }]) } finally { setBusy(false) }
  }
  return (
    <div className="flex h-full flex-col">
      <div className="flex gap-1 border-b border-border p-2"><Input className="flex-1 font-mono" value={cmd} onChange={e => setCmd(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !busy) void run() }} /><select className="rounded border border-border bg-input px-1 text-ui-sm" value={user} onChange={e => setUser(e.target.value)}><option>agent</option><option>root</option></select>{busy ? <Button size="sm" onClick={() => abort.current?.abort()}>停止</Button> : <Button size="sm" kind="primary" onClick={run}>运行</Button>}</div>
      <pre className="out min-h-0 flex-1 overflow-auto bg-bg-alt p-2">{out.map((o, i) => <span key={i} className={cx(o.k === 'stderr' && 'text-destructive', o.k === 'meta' && 'text-fg-subtle')}>{o.t}{o.k === 'meta' ? '\n' : ''}</span>)}</pre>
    </div>
  )
}

function Files({ args }: { args: Record<string, string> }) {
  const sid = args.sandbox; const [path, setPath] = useState(args.path || '/home/agent'); const [list, setList] = useState<string>(''); const [content, setContent] = useState<string | null>(null); const [err, setErr] = useState('')
  const load = async (p: string) => {
    setErr(''); setContent(null)
    try {
      const r = await POST<{ exit: number; stdout: string; stderr: string }>(`/sandboxes/${sid}/exec`, { cmd: `if [ -d "$FC_PATH" ]; then ls -la --group-directories-first "$FC_PATH"; else echo __FILE__; fi`, user: 'root', timeout: 20, env: { FC_PATH: p } })
      if (r.stdout.startsWith('__FILE__')) { const raw = await api<ArrayBuffer>('GET', `/sandboxes/${sid}/files?path=${encodeURIComponent(p)}`, undefined, { raw: true }); const u = new Uint8Array(raw); const bad = u.subarray(0, 4096).filter(c => c === 0).length > 0; setContent(bad ? `(二进制,${u.length} 字节)` : new TextDecoder().decode(u).slice(0, 200000)); setList('') }
      else setList(r.stdout || r.stderr)
    } catch (e) { setErr((e as Error).message) }
  }
  useEffect(() => { void load(path) }, [sid])
  const names = list.split('\n').slice(1).map(l => l.trim().split(/\s+/).slice(8).join(' ')).filter(n => n && n !== '.' && n !== '..')
  return (
    <div className="flex h-full flex-col">
      <div className="flex gap-1 border-b border-border p-2"><Input className="flex-1 font-mono" value={path} onChange={e => setPath(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void load(path) }} /><Button size="sm" onClick={() => { const up = path.replace(/\/[^/]*\/?$/, '') || '/'; setPath(up); void load(up) }}>上一级</Button></div>
      {err && <p className="px-3 py-1 text-ui-sm text-destructive">{err}</p>}
      {content != null ? <pre className="out min-h-0 flex-1 overflow-auto p-2">{content}</pre> : <ul className="min-h-0 flex-1 overflow-auto p-2 text-ui-base">{names.map(n => <li key={n}><button className="font-mono hover:text-brand" onClick={() => { const p = (path.replace(/\/$/, '') + '/' + n); setPath(p); void load(p) }}>{n}</button></li>)}</ul>}
    </div>
  )
}

function Events({ args }: { args: Record<string, string> }) {
  const sid = args.sandbox; const [events, setEvents] = useState<Event[]>([]); const [err, setErr] = useState(''); const since = useRef(0)
  useEffect(() => {
    let alive = true; since.current = 0; setEvents([])
    const tick = async () => { if (!alive) return; try { const r = await GET<{ events: Event[]; last_seq: number }>(`/sandboxes/${sid}/events?since=${since.current}&max=500`); if (r.events.length) { setEvents(e => [...e, ...r.events].slice(-2000)); since.current = r.last_seq } setErr('') } catch (e) { setErr((e as Error).message) } if (alive) setTimeout(tick, 2000) }
    void tick(); return () => { alive = false }
  }, [sid])
  return (
    <div className="h-full overflow-auto p-2">
      {err && <p className="text-ui-sm text-destructive">{err}</p>}
      <table className="w-full text-ui-sm"><tbody>{events.map(e => <tr key={e.seq} className="border-b border-border align-top"><td className="whitespace-nowrap pr-2 text-fg-subtle">{e.seq}</td><td className="whitespace-nowrap pr-2 text-fg-subtle">{when(e.ts)}</td><td className="pr-2 font-medium">{e.type}</td><td className="break-all font-mono text-ui-xs">{JSON.stringify(e.data).slice(0, 300)}</td></tr>)}</tbody></table>
      {!events.length && <p className="text-ui-base text-fg-subtle">还没有事件。</p>}
    </div>
  )
}

function Diff({ args }: { args: Record<string, string> }) {
  const sid = args.sandbox; const [base, setBase] = useState(args.base || 'main'); const [cwd, setCwd] = useState(args.cwd || '/work/repo'); const [d, setD] = useState<{ stat: string; diff: string; stderr: string } | null>(null); const [err, setErr] = useState('')
  const load = () => GET<{ stat: string; diff: string; stderr: string }>(`/sandboxes/${sid}/diff?base=${encodeURIComponent(base)}&cwd=${encodeURIComponent(cwd)}`).then(r => { setD(r); setErr('') }).catch(e => setErr((e as Error).message))
  useEffect(() => { void load() }, [sid])
  const lines = (d?.diff || '').split('\n')
  return (
    <div className="flex h-full flex-col">
      <div className="flex gap-1 border-b border-border p-2"><Input className="w-28 font-mono" value={base} onChange={e => setBase(e.target.value)} title="基线" /><Input className="flex-1 font-mono" value={cwd} onChange={e => setCwd(e.target.value)} title="仓库目录" /><Button size="sm" onClick={load}>刷新</Button></div>
      {err && <p className="px-3 py-1 text-ui-sm text-destructive">{err}</p>}
      {d && <pre className="out border-b border-border bg-bg-alt p-2 text-fg-subtle">{d.stat || '(没有差异)'}{d.stderr ? '\n' + d.stderr : ''}</pre>}
      <pre className="out min-h-0 flex-1 overflow-auto p-2">{lines.map((l, i) => <span key={i} className={cx('block', l.startsWith('+') && !l.startsWith('+++') && 'bg-diff-add', l.startsWith('-') && !l.startsWith('---') && 'bg-diff-del', (l.startsWith('@@') || l.startsWith('diff ')) && 'text-fg-subtle')}>{l}</span>)}</pre>
    </div>
  )
}

export const rails: Plugin = {
  id: 'rails',
  activate(ctx) {
    ctx.rail({ id: 'terminal', label: '终端', component: Terminal, order: 10 })
    ctx.rail({ id: 'exec', label: '执行', component: Exec, order: 11 })
    ctx.rail({ id: 'files', label: '文件', component: Files, order: 20 })
    ctx.rail({ id: 'events', label: '事件流', component: Events, order: 30 })
    ctx.rail({ id: 'diff', label: 'diff', component: Diff, order: 40 })
  },
}
