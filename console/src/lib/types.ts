export type NetPolicy = { mode?: string; allow_hosts?: string[]; allow_direct?: string[]; rules?: unknown[]; limits?: { requests_per_min?: number; max_bytes?: number } }
export type Sandbox = {
  id: string; status: string; class?: string; template?: string; vcpu?: number; mem?: number; disk?: number; node?: string; project?: string; created?: number; ip?: string | null
  network?: NetPolicy; volumes?: { id: string; mount: string; ro: boolean }[]; checkpoints?: string[]; env_keys?: string[]; labels?: Record<string, string>; metadata?: Record<string, string>
  disk_used_mib?: number | null; egress?: { out: number; in: number }; ttl_idle_s?: number; ttl_max_s?: number; retain_paused_s?: number; durable?: boolean; boot_mode?: string; layers?: string[]; durable_bundle?: string
}
export type AuditEvent = { op: string; ts: number; project?: string; for_project?: string; sandbox?: string; by?: string; [k: string]: unknown }
export type NetRequest = { id: string; sandbox: string; project: string; by: string; network: Record<string, unknown>; created: number; status: string }
export type Decision = { method?: string; scheme?: string; host?: string; port?: number; path?: string | null; allowed: boolean; rule?: string | null; injected?: string[] }
export type Checkpoint = { name: string; note?: string; created: number; chain_len?: number; upper_mib?: number; paused_s?: number }
export type NodeRow = { name: string; url: string; alive: boolean; cordoned?: boolean; load?: number; reserve_vms?: number; reserve_mem_mib?: number; health?: Record<string, number | string | boolean | null> | null; last_err?: string | null }
