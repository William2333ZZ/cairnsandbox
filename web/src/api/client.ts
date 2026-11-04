// HTTP to the control plane (same origin). The bearer token is kept in localStorage as a per-viewer convenience.
export class ApiError extends Error { constructor(public status: number, message: string) { super(message) } }

export function token(): string { try { return localStorage.getItem('cp_token') || '' } catch { return '' } }
export function setToken(t: string) { try { t ? localStorage.setItem('cp_token', t) : localStorage.removeItem('cp_token') } catch { /* private window */ } }

export function project(): string { try { return localStorage.getItem('cp_project') || '' } catch { return '' } }   // the console's current project (docs/008 控制台); empty = the token's default
export function setProject(p: string) { try { p ? localStorage.setItem('cp_project', p) : localStorage.removeItem('cp_project') } catch { /* ignore */ } window.dispatchEvent(new CustomEvent('cp-project')) }
export function agent(): string { try { return localStorage.getItem('agent') || 'main' } catch { return 'main' } }
export function setAgent(a: string) { try { a && a !== 'main' ? localStorage.setItem('agent', a) : localStorage.removeItem('agent') } catch { /* ignore */ } }
function headers(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra }
  const t = token(); if (t) h.Authorization = 'Bearer ' + t
  const a = agent(); if (a !== 'main') h['X-Agent'] = a                                  // which of the user's agents this request is about (docs/001 多个 agent)
  const p = project(); if (p) h['X-Project'] = p                                          // which project a create / quota / billing call is about (CONTRACT §1 租户)
  return h
}

export async function api<T = unknown>(method: string, path: string, body?: unknown, opts: { raw?: boolean; signal?: AbortSignal } = {}): Promise<T> {
  const init: RequestInit = { method, headers: headers(body !== undefined && !(body instanceof Uint8Array) ? { 'Content-Type': 'application/json' } : {}), signal: opts.signal }
  if (body !== undefined) init.body = body instanceof Uint8Array ? (body as unknown as BodyInit) : JSON.stringify(body)
  const r = await fetch(path, init)
  if (r.status === 401) window.dispatchEvent(new CustomEvent('cp-unauthorized'))      // the shell shows the login dialog (docs/008 设置: 登录与令牌)
  if (!r.ok) {
    let msg = r.statusText
    try { const j = await r.json(); msg = j.error || JSON.stringify(j) } catch { /* not json */ }
    throw new ApiError(r.status, msg)
  }
  if (opts.raw) return (await r.arrayBuffer()) as unknown as T
  const text = await r.text()
  return (text ? JSON.parse(text) : {}) as T
}
export const GET = <T,>(p: string, signal?: AbortSignal) => api<T>('GET', p, undefined, { signal })
export const POST = <T,>(p: string, b?: unknown) => api<T>('POST', p, b ?? {})
export const PUT = <T,>(p: string, b?: unknown) => api<T>('PUT', p, b ?? {})
export const DEL = <T,>(p: string) => api<T>('DELETE', p)

/** Streamed exec: one JSON object per line; yields {event, data} and the final {done,...}. */
export async function* execStream(sandbox: string, body: unknown, signal?: AbortSignal): AsyncGenerator<Record<string, unknown>> {
  const r = await fetch(`/sandboxes/${sandbox}/exec/stream`, { method: 'POST', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify(body), signal })
  if (!r.ok || !r.body) throw new ApiError(r.status, r.statusText)
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = ''
  for (;;) {
    const { value, done } = await reader.read(); if (done) break
    buf += dec.decode(value, { stream: true }); let i
    while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1); if (line) yield JSON.parse(line) }
  }
  if (buf.trim()) yield JSON.parse(buf)
}

/** Server-sent events from the control plane (`/events/stream`); falls back silently when the route is missing. */
export function subscribe(onEvent: (e: { type: string; data: unknown }) => void, onStatus?: (open: boolean) => void): () => void {
  let es: EventSource | null = null; let stopped = false; let retry = 1000
  const open = () => {
    if (stopped) return
    try { es = new EventSource('/events/stream' + (token() ? `?token=${encodeURIComponent(token())}` : '')) } catch { return }
    es.onopen = () => { retry = 1000; onStatus?.(true) }
    es.onmessage = m => { try { onEvent(JSON.parse(m.data)) } catch { /* ignore */ } }
    es.onerror = () => { es?.close(); es = null; onStatus?.(false); if (!stopped) setTimeout(open, retry = Math.min(retry * 2, 30000)) }
  }
  open()
  return () => { stopped = true; es?.close(); onStatus?.(false) }
}

/** Check a token with the control plane before keeping it. */
export async function login(t: string): Promise<boolean> {
  const r = await fetch('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: t }) })
  if (r.ok) { setToken(t); try { const j = await r.json(); localStorage.setItem('cp_user', String(j.user || '')) } catch { /* ignore */ } }
  return r.ok
}
export interface Me { user: string | null; admin: boolean; auth: boolean }
export const me = () => GET<Me>('/me')
