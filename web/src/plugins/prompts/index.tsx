// 提示 (dsh-superagent 004, D27): the team's own words for how the agent works. Three sections, each saved as a new version; left alone, a section follows the built-in text.
// This is advice the agent reads. What the host enforces (the gate, approvals, the review) is not editable here.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { api, GET } from '../../api/client'
import { Badge, Button, ErrorLine, Panel, TextArea, when } from '../../ui'

interface Section { label: string; default: string; text: string; overridden: boolean; version: number; default_updated: boolean; history: { n: number; by?: string; at?: number; size: number; reset: boolean }[] }
interface View { sections: Record<string, Section>; limit: number }

function One({ name, s, limit, reload }: { name: string; s: Section; limit: number; reload: () => Promise<void> }) {
  const [text, setText] = useState(s.text); const [err, setErr] = useState(''); const [open, setOpen] = useState(false)
  useEffect(() => setText(s.text), [s.text, s.version])
  const dirty = text.trim() !== s.text.trim()
  const run = async (f: () => Promise<unknown>) => { try { await f(); setErr(''); await reload() } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="mb-4 border-b border-border pb-4 last:border-0">
      <div className="mb-1 flex items-center gap-2 text-ui-base font-medium">{s.label}
        {s.overridden ? <Badge tone="brand">团队写的 · 版本 {s.version}</Badge> : <Badge>内置</Badge>}
        {s.default_updated && <Badge tone="warning" title="内置文字更新了,你的版本还是旧基础上写的">内置已更新</Badge>}</div>
      <TextArea rows={4} value={text} onChange={e => setText(e.target.value)} placeholder={s.default || '(没有内置文字;写下团队对这一项的要求)'} />
      <div className="mt-1 flex items-center gap-2 text-ui-sm text-fg-subtle">
        <span>{text.length}/{limit}</span>
        <Button size="sm" kind="primary" disabled={!dirty || text.length > limit} onClick={() => run(() => api('PUT', `/intake/prompts/${name}`, { text, base: s.version }))}>保存为新版本</Button>
        {s.overridden && <Button size="sm" onClick={() => run(() => api('DELETE', `/intake/prompts/${name}`))}>回到内置</Button>}
        {s.history.length > 0 && <Button size="sm" onClick={() => setOpen(!open)}>{open ? '收起历史' : `历史(${s.history.length})`}</Button>}
      </div>
      {open && <ul className="mt-1 text-ui-sm text-fg-subtle">{s.history.slice().reverse().map(h => <li key={h.n}>版本 {h.n} · {h.reset ? '回到内置' : `${h.size} 字`} · {h.by || '-'} · {when(h.at)}</li>)}</ul>}
      {err && <ErrorLine>{err}</ErrorLine>}
    </div>
  )
}

function PromptsPanel() {
  const [v, setV] = useState<View | null>(null); const [err, setErr] = useState('')
  const load = async () => { try { setV(await GET<View>('/intake/prompts')); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load() }, [])
  return (
    <Panel title="提示">
      <p className="mb-3 text-ui-sm text-fg-subtle">团队对 agent 的要求:怎么说话、范围、怎么交付。这是写给 agent 看的建议;门、批准、审查这些宿主的检查不读它,也改不了。每次保存是一个新版本,可以回到内置。</p>
      {err && <ErrorLine>{err}</ErrorLine>}
      {v && Object.entries(v.sections).map(([k, s]) => <One key={k} name={k} s={s} limit={v.limit} reload={load} />)}
    </Panel>
  )
}

export const prompts: Plugin = {
  id: 'prompts',
  activate(ctx) {
    ctx.slot({ slot: 'settings', id: 'prompts', component: PromptsPanel as React.ComponentType<Record<string, unknown>>, order: 20 })
  },
}
