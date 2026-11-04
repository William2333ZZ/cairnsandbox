'use client'
// 无业务的基础组件。颜色只用语义令牌(globals.css),状态色只用于状态。
import { useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from 'react'
import { useI18n, word } from '@/lib/i18n'

export const cx = (...a: (string | false | null | undefined)[]) => a.filter(Boolean).join(' ')

type BtnKind = 'primary' | 'secondary' | 'danger' | 'ghost'
export function Button({ kind = 'secondary', size = 'md', className, ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { kind?: BtnKind; size?: 'sm' | 'md' }) {
  const k = { primary: 'border border-brand bg-brand text-fg-inverse hover:opacity-85', secondary: 'border border-border bg-transparent text-fg hover:bg-hover', danger: 'border border-border text-destructive hover:bg-destructive-surface', ghost: 'border border-transparent hover:bg-hover' }[kind]
  return <button {...p} className={cx('inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-lg transition-colors disabled:cursor-not-allowed disabled:opacity-50', size === 'sm' ? 'h-7 px-2 text-ui-sm' : 'h-8 px-3 text-ui-base', k, className)} />
}

export type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info'
export function Badge({ tone = 'neutral', children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  const t = { neutral: 'border-border text-fg-subtle', success: 'border-success text-success bg-success-surface', warning: 'border-warning text-warning bg-warning-surface', danger: 'border-destructive text-destructive bg-destructive-surface', info: 'border-link text-link bg-accent' }[tone]
  return <span className={cx('inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-1.5 text-ui-xs leading-5', t, className)}>{children}</span>
}

const STATUS_TONE: Record<string, Tone> = { running: 'success', paused: 'warning', unreachable: 'danger', failed: 'danger', creating: 'neutral' }
export function StatusBadge({ status }: { status: string }) {
  const { t } = useI18n()
  const tone = STATUS_TONE[status] ?? 'neutral'
  const dot = { success: 'bg-success', warning: 'bg-warning', danger: 'bg-destructive', neutral: 'bg-fg-subtlest', info: 'bg-link' }[tone]
  return <Badge tone={tone}><span className={cx('h-1.5 w-1.5 rounded-full', dot)} />{(() => { const k = `status.${status}`; const v = t(k); return v === k ? status : v })()}</Badge>
}

export function Panel({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  const { lang } = useI18n()
  return (
    <section className={cx('rounded-xl border border-border bg-panel', className)}>
      {(title || actions) && <header className="flex items-center gap-2 border-b border-border px-3 py-2"><h2 className="text-ui-caption font-medium text-fg-subtle">{word(lang, title) as ReactNode}</h2><div className="flex-1" />{actions}</header>}
      <div className="p-3">{children}</div>
    </section>
  )
}

export function Tile({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className={cx('rounded-xl border border-border px-3.5 py-3', tone === 'warning' ? 'bg-warning-surface' : 'bg-panel')}>
      <div className="text-ui-caption text-fg-subtle">{label}</div>
      <div className="text-ui-xl font-medium">{value}</div>
      {sub && <div className="text-ui-sm text-fg-subtle">{sub}</div>}
    </div>
  )
}

export type Col<T> = { h: ReactNode; cell: (row: T) => ReactNode; w?: string }
export function Table<T>({ cols, rows, keyOf, empty, onRow }: { cols: Col<T>[]; rows?: T[] | null; keyOf: (r: T) => string; empty?: ReactNode; onRow?: (r: T) => void }) {
  const { lang } = useI18n()
  if (!rows) return <p className="text-ui-sm text-fg-subtle">…</p>
  if (rows.length === 0) return <div className="py-6 text-center text-ui-base text-fg-subtle">{empty}</div>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-ui-base">
        <thead><tr className="border-b border-border text-left text-ui-caption text-fg-subtle">{cols.map((c, i) => <th key={i} className="whitespace-nowrap px-2 py-1.5 text-ui-sm font-medium" style={c.w ? { width: c.w } : undefined}>{word(lang, c.h) as ReactNode}</th>)}</tr></thead>
        <tbody>{rows.map(r => <tr key={keyOf(r)} onClick={onRow ? () => onRow(r) : undefined} className={cx('border-b border-border last:border-0', onRow && 'cursor-pointer hover:bg-bg-alt')}>{cols.map((c, i) => <td key={i} className="px-2 py-1.5 align-middle [&_a]:whitespace-nowrap">{c.cell(r)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}

export function ErrorLine({ children }: { children: ReactNode }) {
  return <p role="alert" className="rounded-lg border border-destructive bg-destructive-surface px-3 py-1.5 text-ui-sm text-destructive">{children}</p>
}

export function EmptyState({ title, body, children }: { title: string; body?: string; children?: ReactNode }) {
  return <div className="mx-auto max-w-xl py-10 text-center"><h2 className="text-ui-lg font-medium">{title}</h2>{body && <p className="mt-1 text-ui-base text-fg-subtle">{body}</p>}<div className="mt-4 text-left">{children}</div></div>
}

export function CodeBlock({ code, label }: { code: string; label?: string }) {
  const { t } = useI18n(); const [done, setDone] = useState(false)
  return (
    <div className="rounded-lg border border-border bg-surface">
      <div className="flex items-center gap-2 px-3 pt-1.5 text-ui-caption text-fg-subtle">{label}<span className="flex-1" /><button className="hover:text-fg" onClick={() => { void navigator.clipboard?.writeText(code); setDone(true); setTimeout(() => setDone(false), 1500) }}>{done ? t('common.copied') : t('common.copy')}</button></div>
      <pre className="code overflow-x-auto px-3 pb-2 pt-1">{code}</pre>
    </div>
  )
}

export const inputCls = 'h-8 rounded-lg border border-border bg-input px-2.5 text-ui-base placeholder:text-fg-subtlest focus:border-border-hover'
export function Input(p: InputHTMLAttributes<HTMLInputElement>) { return <input {...p} className={cx(inputCls, p.className)} /> }
export function Select(p: SelectHTMLAttributes<HTMLSelectElement>) { return <select {...p} className={cx(inputCls, p.className)} /> }

export function Pill({ active, children, onClick }: { active?: boolean; children: ReactNode; onClick: () => void }) {
  return <button onClick={onClick} className={cx('rounded-full border px-2.5 py-0.5 text-ui-sm', active ? 'border-brand bg-brand text-fg-inverse' : 'border-border bg-panel hover:bg-hover')}>{children}</button>
}
