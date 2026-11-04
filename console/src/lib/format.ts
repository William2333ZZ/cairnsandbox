export const age = (ts?: number | null, now = Date.now() / 1000) => {
  if (!ts) return '-'
  const s = Math.max(0, Math.round(now - ts))
  if (s < 90) return `${s}s`
  if (s < 5400) return `${Math.round(s / 60)}m`
  if (s < 129600) return `${Math.round(s / 3600)}h`
  return `${Math.round(s / 86400)}d`
}
export const when = (ts?: number | null) => ts ? new Date(ts * 1000).toLocaleString() : '-'
export const usd = (v?: number | null) => v == null ? '-' : `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`
export const mib = (v?: number | null) => v == null ? '-' : v >= 1024 ? `${(v / 1024).toFixed(1)} GiB` : `${Math.round(v * 10) / 10} MiB`
export const bytes = (n?: number | null) => n == null ? '-' : n >= 1048576 ? `${(n / 1048576).toFixed(1)} MiB` : n >= 1024 ? `${Math.round(n / 1024)} KiB` : `${n} B`
export const pct = (v: number) => `${Math.round(v * 100)}%`
