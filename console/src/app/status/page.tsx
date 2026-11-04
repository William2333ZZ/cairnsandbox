'use client'
// 公开状态页(D61):不需要登录,只读 GET /status,30 秒刷新。组件名是通用的,不含节点或主机信息。
import { useEffect, useState } from 'react'
import { useI18n } from '@/lib/i18n'
import { Badge } from '@/components/ui'

type Status = { status: 'operational' | 'degraded' | 'outage'; components: Record<string, string>; updated: number }
const TONE = { operational: 'success', degraded: 'warning', outage: 'danger' } as const

export default function PublicStatus() {
  const { t } = useI18n(); const [s, setS] = useState<Status | null>(null), [err, setErr] = useState(false)
  useEffect(() => {
    let live = true
    const load = async () => { try { const r = await fetch('/status', { cache: 'no-store' }); const j = (await r.json()) as Status; if (live) { setS(j); setErr(false) } } catch { if (live) setErr(true) } }
    void load(); const id = setInterval(load, 30000); return () => { live = false; clearInterval(id) }
  }, [])
  return (
    <main className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-ui-xl font-medium">{t('status.title')}</h1>
      {err && <p className="text-ui-base text-destructive">{t('status.cantreach')}</p>}
      {s && <>
        <div className="rounded-xl border border-border bg-panel p-4"><div className="text-ui-lg font-medium">{t(`status.overall.${s.status}`)}</div><div className="text-ui-sm text-fg-subtle">{new Date(s.updated * 1000).toLocaleString()}</div></div>
        <ul className="divide-y divide-border rounded-xl border border-border bg-panel">
          {Object.entries(s.components).sort().map(([k, v]) => <li key={k} className="flex items-center justify-between px-4 py-2 text-ui-base"><span>{t(`status.c.${k}`)}</span><Badge tone={TONE[v as keyof typeof TONE] ?? 'neutral'}>{t(`status.v.${v}`)}</Badge></li>)}
        </ul>
      </>}
    </main>
  )
}
