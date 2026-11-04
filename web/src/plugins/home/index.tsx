// 我的目录: the personal harness directory (docs/004 个人 harness 目录; D29) as a page. What is here reaches the next run as-is (no gate);
// the machine's proposals are listed, never applied from here (apply is `python -m aios.home apply`, on purpose a step a person takes).
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { DEL, GET, PUT } from '../../api/client'
import { Badge, Button, ErrorLine, Input, Panel, TextArea } from '../../ui'

type View = { dir: string; digest: string; in_sandbox: string; files: { path: string; size: number }[]; proposals: string[]; problems: string[]; limit_bytes: number }
type File = { path: string; text: string; digest: string }

const STARTERS: [string, string][] = [['rules/01.md', '一条规则:一个文件一条,文件名序;HTML 注释不算。'], ['memory.md', 'harness 自己的笔记,替换包内的 memory.md。'], ['prompts/system.md', '替换系统模板。'], ['prompts/instance.md', '替换任务模板,必须含 {{task}}。'], ['config.json', '覆盖包内 config.json:字典逐层合并,其他整体替换。']]

function HomePage() {
  const [v, setV] = useState<View | null>(null); const [err, setErr] = useState(''); const [sel, setSel] = useState<string | null>(null); const [f, setF] = useState<File | null>(null); const [text, setText] = useState(''); const [newPath, setNewPath] = useState('')
  const load = async () => { try { setV(await GET<View>('/intake/home')); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load() }, [])
  const open = async (p: string) => { setSel(p); try { const r = await GET<File>(`/intake/home/file?path=${encodeURIComponent(p)}`); setF(r); setText(r.text) } catch (e) { setF({ path: p, text: '', digest: '' }); setText(''); if (!(e as Error).message.includes('404')) setErr((e as Error).message) } }
  const save = async () => { if (!sel) return; try { await PUT('/intake/home/file', { path: sel, text }); setErr(''); await load(); await open(sel) } catch (e) { setErr((e as Error).message) } }
  const remove = async (p: string) => { if (!window.confirm(`删除 ${p}?`)) return; try { await DEL(`/intake/home/file?path=${encodeURIComponent(p)}`); if (sel === p) { setSel(null); setF(null) } await load() } catch (e) { setErr((e as Error).message) } }
  const dirty = f !== null && text !== f.text
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex items-center gap-3"><h1 className="text-ui-xl font-semibold">我的目录</h1>{v && <Badge tone={v.problems.length ? 'destructive' : v.digest ? 'brand' : 'neutral'}>{v.problems.length ? `${v.problems.length} 个问题` : v.digest ? `版本 ${v.digest}` : '空'}</Badge>}</div>
      <p className="text-ui-sm text-fg-subtle">你带到每次任务里的东西:规则、笔记、提示、技能、中间件。改了下一次派工就带着(送进沙箱的 {v?.in_sandbox || '/work/home'},只读);机器只能往 proposals/ 写提案,`python -m aios.home apply &lt;id&gt;` 才搬进来。{v?.dir && <span className="ml-1 font-mono text-fg-subtlest">{v.dir}</span>}</p>
      {err && <ErrorLine>{err}</ErrorLine>}
      {v && v.problems.length > 0 && <Panel title="check 不过(这样的目录不开跑)"><ul className="list-disc pl-5 text-ui-base text-destructive">{v.problems.map(p => <li key={p}>{p}</li>)}</ul></Panel>}
      <div className="grid gap-4 md:grid-cols-[18rem_1fr]">
        <Panel title="文件">
          {v?.files.length === 0 && <p className="mb-2 text-ui-sm text-fg-subtlest">还什么都没有。</p>}
          {v?.files.map(x => <div key={x.path} className={`flex items-center gap-2 rounded px-1 py-0.5 text-ui-base ${sel === x.path ? 'bg-accent' : ''}`}><button className="flex-1 truncate text-left font-mono hover:text-fg" onClick={() => void open(x.path)}>{x.path}</button><span className="text-ui-sm text-fg-subtlest">{x.size}</span><button className="text-ui-sm text-fg-subtle hover:text-destructive" onClick={() => void remove(x.path)}>删</button></div>)}
          <div className="mt-2 flex gap-1"><Input className="flex-1" value={newPath} onChange={e => setNewPath(e.target.value)} placeholder="rules/02.md" list="home-starters" /><Button size="sm" disabled={!newPath.trim()} onClick={() => { void open(newPath.trim()); setNewPath('') }}>新建</Button></div>
          <datalist id="home-starters">{STARTERS.map(([p]) => <option key={p} value={p} />)}</datalist>
          {v && v.proposals.length > 0 && <div className="mt-3 border-t border-border pt-2"><div className="mb-1 text-ui-caption text-fg-subtle">机器的提案(待你 apply)</div>{v.proposals.map(p => <div key={p} className="font-mono text-ui-sm">{p}</div>)}</div>}
        </Panel>
        <Panel title={sel ? sel : '选一个文件,或新建'} actions={sel && <div className="flex items-center gap-2 text-ui-sm text-fg-subtle"><span>{text.length}/{v?.limit_bytes ?? '-'}</span><Button size="sm" kind="primary" disabled={!dirty} onClick={save}>保存</Button></div>}>
          {sel ? <TextArea rows={22} className="font-mono" value={text} onChange={e => setText(e.target.value)} placeholder={STARTERS.find(([p]) => p === sel)?.[1] || ''} /> : <ul className="text-ui-sm text-fg-subtle">{STARTERS.map(([p, d]) => <li key={p}><span className="font-mono">{p}</span> — {d}</li>)}</ul>}
        </Panel>
      </div>
    </div>
  )
}

export const home: Plugin = {
  id: 'home',
  activate(ctx) {
    ctx.nav({ id: 'home', label: '我的目录', href: '#/home', order: 36 })
    ctx.route({ id: 'home', pattern: /^#\/home$/, component: HomePage, nav: 'home' })
  },
}
