// 通知 (docs/001 第 5 阶段, docs/008): when the agent says something that needs the person — done, stuck, a question, a draft to
// approve, an update — the browser shows a notification, if the person allowed it. Nothing else notifies. The watermark of
// what was already shown lives in this browser only.
import { useState } from 'react'
import type { Plugin } from '../../kernel'
import { GET } from '../../api/client'
import type { ConvRow } from '../../api/types'
import { Button, Panel } from '../../ui'

const KINDS: Record<string, string> = { ready: '做完了', stuck: '卡住了', question: '要你回答', approval: '要你同意', update: '更新了' }
const key = 'notify.since'
const since = (): number => { try { return Number(localStorage.getItem(key) || 0) } catch { return 0 } }
const setSince = (t: number) => { try { localStorage.setItem(key, String(t)) } catch { /* ignore */ } }
const enabled = () => { try { return localStorage.getItem('notify') === '1' } catch { return false } }
const setEnabled = (v: boolean) => { try { localStorage.setItem('notify', v ? '1' : '0') } catch { /* ignore */ } }

async function poll() {
  if (!enabled() || typeof Notification === 'undefined' || Notification.permission !== 'granted') return
  const from = since() || Date.now() / 1000 - 60                                              // first run: only the last minute, never the whole history
  let rows: ConvRow[] = []
  try { rows = (await GET<{ items: ConvRow[] }>(`/intake/conversation?since=${from}&limit=50`)).items } catch { return }
  let last = from
  for (const r of rows) {
    last = Math.max(last, r.ts)
    if (r.kind !== 'say' || !r.say || !KINDS[r.say]) continue
    const n = new Notification(`${KINDS[r.say]}${r.short ? ` · ${r.short}` : ''}`, { body: r.text.replace(/ This is by an automated agent.*$/, '').slice(0, 180), tag: r.id })
    n.onclick = () => { window.focus(); if (r.ticket) location.hash = `#/w/${r.ticket}`; else location.hash = '#/w' }
  }
  setSince(last)
}

export function NotifySettings() {
  const [on, setOn] = useState(enabled()); const [perm, setPerm] = useState(typeof Notification === 'undefined' ? 'unsupported' : Notification.permission)
  const turnOn = async () => { if (typeof Notification === 'undefined') return; const p = await Notification.requestPermission(); setPerm(p); if (p === 'granted') { setEnabled(true); setOn(true); setSince(Date.now() / 1000) } }
  return (
    <Panel title="通知">
      <div className="flex flex-wrap items-center gap-3 text-ui-base">
        <span className="text-fg">它做完、卡住、要你回答或同意、更新了时,浏览器弹一条通知。</span>
        {perm === 'unsupported' ? <span className="text-ui-sm text-fg-subtle">这个浏览器不支持通知。</span>
          : perm === 'denied' ? <span className="text-ui-sm text-destructive">浏览器拒绝了通知;在地址栏的站点设置里改。</span>
          : on && perm === 'granted' ? <Button size="sm" onClick={() => { setEnabled(false); setOn(false) }}>关掉</Button>
          : <Button size="sm" kind="primary" onClick={turnOn}>打开</Button>}
      </div>
    </Panel>
  )
}

export const notify: Plugin = {
  id: 'notify',
  activate(ctx) {
    const timer = setInterval(() => void poll(), 8000); void poll()
    ctx.slot({ slot: 'settings', id: 'notify', component: NotifySettings as React.ComponentType<Record<string, unknown>>, order: 20 })
    return () => clearInterval(timer)
  },
}
