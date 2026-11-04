// Zustand store: one slice per service. Pages read from here and call the actions; nothing else fetches.
import { create } from 'zustand'
import { GET, POST, PUT, DEL, subscribe } from '../api/client'
import type { Approval, Attention, Batch, BatchRow, Health, Policy, Sandbox, TaskSetRow, Thread, Ticket, TicketRow, Version, Decision } from '../api/types'

type Loading<T> = { data: T | null; error: string | null; at: number }
const empty = <T,>(): Loading<T> => ({ data: null, error: null, at: 0 })

export interface Store {
  theme: 'light' | 'dark' | 'system'; setTheme: (t: Store['theme']) => void
  sidebar: 'open' | 'collapsed'; toggleSidebar: () => void
  rightWidth: number; setRightWidth: (w: number) => void; rightOpen: boolean; setRightOpen: (v: boolean) => void
  health: Loading<Health>; attention: Loading<Attention>; sandboxes: Loading<Sandbox[]>
  tickets: Loading<TicketRow[]>; ticket: Record<string, Loading<Ticket>>; threads: Loading<Thread[]>; thread: Record<string, Loading<Thread>>
  decisions: Loading<Decision[]>; policy: Loading<Policy>; approvals: Loading<Approval[]>
  tasksets: Loading<TaskSetRow[]>; versions: Loading<Version[]>; batches: Loading<BatchRow[]>; batch: Record<string, Loading<Batch>>
  live: boolean; lastEvent: { type: string; data: unknown } | null
  load: {
    health(): Promise<void>; attention(): Promise<void>; sandboxes(): Promise<void>; tickets(state?: string): Promise<void>; ticket(id: string): Promise<void>
    threads(): Promise<void>; thread(id: string): Promise<void>; decisions(): Promise<void>; policy(): Promise<void>; approvals(): Promise<void>
    tasksets(): Promise<void>; versions(): Promise<void>; batches(): Promise<void>; batch(id: string): Promise<void>
  }
  act: {
    request(text: string, budget_usd?: number): Promise<{ trigger: string; thread: string }>
    stopTicket(id: string): Promise<void>; retryTicket(id: string): Promise<void>; replyTicket(id: string, text: string): Promise<void>
    decide(approval: string, decision: 'approve' | 'deny', reason?: string, override?: boolean): Promise<Approval>
    reevaluate(trigger: string): Promise<void>; pause(paused: boolean): Promise<void>; savePolicy(p: Partial<Policy>): Promise<{ version: string; changed: boolean }>
    sandbox(id: string, op: 'pause' | 'resume' | 'delete' | 'fork' | 'pack', body?: unknown): Promise<unknown>
    createSandbox(spec: unknown): Promise<Sandbox>
  }
  startLive(): () => void
}

async function run<T>(set: (l: Loading<T> | ((prev: Loading<T>) => Loading<T>)) => void, fn: () => Promise<T>) {
  try { const data = await fn(); set({ data, error: null, at: Date.now() }) }
  catch (e) { set(prev => ({ ...prev, error: (e as Error).message, at: Date.now() })) }      // one failed poll keeps the page's data; the error shows as a banner
}

export const useStore = create<Store>((set, get) => {
  type L = Loading<unknown>; type Upd = L | ((prev: L) => L)
  const slice = <K extends keyof Store>(k: K) => (l: Upd) => set(s => ({ [k]: typeof l === 'function' ? l(s[k] as L) : l }) as Pick<Store, K>)
  const keyed = (k: 'ticket' | 'thread' | 'batch', id: string) => (l: Upd) => set(s => ({ [k]: { ...s[k], [id]: typeof l === 'function' ? l((s[k] as Record<string, L>)[id] ?? empty()) : l } }) as Pick<Store, typeof k>)
  const theme = ((): Store['theme'] => { try { return (localStorage.getItem('theme') as Store['theme']) || 'system' } catch { return 'system' } })()
  const rightWidth = ((): number => { try { return Number(localStorage.getItem('rightWidth')) || 0 } catch { return 0 } })()
  return {
    theme, setTheme: t => { try { localStorage.setItem('theme', t) } catch { /* ignore */ } set({ theme: t }) },
    sidebar: ((): Store['sidebar'] => { try { return (localStorage.getItem('sidebar') as Store['sidebar']) || 'collapsed' } catch { return 'collapsed' } })(),
    toggleSidebar: () => set(s => { const v = s.sidebar === 'open' ? 'collapsed' : 'open'; try { localStorage.setItem('sidebar', v) } catch { /* ignore */ } return { sidebar: v } }),
    rightWidth, setRightWidth: w => { try { localStorage.setItem('rightWidth', String(w)) } catch { /* ignore */ } set({ rightWidth: w }) },
    rightOpen: false, setRightOpen: v => set({ rightOpen: v }),
    health: empty(), attention: empty(), sandboxes: empty(), tickets: empty(), ticket: {}, threads: empty(), thread: {}, decisions: empty(), policy: empty(), approvals: empty(),
    tasksets: empty(), versions: empty(), batches: empty(), batch: {}, live: false, lastEvent: null,
    load: {
      health: () => run(slice('health'), () => GET<Health>('/health')),
      attention: () => run(slice('attention'), () => GET<Attention>('/intake/attention')),
      sandboxes: () => run(slice('sandboxes'), async () => (await GET<{ sandboxes: Sandbox[] }>('/sandboxes')).sandboxes),
      tickets: (state?: string) => run(slice('tickets'), async () => (await GET<{ tickets: TicketRow[] }>('/intake/tickets' + (state ? `?state=${state}` : ''))).tickets),
      ticket: (id: string) => run(keyed('ticket', id), () => GET<Ticket>(`/intake/tickets/${id}`)),
      threads: () => run(slice('threads'), async () => (await GET<{ threads: Thread[] }>('/intake/threads')).threads),
      thread: (id: string) => run(keyed('thread', id), () => GET<Thread>(`/intake/threads/${encodeURIComponent(id)}`)),
      decisions: () => run(slice('decisions'), async () => (await GET<{ decisions: Decision[] }>('/intake/decisions')).decisions),
      policy: () => run(slice('policy'), () => GET<Policy>('/intake/policy')),
      approvals: () => run(slice('approvals'), async () => (await GET<{ approvals: Approval[] }>('/intake/approvals?state=pending')).approvals),
      tasksets: () => run(slice('tasksets'), async () => (await GET<{ tasksets: TaskSetRow[] }>('/eval/tasksets')).tasksets),
      versions: () => run(slice('versions'), async () => (await GET<{ versions: Version[] }>('/eval/versions')).versions),
      batches: () => run(slice('batches'), async () => (await GET<{ batches: BatchRow[] }>('/eval/batches')).batches),
      batch: (id: string) => run(keyed('batch', id), () => GET<Batch>(`/eval/batches/${id}`)),
    },
    act: {
      request: async (text, budget_usd) => { const r = await POST<{ trigger: string; thread: string }>('/intake/requests', { text, budget_usd, actor: 'workbench' }); void get().load.attention(); return r },
      stopTicket: async id => { await POST(`/intake/tickets/${id}/stop`, { reason: 'panel' }); await get().load.ticket(id); void get().load.attention() },
      retryTicket: async id => { await POST(`/intake/tickets/${id}/retry`); await get().load.ticket(id) },
      replyTicket: async (id, text) => { await POST(`/intake/tickets/${id}/reply`, { text, actor: 'panel' }); await get().load.ticket(id) },
      decide: async (approval, decision, reason, override) => { const r = await POST<Approval>(`/intake/approvals/${approval}`, { decision, actor: 'panel', reason: reason || undefined, override: override || undefined }); void get().load.approvals(); void get().load.tickets(); void get().load.attention(); return r },
      reevaluate: async trigger => { await POST(`/intake/triggers/${trigger}/reevaluate`) },
      pause: async paused => { await POST('/intake/pause', { paused }); await get().load.attention() },
      savePolicy: async p => { const r = await PUT<{ version: string; changed: boolean }>('/intake/policy', p); await get().load.policy(); return r },
      sandbox: async (id, op, body) => {
        const r = op === 'delete' ? await DEL(`/sandboxes/${id}`) : await POST(`/sandboxes/${id}/${op}`, body ?? {})
        void get().load.sandboxes(); return r
      },
      createSandbox: async spec => { const r = await POST<Sandbox>('/sandboxes', spec); void get().load.sandboxes(); return r },
    },
    startLive: () => {
      const stop = subscribe(e => {
        set({ lastEvent: e })
        const t = e.type || ''
        if (t.startsWith('sandbox')) void get().load.sandboxes()
        if (t.startsWith('ticket') || t.startsWith('decision') || t.startsWith('trigger')) {
          void get().load.attention(); void get().load.tickets(); void get().load.approvals()
          for (const id of Object.keys(get().ticket)) void get().load.ticket(id)           // open detail pages follow the event too
        }
        if (t.startsWith('batch')) { void get().load.batches(); for (const id of Object.keys(get().batch)) void get().load.batch(id) }
        if (t === 'node') void get().load.health()
      }, open => set({ live: open }))                                                        // live only while the stream is actually connected
      return () => { set({ live: false }); stop() }
    },
  }
})

/** Poll a loader until SSE takes over; returns the cleanup. */
export function usePolling(fn: () => Promise<void>, ms: number) {
  return () => { let alive = true; const tick = async () => { if (!alive) return; await fn(); if (alive && !useStore.getState().live) setTimeout(tick, ms); else if (alive) setTimeout(tick, ms * 6) }; void tick(); return () => { alive = false } }
}
