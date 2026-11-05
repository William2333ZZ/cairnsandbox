'use client'
import { useState, type ReactNode } from 'react'
import { POST } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { serverError, useI18n } from '@/lib/i18n'
import { Button, CodeBlock, ErrorLine, Input } from './ui'

export function AuthGate({ children }: { children: ReactNode }) {
  const { me, loading, login, error } = useAuth(); const { t, lang } = useI18n(); const [tok, setTok] = useState(''); const [bad, setBad] = useState(false)
  const [signup, setSignup] = useState(false), [f, setF] = useState({ code: '', account: '', user: '' }), [serr, setSerr] = useState(''), [issued, setIssued] = useState('')
  if (loading) return <div className="flex h-screen items-center justify-center text-ui-base text-fg-subtle">{t('common.loading')}</div>
  if (me && !issued) return <>{children}</>
  const join = async (e: React.FormEvent) => {
    e.preventDefault()
    try { const r = await POST<{ token: string }>('/signup', f); setIssued(r.token); setSerr('') } catch (x) { setSerr(serverError(lang, (x as Error).message)) }
  }
  if (issued) return (
    <div className="flex h-screen items-center justify-center bg-bg p-4"><div className="w-full max-w-sm space-y-3 rounded-2xl border border-border bg-panel p-5">
      <h1 className="text-ui-xl font-medium">{t('signup.done')}</h1><p className="text-ui-base text-fg-subtle">{t('signup.once')}</p>
      <CodeBlock label={t('login.token')} code={issued} />
      <Button kind="primary" className="w-full justify-center" onClick={async () => { const tk = issued; setIssued(''); setSignup(false); setF({ code: '', account: '', user: '' }); await login(tk) }}>{t('signup.enter')}</Button>
    </div></div>
  )
  return (
    <div className="flex h-screen items-center justify-center bg-bg p-4">
      {signup ? (
        <form className="w-full max-w-sm space-y-3 rounded-2xl border border-border bg-panel p-5" onSubmit={join}>
          <h1 className="text-ui-xl font-medium">{t('signup.title')}</h1><p className="text-ui-base text-fg-subtle">{t('signup.hint')}</p>
          <Input className="w-full" placeholder={t('signup.code')} value={f.code} onChange={e => setF({ ...f, code: e.target.value })} aria-label={t('signup.code')} />
          <Input className="w-full" placeholder={t('signup.account')} value={f.account} onChange={e => setF({ ...f, account: e.target.value.toLowerCase() })} aria-label={t('signup.account')} />
          <Input className="w-full" placeholder={t('signup.user')} value={f.user} onChange={e => setF({ ...f, user: e.target.value.toLowerCase() })} aria-label={t('signup.user')} />
          {serr && <ErrorLine>{serr}</ErrorLine>}
          <Button kind="primary" type="submit" className="w-full justify-center" disabled={!f.code.trim() || !f.account || !f.user}>{t('signup.go')}</Button>
          <button type="button" className="text-ui-sm text-link" onClick={() => setSignup(false)}>{t('signup.back')}</button>
        </form>
      ) : (
        <form className="w-full max-w-sm space-y-3 rounded-2xl border border-border bg-panel p-5" onSubmit={async e => { e.preventDefault(); setBad(!(await login(tok))) }}>
          <h1 className="text-ui-xl font-medium">{t('login.title')}</h1>
          <p className="text-ui-base text-fg-subtle">{t('login.hint')}</p>
          <Input autoFocus type="password" className="w-full" placeholder={t('login.token')} value={tok} onChange={e => { setTok(e.target.value); setBad(false) }} aria-label={t('login.token')} />
          {(bad || error) && <ErrorLine>{t('login.bad')}</ErrorLine>}
          <Button kind="primary" type="submit" className="w-full justify-center" disabled={!tok.trim()}>{t('login.go')}</Button>
          <button type="button" className="text-ui-sm text-link" onClick={() => setSignup(true)}>{t('signup.have')}</button>
        </form>
      )}
    </div>
  )
}
