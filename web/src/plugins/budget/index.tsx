// 预算 (docs/001 第 1 阶段, D20): what one job may cost, and what it may spend in a day on its own initiative (CI, routines, evaluation).
// What a person asks for is never held back by the daily limit. Edited here, stored in the policy.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { GET, PUT } from '../../api/client'
import { Button, ErrorLine, Input, Panel } from '../../ui'

interface Pol { defaults?: { budget_usd?: number; daily_budget_usd?: number } }
interface Health { spent_today?: number; daily_budget_usd?: number }

function BudgetPanel() {
  const [job, setJob] = useState(''); const [day, setDay] = useState(''); const [spent, setSpent] = useState<number | null>(null); const [err, setErr] = useState(''); const [saved, setSaved] = useState(false)
  const load = async () => {
    try {
      const [p, h] = await Promise.all([GET<Pol>('/intake/policy'), GET<Health>('/intake/health')])
      setJob(String(p.defaults?.budget_usd ?? '')); setDay(String(p.defaults?.daily_budget_usd ?? '')); setSpent(h.spent_today ?? 0); setErr('')
    } catch (e) { setErr((e as Error).message) }
  }
  useEffect(() => { void load() }, [])
  const ok = (v: string) => v.trim() !== '' && Number.isFinite(Number(v)) && Number(v) > 0
  const save = async () => {
    try { await PUT('/intake/policy', { defaults: { budget_usd: Number(job), daily_budget_usd: Number(day) } }); setSaved(true); setTimeout(() => setSaved(false), 1500); await load() } catch (e) { setErr((e as Error).message) }
  }
  return (
    <Panel title="预算" actions={<Button size="sm" kind="primary" disabled={!ok(job) || !ok(day)} onClick={save}>{saved ? '已保存' : '保存'}</Button>}>
      <p className="mb-2 text-ui-sm text-fg-subtle">每件事最多花多少;以及它自己发起的事(CI、例行、评测)一天合计最多花多少——到了就先停着。你开口要的事不受每日上限限制。</p>
      {err && <ErrorLine>{err}</ErrorLine>}
      <div className="flex flex-wrap items-center gap-4 text-ui-base">
        <label className="flex items-center gap-2">每件事 $<Input className="w-24" inputMode="decimal" value={job} onChange={e => setJob(e.target.value)} /></label>
        <label className="flex items-center gap-2">每天(自己发起的) $<Input className="w-24" inputMode="decimal" value={day} onChange={e => setDay(e.target.value)} /></label>
        {spent !== null && <span className="text-ui-sm text-fg-subtle">今天已花 ${spent.toFixed(2)}</span>}
      </div>
    </Panel>
  )
}

export const budget: Plugin = {
  id: 'budget',
  activate(ctx) {
    ctx.slot({ slot: 'settings', id: 'budget', component: BudgetPanel as React.ComponentType<Record<string, unknown>>, order: 10 })
  },
}
