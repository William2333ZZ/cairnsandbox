// 例行 (docs/005 例行, docs/008): the person's routines — text, when, budget, last run; pause / resume / delete. New ones are
// made in the conversation ("以后每天早上八点……") and only exist once the person approved the draft.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { GET, POST } from '../../api/client'
import { ErrorLine, cx, when } from '../../ui'

interface Routine { id: string; text: string; every_s?: number; daily_at?: string; enabled?: boolean; budget_usd?: number; last?: number | null }
const whenLabel = (r: Routine) => r.daily_at ? `每天 ${r.daily_at}` : r.every_s ? (r.every_s % 86400 === 0 ? `每 ${r.every_s / 86400} 天` : r.every_s % 3600 === 0 ? `每 ${r.every_s / 3600} 小时` : `每 ${Math.round(r.every_s / 60)} 分钟`) : '—'

function RoutinesPage() {
  const [rows, setRows] = useState<Routine[] | null>(null); const [err, setErr] = useState('')
  const load = async () => { try { setRows((await GET<{ routines: Routine[] }>('/intake/routines')).routines); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load(); const i = setInterval(() => void load(), 15000); return () => clearInterval(i) }, [])
  const upd = async (id: string, body: Record<string, unknown>) => { try { setRows((await POST<{ routines: Routine[] }>(`/intake/routines/${id}`, body)).routines) } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-6 py-6">
      <div><h1 className="text-ui-xl font-medium text-fg">例行</h1><p className="mt-1 text-ui-base text-fg-subtle">到点自己醒来做的事。在对话里说"以后每天早上八点……",同意草稿后就出现在这里。</p></div>
      {err && <ErrorLine>{err}</ErrorLine>}
      {rows && !rows.length && <p className="text-ui-base text-fg-subtlest">还没有例行。</p>}
      <div className="flex flex-col gap-2">
        {rows?.map(r => (
          <div key={r.id} className={cx('flex items-center gap-3 rounded-xl border border-border bg-panel px-4 py-3', r.enabled === false && 'opacity-60')}>
            <div className="min-w-0 flex-1">
              <div className="truncate text-ui-base text-fg">{r.text}</div>
              <div className="mt-0.5 flex gap-3 text-ui-sm text-fg-subtle"><span>{whenLabel(r)}</span>{r.budget_usd != null && <span>预算 ${r.budget_usd}</span>}<span>{r.last ? `上次 ${when(r.last)}` : '还没跑过'}</span>{r.enabled === false && <span>已停用</span>}</div>
            </div>
            <button onClick={() => upd(r.id, { enabled: r.enabled === false })} className="h-7 rounded-md border border-border px-2 text-ui-sm text-fg hover:bg-hover">{r.enabled === false ? '启用' : '停用'}</button>
            <button onClick={() => { if (window.confirm('删除这条例行?')) void upd(r.id, { delete: true }) }} className="h-7 rounded-md px-2 text-ui-sm text-fg-subtle hover:bg-destructive-surface hover:text-destructive">删除</button>
          </div>
        ))}
      </div>
    </div>
  )
}

export const routines: Plugin = {
  id: 'routines',
  activate(ctx) {
    ctx.nav({ id: 'routines', label: '例行', href: '#/routines', order: 4 })
    ctx.route({ id: 'routines', pattern: /^#\/routines$/, component: RoutinesPage, nav: 'routines' })
  },
}
