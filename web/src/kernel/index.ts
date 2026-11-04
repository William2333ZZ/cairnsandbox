// The plugin kernel (docs/008-workbench.md, D12/D13): a small registry the DSH way. Every page, nav entry, right-rail
// panel, command and service is contributed by a plugin; the shell only renders what the registry holds.
// Plugins are plain modules with a manifest; the kernel resolves `requires`, activates in dependency order, and lets a
// plugin be deactivated (its contributions are removed by the disposers it returned).
import type { ComponentType } from 'react'
import { useSyncExternalStore } from 'react'

export interface NavEntry { id: string; label: string; href: string; order?: number; group?: 'main' | 'bottom'; badge?: (state: unknown) => number | string | null }   // badge is a selector over the store state (subscribed by the shell)
export interface Route { id: string; pattern: RegExp; component: ComponentType<{ params: string[] }>; nav?: string }
export interface RailTab { id: string; label: string; component: ComponentType<{ args: Record<string, string> }>; order?: number }
export interface Command { id: string; label: string; run: (arg?: unknown) => void | Promise<void>; keys?: string }
export interface SlotItem { slot: string; id: string; component: ComponentType<Record<string, unknown>>; order?: number }

export interface PluginContext {
  nav(e: NavEntry): () => void
  route(r: Route): () => void
  rail(t: RailTab): () => void
  command(c: Command): () => void
  slot(s: SlotItem): () => void
  service<T>(name: string, impl: T): () => void
  use<T>(name: string): T
  on(event: string, fn: (data: unknown) => void): () => void
  emit(event: string, data?: unknown): void
}
export interface Plugin { id: string; requires?: string[]; activate(ctx: PluginContext): void | (() => void) | Promise<void | (() => void)> }

type Listener = () => void
class Registry {
  nav: NavEntry[] = []; routes: Route[] = []; rails: RailTab[] = []; commands: Command[] = []; slots: SlotItem[] = []
  services = new Map<string, unknown>(); handlers = new Map<string, Set<(d: unknown) => void>>()
  active = new Map<string, (() => void) | void>(); version = 0; listeners = new Set<Listener>()
  bump() { this.version++; this.listeners.forEach(l => l()) }
  add<T extends { id: string }>(list: T[], item: T) { list.push(item); this.bump(); return () => { const i = list.indexOf(item); if (i >= 0) list.splice(i, 1); this.bump() } }
}
const reg = new Registry()

function ctxFor(pluginId: string): PluginContext {
  return {
    nav: e => reg.add(reg.nav, e), route: r => reg.add(reg.routes, r), rail: t => reg.add(reg.rails, t), command: c => reg.add(reg.commands, c), slot: s => reg.add(reg.slots, s),
    service: (name, impl) => { if (reg.services.has(name)) throw new Error(`${pluginId}: service ${name} already provided`); reg.services.set(name, impl); reg.bump(); return () => { reg.services.delete(name); reg.bump() } },
    use: <T,>(name: string) => { if (!reg.services.has(name)) throw new Error(`${pluginId}: service ${name} missing (declare it in requires)`); return reg.services.get(name) as T },
    on: (event, fn) => { const s = reg.handlers.get(event) ?? new Set(); s.add(fn); reg.handlers.set(event, s); return () => s.delete(fn) },
    emit: (event, data) => { reg.handlers.get(event)?.forEach(fn => fn(data)); reg.handlers.get('*')?.forEach(fn => fn({ event, data })) },
  }
}

/** Activate plugins in dependency order; a plugin whose requirement is missing is skipped with a console warning. */
export async function activate(plugins: Plugin[]) {
  const byId = new Map(plugins.map(p => [p.id, p])); const done = new Set<string>(); const disposers: (() => void)[] = []
  const go = async (p: Plugin, chain: string[]): Promise<boolean> => {
    if (reg.active.has(p.id)) return true
    if (chain.includes(p.id)) throw new Error(`plugin cycle: ${[...chain, p.id].join(' -> ')}`)
    for (const r of p.requires ?? []) { const q = byId.get(r); if (!q || !(await go(q, [...chain, p.id]))) { console.warn(`plugin ${p.id} skipped: requires ${r}`); return false } }
    const ctx = ctxFor(p.id); const subs: (() => void)[] = []
    const wrapped: PluginContext = { ...ctx, nav: e => { const d = ctx.nav(e); subs.push(d); return d }, route: r => { const d = ctx.route(r); subs.push(d); return d }, rail: t => { const d = ctx.rail(t); subs.push(d); return d },
      command: c => { const d = ctx.command(c); subs.push(d); return d }, slot: s => { const d = ctx.slot(s); subs.push(d); return d }, service: (n, i) => { const d = ctx.service(n, i); subs.push(d); return d }, on: (e, f) => { const d = ctx.on(e, f); subs.push(d); return d } }
    let ret: void | (() => void)
    try { ret = await p.activate(wrapped) }
    catch (e) { subs.reverse().forEach(d => d()); console.warn(`plugin ${p.id} failed to activate:`, e); return false }   // nothing of it stays registered
    const dispose = () => { if (typeof ret === 'function') ret(); subs.reverse().forEach(d => d()); reg.active.delete(p.id); reg.bump() }
    reg.active.set(p.id, dispose); disposers.push(dispose)
    done.add(p.id); return true
  }
  for (const p of plugins) await go(p, [])
  return () => disposers.forEach(d => d())
}
export function deactivate(id: string) { reg.active.get(id)?.() }

/** React binding: re-render when the registry changes. */
export function useRegistry() { return useSyncExternalStore(l => { reg.listeners.add(l); return () => reg.listeners.delete(l) }, () => reg.version) }
export const registry = reg
export function emit(event: string, data?: unknown) { ctxFor('kernel').emit(event, data) }
export function service<T>(name: string): T { return ctxFor('kernel').use<T>(name) }
