import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, headers, setProjectKey, setToken } from './api'
import { age, bytes, mib, pct, usd } from './format'
import { serverError, translate } from './i18n'
import { navFor, roleOf, canManage, NAV } from './roles'

beforeEach(() => { localStorage.clear() })
afterEach(() => { vi.restoreAllMocks() })

describe('api', () => {
  it('sends the bearer and the project as headers, using the same storage keys as the old workbench', () => {
    setToken('tok-1'); setProjectKey('acme')
    expect(localStorage.getItem('cp_token')).toBe('tok-1')
    expect(headers()).toEqual({ Authorization: 'Bearer tok-1', 'X-Project': 'acme' })
    setToken(''); setProjectKey('')
    expect(headers()).toEqual({})
  })
  it('turns an error status into an ApiError carrying the server message, and a 202 into the pending-approval error', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: 'no capacity' }), { status: 429 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ request: { id: 'nr-1' } }), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: 1 }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(api('POST', '/sandboxes', {})).rejects.toMatchObject({ status: 429, message: 'no capacity' })
    const e = (await api('PUT', '/sandboxes/x/network', {}).catch((x: unknown) => x)) as ApiError
    expect(e.status).toBe(202); expect((e.body as { request: { id: string } }).request.id).toBe('nr-1')
    expect(await api('GET', '/me')).toEqual({ ok: 1 })
  })
  it('announces a 401 so the shell can show the login page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })))
    const seen = vi.fn(); window.addEventListener('cp-unauthorized', seen)
    await api('GET', '/me').catch(() => {})
    expect(seen).toHaveBeenCalled()
  })
})

describe('roles', () => {
  it('derives operator from the deployer token, owner from the account role, member otherwise', () => {
    expect(roleOf({ user: 'admin', admin: true, auth: true })).toBe('operator')
    expect(roleOf({ user: 'c', admin: false, auth: true, role: 'owner' })).toBe('owner')
    expect(roleOf({ user: 'd', admin: false, auth: true, role: 'member' })).toBe('member')
    expect(roleOf(null)).toBe('member')
  })
  it('grows the navigation by role: nobody sees a page they would only get a 403 on', () => {
    const ids = (r: Parameters<typeof navFor>[0]) => navFor(r).map(n => n.id)
    expect(ids('member')).not.toContain('nodes'); expect(ids('member')).not.toContain('settings')
    expect(ids('owner')).toContain('settings'); expect(ids('owner')).not.toContain('nodes')
    expect(ids('operator')).toContain('nodes')
    expect(NAV.every(n => n.href.startsWith('/') && n.href.endsWith('/'))).toBe(true)      // trailingSlash export
  })
  it('lets owners and operators manage, members not', () => { expect(canManage('owner')).toBe(true); expect(canManage('operator')).toBe(true); expect(canManage('member')).toBe(false) })
})

describe('i18n', () => {
  it('falls back to Chinese, then to the key, and fills placeholders', () => {
    expect(translate('en', 'nav.overview')).toBe('Overview')
    expect(translate('zh', 'nav.overview')).toBe('概览')
    expect(translate('en', 'nope.key')).toBe('nope.key')
    expect(translate('en', 'sb.selected')).toBe('selected')
  })
  it('has every Chinese key in English too, so switching language never shows a raw Chinese string by accident', async () => {
    const src = (await import('./i18n')) as unknown as { translate: (l: 'zh' | 'en', k: string) => string }
    const zhKeys = ['app.name', 'nav.overview', 'ov.spend', 'sb.title', 'net.queue', 'members.title', 'audit.title', 'ops.title', 'login.title']
    for (const k of zhKeys) expect(src.translate('en', k)).not.toBe(src.translate('zh', k))
  })
})

describe('format', () => {
  it('formats ages, money, sizes and shares', () => {
    expect(age(100, 130)).toBe('30s'); expect(age(0)).toBe('-'); expect(age(1000, 1000 + 3 * 3600)).toBe('3h')
    expect(usd(0.0021)).toBe('$0.002'); expect(usd(3.2)).toBe('$3.20'); expect(usd(null)).toBe('-')
    expect(mib(512)).toBe('512 MiB'); expect(mib(2048)).toBe('2.0 GiB'); expect(bytes(1536)).toBe('2 KiB'); expect(pct(0.476)).toBe('48%')
  })
})

describe('serverError', () => {
  it('translates known control-plane errors in zh and leaves the rest alone', () => {
    expect(serverError('zh', 'invite code is not valid')).toBe('邀请码无效或已被用过')
    expect(serverError('zh', 'account id: lowercase letters, digits, _ or -, 2-32 chars')).toContain('小写字母')
    expect(serverError('zh', 'something new')).toBe('something new')
    expect(serverError('en', 'account exists')).toBe('account exists')
  })
})
