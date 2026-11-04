// 接入 (docs/001 第 2 阶段, docs/008): where the agent is connected — the code host (Gitea or GitHub) and the workbench — with
// the bot identity, the repository, the read cursor, errors; pause and resume. New connections are a deployment setting
// (the token file), so this page lists and controls, it does not create.
import { useEffect, useState } from 'react'
import type { Plugin } from '../../kernel'
import { GET, POST } from '../../api/client'
import { Badge, ErrorLine, age, cx } from '../../ui'

interface Connector { kind: string; title: string; url?: string; repo?: string; bot?: string; enabled: boolean; readable?: boolean; paused?: boolean; cursor?: { polled?: number; last_n?: number } | null; error?: string | null }

function ConnectorsPage() {
  const [rows, setRows] = useState<Connector[] | null>(null); const [err, setErr] = useState('')
  const load = async () => { try { setRows((await GET<{ connectors: Connector[] }>('/intake/connectors')).connectors); setErr('') } catch (e) { setErr((e as Error).message) } }
  useEffect(() => { void load(); const i = setInterval(() => void load(), 10000); return () => clearInterval(i) }, [])
  const pause = async (c: Connector, paused: boolean) => { try { setRows((await POST<{ connectors: Connector[] }>(`/intake/connectors/${c.kind}/pause`, { paused })).connectors) } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-6 py-6">
      <div><h1 className="text-ui-xl font-medium text-fg">接入</h1><p className="mt-1 text-ui-base text-fg-subtle">它以自己的账号出现在这些地方;接入的凭据放在部署机的密钥目录,从不进沙箱。</p></div>
      {err && <ErrorLine>{err}</ErrorLine>}
      <div className="flex flex-col gap-2">
        {rows?.map(c => (
          <div key={c.kind} className={cx('flex items-center gap-3 rounded-xl border border-border bg-panel px-4 py-3', c.paused && 'opacity-60')}>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-ui-base text-fg"><span className="font-medium">{c.title}</span>{c.repo && <span className="font-mono text-ui-sm text-fg-subtle">{c.repo}</span>}{c.bot && <span className="text-ui-sm text-fg-subtle">账号 {c.bot}</span>}</div>
              <div className="mt-0.5 flex flex-wrap gap-3 text-ui-sm text-fg-subtle">
                {c.url && <a href={c.url} target="_blank" rel="noreferrer">{c.url}</a>}
                {c.kind !== 'workbench' && (c.enabled ? <Badge tone="success">可读可写</Badge> : c.readable ? <Badge tone="warning">只读(没有令牌)</Badge> : <Badge tone="neutral">未配置</Badge>)}
                {c.cursor?.polled ? <span>上次读取 {age(c.cursor.polled)}{c.cursor.last_n ? ` · ${c.cursor.last_n} 条` : ''}</span> : null}
                {c.paused && <span>已暂停</span>}
                {c.error && <span className="text-destructive">{c.error}</span>}
              </div>
            </div>
            {c.kind !== 'workbench' && c.enabled && <button onClick={() => pause(c, !c.paused)} className="h-7 rounded-md border border-border px-2 text-ui-sm text-fg hover:bg-hover">{c.paused ? '恢复' : '暂停'}</button>}
          </div>
        ))}
      </div>
      <p className="text-ui-sm text-fg-subtlest">要接另一个代码托管:在部署机上设 CODE_HOST=github、GITHUB_REPO、GITHUB_TOKEN_FILE,重启 Agent 服务(docs/010)。</p>
    </div>
  )
}

export const connectors: Plugin = {
  id: 'connectors',
  activate(ctx) {
    ctx.nav({ id: 'connectors', label: '接入', href: '#/connectors', order: 6 })
    ctx.route({ id: 'connectors', pattern: /^#\/connectors$/, component: ConnectorsPage, nav: 'connectors' })
  },
}
