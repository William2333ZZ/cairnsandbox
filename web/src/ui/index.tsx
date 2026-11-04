// Basic components with no business knowledge. Colors and sizes only through DESIGN.md tokens.
import React from 'react'

export const cx = (...a: (string | false | null | undefined)[]) => a.filter(Boolean).join(' ')

export function Button({ kind = 'default', size = 'md', className, ...p }: React.ButtonHTMLAttributes<HTMLButtonElement> & { kind?: 'default' | 'primary' | 'danger' | 'ghost'; size?: 'sm' | 'md' }) {
  const base = 'inline-flex items-center justify-center gap-1.5 rounded-lg border transition-colors disabled:opacity-50 disabled:cursor-default focus:outline-none'
  const k = { default: 'bg-transparent border-border hover:bg-hover text-fg', primary: 'bg-brand border-brand text-fg-inverse hover:opacity-85', danger: 'bg-transparent border-border text-destructive hover:bg-destructive-surface', ghost: 'border-transparent hover:bg-hover text-fg-subtle hover:text-fg' }[kind]
  const s = size === 'sm' ? 'h-7 px-2 text-ui-sm' : 'h-8 px-3 text-ui-base'
  return <button className={cx(base, k, s, className)} {...p} />
}

export function Input(p: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...p} className={cx('h-8 rounded-lg border border-border bg-input px-2.5 text-ui-base placeholder:text-fg-subtlest focus:border-border-hover', p.className)} />
}
export function TextArea(p: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...p} className={cx('w-full rounded-lg border border-border bg-input px-2.5 py-1.5 text-ui-base placeholder:text-fg-subtlest focus:border-border-hover', p.className)} />
}
export function Select(p: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...p} className={cx('h-8 rounded-lg border border-border bg-input px-2 text-ui-base', p.className)} />
}

export type Tone = 'neutral' | 'success' | 'warning' | 'destructive' | 'waiting' | 'brand'
const toneCls: Record<Tone, string> = {
  neutral: 'border-border text-fg-subtle', success: 'border-success text-success bg-success-surface', warning: 'border-warning text-warning bg-warning-surface',
  destructive: 'border-destructive text-destructive bg-destructive-surface', waiting: 'border-waiting text-waiting bg-waiting-surface', brand: 'border-brand text-brand bg-accent',
}
export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: React.ReactNode; title?: string }) {
  return <span title={title} className={cx('inline-block whitespace-nowrap rounded-full border px-1.5 text-ui-xs leading-5', toneCls[tone])}>{children}</span>
}

export function Panel({ title, actions, children, className }: { title?: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={cx('rounded-lg border border-border bg-panel', className)}>
      {(title || actions) && <header className="flex items-center gap-2 border-b border-border px-3 py-1.5"><h2 className="text-ui-caption font-medium text-fg-subtle">{title}</h2><div className="flex-1" />{actions}</header>}
      <div className="p-3">{children}</div>
    </section>
  )
}

export function KV({ rows }: { rows: [React.ReactNode, React.ReactNode][] }) {
  return <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-ui-base">{rows.map(([k, v], i) => <React.Fragment key={i}><dt className="text-fg-subtle">{k}</dt><dd className="min-w-0 break-words">{v}</dd></React.Fragment>)}</dl>
}

export function Table<T>({ cols, rows, onRow, empty = '没有内容', keyOf, loading }: { cols: { h: React.ReactNode; cell: (r: T) => React.ReactNode; w?: string; wrap?: boolean }[]; rows: T[] | null | undefined; onRow?: (r: T) => void; empty?: string; keyOf?: (r: T, i: number) => string; loading?: boolean }) {
  if (rows == null || loading) return <p className="py-3 text-ui-base text-fg-subtlest">加载中</p>
  if (!rows.length) return <p className="py-3 text-ui-base text-fg-subtle">{empty}</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-ui-base">
        <thead><tr>{cols.map((c, i) => <th key={i} style={c.w ? { width: c.w } : undefined} className="whitespace-nowrap border-b border-border px-2 py-1 text-left text-ui-sm font-medium text-fg-subtle">{c.h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={keyOf ? keyOf(r, i) : i} onClick={onRow ? e => { if ((e.target as HTMLElement).closest('a,button,input,select,textarea')) return; onRow(r) } : undefined} className={cx('border-b border-border', onRow && 'cursor-pointer hover:bg-bg-alt')}>
            {cols.map((c, j) => <td key={j} className={cx('px-2 py-1 align-top', c.wrap ? 'break-words' : 'whitespace-nowrap')}>{c.cell(r)}</td>)}
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

export function Empty({ children }: { children: React.ReactNode }) { return <p className="py-6 text-center text-ui-base text-fg-subtle">{children}</p> }
export function ErrorLine({ children }: { children: React.ReactNode }) { return <p className="rounded border border-destructive bg-destructive-surface px-2 py-1 text-ui-base text-destructive">{children}</p> }

export function Confirm({ text, onYes, children }: { text: string; onYes: () => void; children: React.ReactNode }) {
  return <Button kind="danger" size="sm" onClick={() => { if (window.confirm(text)) onYes() }}>{children}</Button>
}

export function ts(v?: number | string | null): number | null { if (v == null) return null; if (typeof v === 'number') return v > 1e12 ? v : v * 1000; const t = Date.parse(v); return isNaN(t) ? null : t }
export function age(v?: number | string | null): string { const t = ts(v); if (!t) return '-'; const s = Math.max(0, (Date.now() - t) / 1000); return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : s < 86400 ? `${(s / 3600).toFixed(1)}h` : `${(s / 86400).toFixed(1)}d` }
export function when(v?: number | string | null): string { const t = ts(v); return t ? new Date(t).toLocaleString() : '-' }
export function num(v: unknown, d = 3): string { return v == null || isNaN(Number(v)) ? '-' : Number(v).toFixed(d) }
export function usd(v: unknown): string { return v == null || isNaN(Number(v)) ? '-' : `$${Number(v).toFixed(3)}` }
