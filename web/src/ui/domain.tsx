// Domain badges: the fixed words and colors from DESIGN.md.
import { Badge, type Tone } from './index'
import type { ReportState, Stage, TicketRow } from '../api/types'

const STAGE: Record<Stage, Tone> = { 'person/delivery': 'waiting', 'person/approve': 'waiting', 'person/answer': 'waiting', 'person/review': 'waiting', 'person/blocked': 'warning', 'person/failed': 'destructive', working: 'success', 'working/queued': 'neutral', idle: 'neutral', ended: 'neutral' }
export function StageBadge({ t }: { t: TicketRow }) { return <Badge tone={STAGE[t.stage] ?? 'neutral'}>{t.stage_label || t.stage}</Badge> }

const REPORT: Record<ReportState, Tone> = { progress: 'neutral', waiting: 'neutral', ready: 'success', blocked: 'warning', failed: 'destructive', dropped: 'neutral' }
export function ReportBadge({ s }: { s?: ReportState | string | null }) { return s ? <Badge tone={REPORT[s as ReportState] ?? 'neutral'}>{s}</Badge> : <span className="text-fg-subtlest">-</span> }

const DECISION: Record<string, Tone> = { accept: 'success', stop: 'neutral', ignore: 'neutral', unavailable: 'warning' }
export function DecisionBadge({ s }: { s: string }) { return <Badge tone={DECISION[s] ?? 'neutral'}>{s}</Badge> }

const SANDBOX: Record<string, Tone> = { running: 'success', paused: 'warning', unreachable: 'destructive', failed: 'destructive', creating: 'neutral', deleted: 'neutral' }
export function SandboxBadge({ s }: { s?: string }) { return <Badge tone={SANDBOX[s ?? ''] ?? 'neutral'}>{s ?? '-'}</Badge> }

const BATCH: Record<string, Tone> = { done: 'success', running: 'brand', queued: 'neutral', failed: 'destructive' }
export function BatchBadge({ s }: { s: string }) { return <Badge tone={BATCH[s] ?? 'neutral'}>{s}</Badge> }

export function Short({ id, n = 8 }: { id?: string | null; n?: number }) { if (!id) return <span className="text-fg-subtlest">-</span>; const i = id.indexOf('-'); return <span className="font-mono text-ui-sm" title={id}>{(i >= 0 ? id.slice(i + 1) : id).slice(0, n)}</span> }
