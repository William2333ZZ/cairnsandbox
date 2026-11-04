// Wire types, aligned with CONTRACT.md. Keep names as the services send them.

export interface NodeHealth { alive: boolean; running?: number | null; total?: number | null; mem_free_mib?: number | null; last_err?: string | null }
export interface Health { ok: boolean; nodes: Record<string, NodeHealth>; routes: number }

export interface Sandbox {
  id: string; status: string; template?: string; class?: 'persistent' | 'ephemeral'; vcpu?: number; mem?: number; disk?: number
  ttl_idle_s?: number; ttl_max_s?: number; created?: number; node?: string; durable?: boolean; boot_mode?: string; layers?: string[]
  network?: { mode?: string; allow_hosts?: string[]; allow_direct?: string[]; limits?: { requests_per_min?: number; max_bytes?: number } }
  checkpoints?: string[]; volumes?: { id: string; mount: string; ro: boolean }[]; env_keys?: string[]; priority?: string; retain_paused_s?: number; paused_at?: number | null
  disk_used_mib?: number | null; egress?: { out: number; in: number }; ip?: string | null; [k: string]: unknown
}

export type Stage = 'person/delivery' | 'person/approve' | 'person/answer' | 'person/review' | 'person/blocked' | 'person/failed' | 'working' | 'working/queued' | 'idle' | 'ended'
export type ReportState = 'progress' | 'waiting' | 'ready' | 'blocked' | 'failed' | 'dropped'
export interface Report { state: ReportState; text?: string; attention?: { kind?: 'question' | 'approval'; question?: string; action?: { kind?: string }; draft?: string }; deliverable?: { branch: string; head: string; base: string } }
export interface Review { verdict: 'ok' | 'warn' | 'block'; reasons: { level: 'warn' | 'block'; why: string }[]; model?: string | null }
export interface Approval { review?: Review | null; id: string; ticket: string; short?: string; ticket_title?: string; kind: 'merge' | 'send' | 'budget' | 'routine'; action: Record<string, unknown>; draft: string; state: 'pending' | 'approved' | 'denied' | 'expired'; created: number; decided?: number | null; decided_by?: string | null; result?: unknown; error?: string | null }

export interface TicketRow {
  id: string; short: string; title: string; thread: string; source: string; state: 'open' | 'ended'; ended_by?: string | null; outcome?: string | null
  branch: string; pr?: { number?: number; url?: string; state?: string } | null; attention?: { question: string; asked: number } | null
  last_report?: { state: ReportState; text?: string; at: number } | null; workspace_layer?: string | null; created: number; updated: number
  stage: Stage; stage_label: string; running: boolean; strength: 'explicit' | 'inferred'; actor: string
}
export interface Mandate { policy_version: string; scope: string; strength: string; basis: string; matched_rules: string[]; actor: string; source: string; budget_usd: number; network: string; version?: unknown; accepted: number }
export interface Input { kind: string; actor?: string; text: string; human?: boolean; ts: number; delivered?: boolean; trigger?: string }
export interface Assignment { id: string; state: string; sandbox?: string | null; node?: string | null; layers?: string[] | null; reports: Report[]; agent_exit?: number | null; cost?: number; tokens_in?: number; tokens_out?: number; elapsed_s?: number | null; error?: string | null; recovery?: boolean; created: number; started?: number | null; ended?: number | null }
export interface Reply { purpose: string; text: string; state: string; created: number }
export interface Ticket extends TicketRow { mandate: Mandate; inputs: Input[]; channels: unknown[]; assignments: Assignment[]; replies: Reply[]; subscriptions: string[] }

export interface Decision { id: string; thread: string; triggers: string[]; outcome: 'accept' | 'stop' | 'ignore' | 'unavailable'; strength?: string | null; basis?: string | null; reason?: string; matched_rules: string[]; would_accept?: boolean; policy_version: string; by_whom: string; ticket?: string | null; created: number }
export interface Trigger { id: string; source: string; actor: string; human: boolean; addressed: boolean; scope?: string; text: string; url?: string | null; threads: string[]; disposition?: string; created: number }
export interface Thread { id: string; source: string; title?: string; subscriptions: string[]; eval?: { triggers: string[] } | null; closed?: unknown; created: number; updated: number; triggers?: Trigger[]; decisions?: Decision[]; tickets?: TicketRow[] }

export interface Attention {
  needs_person: TicketRow[]; unavailable: Decision[]; problems: { name: string; error: string; since: number }[]
  counts: { triggers_24h: number; decisions_24h: number; tickets_open: number; running: number; slots: number; would_accept_24h: number }
  paused: boolean; policy_version: string; mode: 'off' | 'observe' | 'auto'
  sources: Record<string, { enabled: boolean; repo?: string; bot?: string; cursor?: { polled: number } | null; error?: string | null }>
}
export interface Policy { version: string; mode: 'off' | 'observe' | 'auto'; rules: { name: string; text: string; enabled: boolean }[]; defaults: { budget_usd: number; network: string; version: unknown; scope: string }; sources: Record<string, { allowed: string[]; bots: string[]; admission?: Record<string, string[]> }> }

export interface TaskSetRow { name: string; version: string; n_tasks: number; groups: Record<string, number>; checked: boolean }
export interface Version { id: string; label: string; agent_cmd?: string | null; layers: string[]; note?: string; created: number; parent?: string | null; runtime?: string; harness_layer?: string }
export interface BatchRow { id: string; kind: string; taskset: string; version: string; k: number; label?: string; status: string; created: number; progress: { done: number; total: number; infra_fail: number }; summary?: unknown }
export interface RunCell { run_id: string; score: number | null; pass: boolean | null; infra_fail: boolean; cost: number; tokens_in: number; tokens_out: number; elapsed_s: number; cheat: unknown[] }
export interface Batch extends BatchRow { runs: string[]; matrix: Record<string, RunCell[]>; stats?: { mean?: number; avg_k?: number; delta?: number; paired?: { diff: number; ci95: [number, number]; mcnemar_p: number; n_tasks: number }; accept?: boolean; reason?: string } }
export interface Event { seq: number; ts: number; uid: number; type: string; data: unknown }

// docs/005 对话与路由: the user's conversation with their agent
export type SayKind = 'ack' | 'ready' | 'stuck' | 'question' | 'approval' | 'digest' | 'update'
export interface ConvRow {
  id: string; ts: number; kind: 'message' | 'say' | 'work' | 'note'; say?: SayKind | null; actor?: string | null; text: string; ticket?: string | null
  origin?: { source?: string; thread?: string; url?: string | null }; dest?: { channels?: unknown[] }; delivered?: number; meta?: Record<string, unknown>
  stage?: Stage; short?: string; pr?: { number?: number; url?: string } | null
}
