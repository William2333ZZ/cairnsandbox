// 记忆 (docs/005 记忆, docs/008): the agent's four layers, shown and editable by the person. Facts can be added and deleted;
// log and notes can be deleted; artifacts are the jobs' PRs and workspace layers. Memory is information, never instructions.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { DEL, GET, POST, PUT } from '../../api/client'
import { Button, ErrorLine, Input, age, cx } from '../../ui'

interface Row { id: string; layer: string; text: string; ticket?: string | null; repo?: string | null; source?: string; created: number; chk?: { kind: string; pattern: string } | null }
interface Memory { profile: Row[]; log: Row[]; notes: Row[]; rules: Row[]; artifacts: { ticket: string; short: string; title: string; pr?: { number?: number; url?: string } | null; workspace_layer?: string | null; updated: number }[] }

function MemoryPage() {
  const [m, setM] = useState<Memory | null>(null); const [err, setErr] = useState(''); const [text, setText] = useState('')
  const load = async () => { try { setM(await GET<Memory>('/intake/memory')); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load() }, [])
  const add = async () => { if (!text.trim()) return; setM(await POST<Memory>('/intake/memory/profile', { text: text.trim() })); setText('') }
  const del = async (id: string) => { await DEL(`/intake/memory/${id}`); await load() }
  const edit = async (id: string, t: string) => { if (!m) return; setM(await PUT<Memory>('/intake/memory/profile', { facts: m.profile.map(f => f.id === id ? { id, text: t } : { id: f.id, text: f.text }) })) }
  const Section = ({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) => (
    <section className="flex flex-col gap-2"><h2 className="text-ui-base font-medium text-fg">{title}<span className="ml-2 text-ui-sm font-normal text-fg-subtle">{hint}</span></h2>{children}</section>
  )
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-8 px-6 py-6">
      <div><h1 className="text-ui-xl font-medium text-fg">记忆</h1><p className="mt-1 text-ui-base text-fg-subtle">它记得你的四层。这些是信息,不是指令;删了就不再进任何派工。</p></div>
      {err && <ErrorLine>{err}</ErrorLine>}
      {m && <>
        <Section title="稳定的偏好" hint={`${m.profile.length} 条,最多 100 条;每件事结束后由一次模型调用增删,你也能改`}>
          {m.profile.map(f => <FactRow key={f.id} f={f} onDelete={() => del(f.id)} onEdit={t => edit(f.id, t)} />)}
          <div className="flex gap-2"><Input className="flex-1" value={text} onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void add() }} placeholder="加一条事实,比如:提交信息用中文" /><Button kind="primary" disabled={!text.trim()} onClick={add}>加</Button></div>
        </Section>
        <Section title="你纠正过的" hint="每次你说“不对”、否决一份草稿,它记下一条;之后每件事开始时都带着。不想让它记,就删">
          {!m.rules.length && <p className="text-ui-sm text-fg-subtlest">还没有。你纠正它一次,这里就多一条。</p>}
          {m.rules.map(r => <div key={r.id} className="group flex items-start gap-2 text-ui-base text-fg"><span className="min-w-0 flex-1 break-words">{r.text}</span>{r.chk && <span className="shrink-0 rounded-full border border-border px-2 text-ui-xs text-fg-subtle" title={`${({ path_forbid: '改到的路径不能匹配', new_file_regex: '新文件的路径必须匹配', added_forbid: '新增的行不能匹配' } as Record<string, string>)[r.chk.kind] || r.chk.kind} ${r.chk.pattern}`}>提交前检查</span>}{r.repo && <span className="shrink-0 font-mono text-ui-sm text-fg-subtlest">{r.repo}</span>}{r.ticket && <a className="shrink-0 font-mono text-ui-sm" href={`#/w/${r.ticket}`}>{r.ticket.slice(-8)}</a>}<span className="shrink-0 text-ui-sm text-fg-subtlest">{age(r.created)}</span><button onClick={() => del(r.id)} className="shrink-0 text-ui-sm text-fg-subtlest opacity-0 hover:text-destructive group-hover:opacity-100">删</button></div>)}
        </Section>
        <Section title="带日期的日志" hint="每件事结束记一条;派工开始时带最相关的 30 条">
          {!m.log.length && <p className="text-ui-sm text-fg-subtlest">还没有。</p>}
          {m.log.map(r => <div key={r.id} className="group flex items-start gap-2 text-ui-base text-fg"><span className="min-w-0 flex-1 break-words">{r.text}</span><button onClick={() => del(r.id)} className="shrink-0 text-ui-sm text-fg-subtlest opacity-0 hover:text-destructive group-hover:opacity-100">删</button></div>)}
        </Section>
        <Section title="工作笔记" hint="harness 在做事时写的;同一仓库上其他在做的事能看到">
          {!m.notes.length && <p className="text-ui-sm text-fg-subtlest">还没有。</p>}
          {m.notes.map(r => <div key={r.id} className="group flex items-start gap-2 text-ui-base text-fg"><a className="shrink-0 font-mono text-ui-sm" href={`#/w/${r.ticket}`}>{(r.ticket || '').slice(-8)}</a><span className="min-w-0 flex-1 break-words">{r.text}</span><span className="shrink-0 text-ui-sm text-fg-subtlest">{age(r.created)}</span><button onClick={() => del(r.id)} className="shrink-0 text-ui-sm text-fg-subtlest opacity-0 hover:text-destructive group-hover:opacity-100">删</button></div>)}
        </Section>
        <Section title="文件与证据" hint="每件事留下的 PR 与工作区层">
          {!m.artifacts.length && <p className="text-ui-sm text-fg-subtlest">还没有。</p>}
          {m.artifacts.map(a => <div key={a.ticket} className="flex items-center gap-2 text-ui-base"><a href={`#/w/${a.ticket}`} className="min-w-0 flex-1 truncate text-fg hover:underline">{a.title}</a>{a.pr?.number && <a href={a.pr.url || '#'} target="_blank" rel="noreferrer" className="shrink-0 text-ui-sm">PR #{a.pr.number}</a>}{a.workspace_layer && <span className="shrink-0 font-mono text-ui-sm text-fg-subtlest">{a.workspace_layer}</span>}</div>)}
        </Section>
      </>}
    </div>
  )
}

function FactRow({ f, onDelete, onEdit }: { f: Row; onDelete: () => void; onEdit: (t: string) => void }) {
  const [editing, setEditing] = useState(false); const [t, setT] = useState(f.text)
  if (editing) return <div className="flex gap-2"><Input className="flex-1" value={t} onChange={e => setT(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { onEdit(t.trim()); setEditing(false) } if (e.key === 'Escape') setEditing(false) }} autoFocus /><Button size="sm" kind="primary" onClick={() => { onEdit(t.trim()); setEditing(false) }}>存</Button></div>
  return <div className={cx('group flex items-start gap-2 text-ui-base text-fg')}><span className="min-w-0 flex-1 break-words">{f.text}</span><span className="shrink-0 text-ui-sm text-fg-subtlest">{f.source === 'person' ? '你写的' : '它学到的'}</span><button onClick={() => setEditing(true)} className="shrink-0 text-ui-sm text-fg-subtlest opacity-0 hover:text-fg group-hover:opacity-100">改</button><button onClick={onDelete} className="shrink-0 text-ui-sm text-fg-subtlest opacity-0 hover:text-destructive group-hover:opacity-100">删</button></div>
}

export const memory: Plugin = {
  id: 'memory',
  activate(ctx) {
    ctx.nav({ id: 'memory', label: '记忆', href: '#/memory', order: 5 })
    ctx.route({ id: 'memory', pattern: /^#\/memory$/, component: MemoryPage, nav: 'memory' })
  },
}
