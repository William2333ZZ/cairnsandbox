'use client'
// 一个很小的折线图(SVG,无依赖):单条序列、自适应纵轴、可选上限线。颜色用令牌,深浅色自动跟随。
export type Point = { t: number; v: number | null }

export function LineChart({ title, points, unit, max, fmt }: { title: string; points: Point[]; unit?: string; max?: number; fmt?: (v: number) => string }) {
  const W = 560, H = 120, PAD = 4
  const ok = points.filter((p): p is { t: number; v: number } => p.v !== null)
  const f = fmt ?? ((v: number) => `${Math.round(v * 10) / 10}${unit ?? ''}`)
  if (ok.length < 2) return <div className="rounded-xl border border-border bg-panel p-3"><div className="text-ui-sm text-fg-subtle">{title}</div><div className="py-6 text-center text-ui-sm text-fg-subtle">…</div></div>
  const t0 = ok[0].t, t1 = ok[ok.length - 1].t, top = Math.max(max ?? 0, ...ok.map(p => p.v), 1e-9) * 1.1
  const x = (t: number) => PAD + ((t - t0) / Math.max(t1 - t0, 1)) * (W - 2 * PAD), y = (v: number) => H - PAD - (v / top) * (H - 2 * PAD)
  const line = ok.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')
  const last = ok[ok.length - 1].v
  return (
    <div className="rounded-xl border border-border bg-panel p-3">
      <div className="flex items-baseline justify-between"><span className="text-ui-sm text-fg-subtle">{title}</span><span className="text-ui-lg font-medium">{f(last)}</span></div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${f(last)}`} className="mt-1 h-28 w-full">
        {max !== undefined && <line x1={PAD} x2={W - PAD} y1={y(max)} y2={y(max)} stroke="var(--c-fg-subtlest)" strokeDasharray="4 3" />}
        <line x1={PAD} x2={W - PAD} y1={H - PAD} y2={H - PAD} stroke="var(--c-border)" />
        <path d={`${line} L${x(t1).toFixed(1)},${H - PAD} L${x(t0).toFixed(1)},${H - PAD} Z`} fill="var(--c-link)" opacity="0.1" />
        <path d={line} fill="none" stroke="var(--c-link)" strokeWidth="1.5" strokeLinejoin="round" />
      </svg>
      <div className="flex justify-between text-ui-caption text-fg-subtlest"><span>{new Date(t0 * 1000).toLocaleTimeString()}</span><span>max {f(Math.max(...ok.map(p => p.v)))}</span><span>{new Date(t1 * 1000).toLocaleTimeString()}</span></div>
    </div>
  )
}

/** 累计计数(出站字节)换成每秒速率:相邻两点的差除以间隔;计数回退(重启)时记为 null。 */
export function rate(points: { t: number; v?: number }[]): Point[] {
  const out: Point[] = []
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i], dt = b.t - a.t
    out.push({ t: b.t, v: a.v === undefined || b.v === undefined || dt <= 0 || b.v < a.v ? null : (b.v - a.v) / dt })
  }
  return out
}

/** 柱状图:每根一个标签与数值;高度按最大值;零值留空位。 */
export function BarChart({ title, bars, fmt }: { title: string; bars: { label: string; v: number }[]; fmt: (v: number) => string }) {
  const W = 560, H = 120, PAD = 4, max = Math.max(...bars.map(b => b.v), 1e-9), bw = (W - 2 * PAD) / Math.max(bars.length, 1)
  const total = bars.reduce((a, b) => a + b.v, 0)
  return (
    <div className="rounded-xl border border-border bg-panel p-3">
      <div className="flex items-baseline justify-between"><span className="text-ui-sm text-fg-subtle">{title}</span><span className="text-ui-lg font-medium">{fmt(total)}</span></div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}: ${fmt(total)}`} className="mt-1 h-28 w-full">
        <line x1={PAD} x2={W - PAD} y1={H - PAD} y2={H - PAD} stroke="var(--c-border)" />
        {bars.map((b, i) => { const h = (b.v / max) * (H - 2 * PAD); return <rect key={i} x={PAD + i * bw + 1} y={H - PAD - h} width={Math.max(bw - 2, 1)} height={h} rx="1.5" fill="var(--c-link)" opacity="0.85"><title>{`${b.label}: ${fmt(b.v)}`}</title></rect> })}
      </svg>
      <div className="flex justify-between text-ui-caption text-fg-subtlest"><span>{bars[0]?.label}</span><span>{bars[bars.length - 1]?.label}</span></div>
    </div>
  )
}
