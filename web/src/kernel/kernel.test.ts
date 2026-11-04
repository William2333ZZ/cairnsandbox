// Kernel behaviour the plugins rely on: dependency order, skipping on missing requirements, disposal, services, events.
import { describe, expect, it } from 'vitest'
import { activate, deactivate, registry, type Plugin } from './index'

const Comp = () => null

describe('kernel', () => {
  it('activates in dependency order and skips plugins whose requirement is missing', async () => {
    const order: string[] = []
    const a: Plugin = { id: 'a', activate: () => { order.push('a') } }
    const b: Plugin = { id: 'b', requires: ['a'], activate: () => { order.push('b') } }
    const c: Plugin = { id: 'c', requires: ['zzz'], activate: () => { order.push('c') } }
    await activate([b, a, c])
    expect(order).toEqual(['a', 'b'])
    expect(registry.active.has('c')).toBe(false)
    deactivate('b'); deactivate('a')
  })

  it('removes every contribution when a plugin is deactivated', async () => {
    const before = { nav: registry.nav.length, routes: registry.routes.length, rails: registry.rails.length }
    const p: Plugin = { id: 'p', activate: ctx => { ctx.nav({ id: 'p', label: 'P', href: '#/p' }); ctx.route({ id: 'p', pattern: /^#\/p$/, component: Comp }); ctx.rail({ id: 'pr', label: 'R', component: Comp }); ctx.service('svc', { x: 1 }) } }
    await activate([p])
    expect(registry.nav.length).toBe(before.nav + 1); expect(registry.routes.length).toBe(before.routes + 1); expect(registry.rails.length).toBe(before.rails + 1); expect(registry.services.has('svc')).toBe(true)
    deactivate('p')
    expect(registry.nav.length).toBe(before.nav); expect(registry.routes.length).toBe(before.routes); expect(registry.rails.length).toBe(before.rails); expect(registry.services.has('svc')).toBe(false)
  })

  it('refuses a duplicate service and delivers events to subscribers and the wildcard', async () => {
    const seen: unknown[] = []
    const s1: Plugin = { id: 's1', activate: ctx => { ctx.service('dup', 1); ctx.on('ping', d => seen.push(['ping', d])); ctx.on('*', d => seen.push(['*', d])) } }
    const s2: Plugin = { id: 's2', requires: ['s1'], activate: ctx => { expect(() => ctx.service('dup', 2)).toThrow(); ctx.emit('ping', 42) } }
    await activate([s1, s2])
    expect(seen).toEqual([['ping', 42], ['*', { event: 'ping', data: 42 }]])
    deactivate('s2'); deactivate('s1')
  })

  it('rolls back a plugin whose activate throws', async () => {
    const before = registry.nav.length
    const bad: Plugin = { id: 'bad', activate: ctx => { ctx.nav({ id: 'bad', label: 'B', href: '#/b' }); throw new Error('boom') } }
    await activate([bad])
    expect(registry.active.has('bad')).toBe(false); expect(registry.nav.length).toBe(before)
  })

  it('detects plugin cycles', async () => {
    const x: Plugin = { id: 'x', requires: ['y'], activate: () => {} }
    const y: Plugin = { id: 'y', requires: ['x'], activate: () => {} }
    await expect(activate([x, y])).rejects.toThrow(/cycle/)
  })
})
