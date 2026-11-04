// 控制面的 HTTP 客户端(CONTRACT §1)。令牌与项目存在 localStorage,键名与旧工作台相同(cp_token / cp_project):同一个来源下登录一次,两边通用。
export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) { super(message) }
}

const TOKEN = 'cp_token', PROJECT = 'cp_project', LANG = 'cp_lang'
const safe = <T,>(f: () => T, d: T): T => { try { return f() } catch { return d } }   // 隐私窗口里 localStorage 会抛
export const getToken = () => safe(() => localStorage.getItem(TOKEN) || '', '')
export const setToken = (t: string) => safe(() => { t ? localStorage.setItem(TOKEN, t) : localStorage.removeItem(TOKEN) }, undefined)
export const getProject = () => safe(() => localStorage.getItem(PROJECT) || '', '')
export const setProjectKey = (p: string) => safe(() => { p ? localStorage.setItem(PROJECT, p) : localStorage.removeItem(PROJECT) }, undefined)
export const getLang = () => safe(() => localStorage.getItem(LANG) || '', '')
export const setLangKey = (l: string) => safe(() => localStorage.setItem(LANG, l), undefined)

export function headers(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra }
  const t = getToken(); if (t) h.Authorization = 'Bearer ' + t
  const p = getProject(); if (p) h['X-Project'] = p                // 建 / 配额 / 账单类调用属于哪个项目(CONTRACT §1 租户)
  return h
}

export async function api<T = unknown>(method: string, path: string, body?: unknown, opts: { raw?: boolean; signal?: AbortSignal } = {}): Promise<T> {
  const isBytes = body instanceof Uint8Array
  const init: RequestInit = { method, headers: headers(body !== undefined && !isBytes ? { 'Content-Type': 'application/json' } : {}), signal: opts.signal }
  if (body !== undefined) init.body = isBytes ? (body as unknown as BodyInit) : JSON.stringify(body)
  const r = await fetch(path, init)
  if (r.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('cp-unauthorized'))
  if (!r.ok && r.status !== 202) {
    let msg = r.statusText, parsed: unknown
    try { parsed = await r.json(); msg = (parsed as { error?: string }).error || JSON.stringify(parsed) } catch { /* 不是 JSON */ }
    throw new ApiError(r.status, msg, parsed)
  }
  if (opts.raw) return (await r.arrayBuffer()) as unknown as T
  const text = await r.text(); const out = text ? JSON.parse(text) : {}
  if (r.status === 202) throw new ApiError(202, 'waiting for approval', out)     // 放宽出口,等 owner 批
  return out as T
}
export const GET = <T,>(p: string, signal?: AbortSignal) => api<T>('GET', p, undefined, { signal })
export const POST = <T,>(p: string, b?: unknown) => api<T>('POST', p, b ?? {})
export const PUT = <T,>(p: string, b?: unknown) => api<T>('PUT', p, b ?? {})
export const DEL = <T,>(p: string) => api<T>('DELETE', p)

/** 一行一个 JSON 的流(exec/stream):逐条 yield。 */
export async function* ndjson(path: string, body: unknown, signal?: AbortSignal): AsyncGenerator<Record<string, unknown>> {
  const r = await fetch(path, { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body), signal })
  if (!r.ok || !r.body) throw new ApiError(r.status, r.statusText)
  const reader = r.body.getReader(), dec = new TextDecoder(); let buf = ''
  for (;;) {
    const { done, value } = await reader.read(); if (done) break
    buf += dec.decode(value, { stream: true })
    let i: number
    while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (line) yield JSON.parse(line) }
  }
}

/** /events/stream 的 SSE:用 fetch 读,令牌走头而不是查询串(不进访问日志)。断了自己重连。 */
export function subscribeEvents(onEvent: (e: { type?: string; at?: number }) => void, signal: AbortSignal) {
  ;(async () => {
    while (!signal.aborted) {
      try {
        const r = await fetch('/events/stream', { headers: headers(), signal })
        if (!r.ok || !r.body) throw new Error(String(r.status))
        const reader = r.body.getReader(), dec = new TextDecoder(); let buf = ''
        for (;;) {
          const { done, value } = await reader.read(); if (done) break
          buf += dec.decode(value, { stream: true })
          let i: number
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2)
            const data = chunk.split('\n').find(l => l.startsWith('data:'))
            if (data) { try { onEvent(JSON.parse(data.slice(5).trim())) } catch { /* keepalive 或半条 */ } }
          }
        }
      } catch { /* 断线 */ }
      await new Promise(res => setTimeout(res, 3000))
    }
  })()
}

export const sandboxPath = (id: string, sub = '') => `/sandboxes/${encodeURIComponent(id)}${sub}`
