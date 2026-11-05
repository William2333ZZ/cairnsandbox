// The HTTP client and the top-level collections. Mirrors the Python SDK (sdk/python/cairnsandbox): same names, snake_case on the wire,
// camelCase in the options. The server's JSON comes back as plain objects; nothing is hidden behind classes that lag the contract.
import { Sandbox } from "./sandbox.ts";

export type Json = Record<string, any>;

export class ApiError extends Error {
  status: number; body?: unknown; method: string; path: string;
  constructor(status: number, message: string, body?: unknown, method = "", path = "") {
    super(`${method} ${path} -> ${status}: ${message}`);
    this.name = new.target.name; this.status = status; this.body = body; this.method = method; this.path = path;
  }
}
export class NotFound extends ApiError {}
export class Forbidden extends ApiError {}
/** 429: no node with room, a volume busy, the sandbox quota. */
export class NoCapacity extends ApiError {}
/** 402: the project's daily budget is spent. */
export class QuotaExceeded extends ApiError {}
/** 202: the change loosens a sandbox's egress and waits for a project owner; `.request` is the pending request. */
export class NeedsApproval extends ApiError {
  request: unknown;
  constructor(status: number, message: string, body: any, method: string, path: string) {
    super(status, message, body, method, path);
    this.request = body?.request;
  }
}

function raise(method: string, path: string, status: number, body: unknown): never {
  const b = body as Json | string | undefined;
  const msg = (typeof b === "object" && b && (b as Json).error) || (typeof b === "string" ? b : JSON.stringify(b ?? "").slice(0, 200));
  const cls = ({ 404: NotFound, 403: Forbidden, 429: NoCapacity, 402: QuotaExceeded } as Record<number, typeof ApiError>)[status] ?? ApiError;
  throw new cls(status, String(msg), body, method, path);
}

export interface ClientOptions { url?: string; token?: string; project?: string; timeoutMs?: number; fetch?: typeof fetch }
export interface RequestOptions { body?: unknown; raw?: boolean; headers?: Record<string, string>; timeoutMs?: number; stream?: boolean }

const env = (k: string): string | undefined => (typeof process !== "undefined" ? process.env?.[k] : undefined);
const drop = (o: Json): Json => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null));
const qs = (o: Json): string => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(drop(o))) p.set(k, String(v)); const s = p.toString(); return s ? "?" + s : ""; };
export { drop, qs };

export class Client {
  url: string; token: string; project?: string; timeoutMs: number;
  private f: typeof fetch;
  sandboxes = new Sandboxes(this); volumes = new Volumes(this); templates = new Templates(this); layers = new Layers(this);
  builds = new Builds(this); billing = new Billing(this); projects = new Projects(this); network = new NetworkRequests(this);

  /** One control plane, one bearer token, optionally one project (sent as X-Project). CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT fill what is left out. */
  constructor(o: ClientOptions = {}) {
    this.url = (o.url ?? env("CAIRN_URL") ?? "http://127.0.0.1:18700").replace(/\/+$/, "");
    this.token = o.token ?? env("CAIRN_TOKEN") ?? "";
    this.project = o.project ?? env("CAIRN_PROJECT") ?? undefined;
    this.timeoutMs = o.timeoutMs ?? 600_000;
    this.f = o.fetch ?? fetch;
  }

  headers(extra: Record<string, string> = {}): Record<string, string> {
    return { ...(this.token ? { Authorization: "Bearer " + this.token } : {}), ...(this.project ? { "X-Project": this.project } : {}), ...extra };
  }

  /** JSON in (object) or bytes in; JSON out, or bytes with raw, or the Response with stream. 4xx/5xx throw an ApiError subclass; 202 throws NeedsApproval. */
  async request(method: string, path: string, o: RequestOptions = {}): Promise<any> {
    const bin = o.body instanceof Uint8Array || o.body instanceof ArrayBuffer;
    const headers = this.headers({ "Content-Type": bin ? "application/octet-stream" : "application/json", ...(o.headers ?? {}) });
    let res: Response;
    try {
      res = await this.f(this.url + path, {
        method, headers, signal: AbortSignal.timeout(o.timeoutMs ?? this.timeoutMs),
        body: o.body === undefined ? undefined : bin ? (o.body as any) : JSON.stringify(o.body),
      });
    } catch (e) {
      throw new ApiError(0, `transport: ${(e as Error).message}`, undefined, method, path);
    }
    if (!res.ok) {
      const text = await res.text();
      let parsed: unknown = text;
      try { parsed = text ? JSON.parse(text) : {}; } catch { /* keep the text */ }
      raise(method, path, res.status, parsed);
    }
    if (o.stream) return res;
    if (o.raw) return new Uint8Array(await res.arrayBuffer());
    const text = await res.text();
    const out = text ? JSON.parse(text) : {};
    if (res.status === 202) throw new NeedsApproval(202, "waiting for an owner's approval", out, method, path);
    return out;
  }
  get(path: string, o?: RequestOptions) { return this.request("GET", path, o); }
  post(path: string, body?: unknown, o?: RequestOptions) { return this.request("POST", path, { ...o, body }); }
  put(path: string, body?: unknown, o?: RequestOptions) { return this.request("PUT", path, { ...o, body }); }
  delete(path: string, o?: RequestOptions) { return this.request("DELETE", path, o); }

  me() { return this.get("/me"); }
  health() { return this.get("/health"); }
  async nodes() { return (await this.get("/nodes")).nodes; }
}

export interface CreateOptions {
  template?: string; mem?: number; vcpu?: number; disk?: number; class?: "persistent" | "ephemeral"; network?: Json; env?: Record<string, string>;
  volumes?: Array<string | Json>; layers?: string[]; template_key?: string; ttl_idle_s?: number; ttl_max_s?: number; retain_paused_s?: number;
  priority?: "interactive" | "batch" | "background"; qos?: string; boot?: string; durable?: boolean; node?: string; metadata?: Json; [extra: string]: unknown;
}

export class Sandboxes {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  /** A new sandbox (CONTRACT §1 POST /sandboxes). Unset options are left to the server's defaults. */
  async create(o: CreateOptions = {}) { return new Sandbox(this.c, await this.c.post("/sandboxes", drop(o))); }
  async get(id: string) { return new Sandbox(this.c, await this.c.get(`/sandboxes/${id}`)); }
  /** status / metadata ({k: v}, all must match) / q (substring) / limit / offset (newest first). */
  async list(f: { status?: string | string[]; metadata?: Record<string, string>; q?: string; limit?: number; offset?: number } = {}) {
    const p = new URLSearchParams();
    for (const s of ([] as string[]).concat(f.status ?? [])) p.append("status", s);
    for (const [k, v] of Object.entries(f.metadata ?? {})) p.append("metadata", `${k}:${v}`);
    for (const k of ["q", "limit", "offset"] as const) if (f[k] !== undefined) p.set(k, String(f[k]));
    const r = await this.c.get("/sandboxes" + (p.toString() ? "?" + p : ""));
    return (Array.isArray(r) ? r : r.sandboxes).map((x: Json) => new Sandbox(this.c, x));
  }
  /** Re-home a durable sandbox from its last published bundle (after a node was lost and nobody evacuated it). */
  async importBundle(bundle: string, node?: string) { return new Sandbox(this.c, await this.c.post("/sandboxes/import", drop({ bundle, node }))); }
}

export class Volumes {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  create(name: string, o: { size_mib?: number; project?: string; node?: string } = {}) { return this.c.post("/volumes", drop({ name, size_mib: 1024, ...o })); }
  /** A volume whose filesystem holds the tar's tree, made without mounting. */
  fromArchive(name: string, tar: Uint8Array, o: { size_mib?: number; node?: string } = {}) { return this.c.post("/volumes/from-archive" + qs({ name, ...o }), tar); }
  async list() { return (await this.c.get("/volumes")).volumes; }
  get(id: string) { return this.c.get(`/volumes/${id}`); }
  delete(id: string) { return this.c.delete(`/volumes/${id}`); }
}

export class Templates {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  async list() { return (await this.c.get("/templates")).templates; }
  delete(name: string) { return this.c.delete(`/templates/${name}`); }   // frees the name; sandboxes already started from it keep running
  everywhere(spec: Json) { return this.c.post("/templates/everywhere", spec); }
}
export class Layers {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  async list() { return (await this.c.get("/layers")).layers; }
  everywhere(id: string) { return this.c.post(`/layers/${id}/everywhere`); }
}

/** Builds: an OCI image becomes a layer, a running sandbox becomes a named template. */
export class Builds {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  fromOci(image: string, o: { name?: string; node?: string } = {}) { return this.c.post("/builds", drop({ kind: "oci", image, ...o })); }
  fromArchive(tar: Uint8Array, name: string, node?: string) { return this.c.post("/builds/archive" + qs({ name, node }), tar); }
  commit(sandbox: string, name: string, note?: string) { return this.c.post("/builds", drop({ kind: "commit", sandbox, name, note })); }
  get(id: string) { return this.c.get(`/builds/${id}`); }
  async list() { return (await this.c.get("/builds")).builds; }
  /** The node's progress lines for a build (oci builds); pass `next` back as `since` to get only what is new. */
  log(id: string, since = 0): Promise<{ lines: string[]; next: number }> { return this.c.get(`/builds/${id}/log` + qs({ since })); }
  async wait(id: string, o: { timeoutS?: number; everyS?: number } = {}) {
    const t0 = Date.now(), timeout = (o.timeoutS ?? 1800) * 1000, every = (o.everyS ?? 2) * 1000;
    for (;;) {
      const b = await this.get(id);
      if (b.status === "done" || b.status === "failed" || Date.now() - t0 > timeout) return b;
      await new Promise(r => setTimeout(r, every));
    }
  }
}

export class Billing {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  private p(project?: string) { return project ?? this.c.project ?? "default"; }
  async today(project?: string): Promise<number> { return (await this.c.get(`/metering/projects/${this.p(project)}/today`)).spent_usd; }
  statement(o: { project?: string; from?: number; to?: number } = {}) { return this.c.get(`/metering/projects/${this.p(o.project)}/statement` + qs({ from: o.from, to: o.to })); }
  async statementCsv(o: { project?: string; from?: number; to?: number } = {}) { return new TextDecoder().decode(await this.c.get(`/metering/projects/${this.p(o.project)}/statement` + qs({ format: "csv", from: o.from, to: o.to }), { raw: true })); }
  settle(from: number, to: number, project?: string) { return this.c.post(`/metering/projects/${this.p(project)}/statements`, { from, to }); }
  async statements(project?: string) { return (await this.c.get(`/metering/projects/${this.p(project)}/statements`)).statements; }
  prices() { return this.c.get("/metering/prices"); }
  async sandboxUsage(id: string) { return (await this.c.get(`/metering/sandboxes/${id}`)).usage; }
}

/** Tenancy: accounts, projects, members, quotas, the project's default network policy. */
export class Projects {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  private p(project?: string) { return project ?? this.c.project ?? "default"; }
  async list(account?: string) { return (await this.c.get(`/accounts/${account ?? (await this.c.me()).account ?? "default"}/projects`)).projects; }
  quota(project?: string) { return this.c.get(`/projects/${this.p(project)}/quota`); }
  setQuota(q: { max_sandboxes?: number; max_storage_gib?: number; daily_usd?: number }, project?: string) { return this.c.put(`/projects/${this.p(project)}/quota`, q); }
  async members(project?: string) { return (await this.c.get(`/projects/${this.p(project)}/members`)).members; }
  addMember(user: string, o: { role?: string; name?: string; project?: string } = {}) { return this.c.post(`/projects/${this.p(o.project)}/members`, drop({ user, role: o.role ?? "member", name: o.name })); }
  async keys(project?: string) { return (await this.c.get(`/projects/${this.p(project)}/keys`)).keys; }
  /** A named key that reaches this project only (role member | viewer). Returns { key, secret }: the secret is shown once. */
  createKey(name: string, o: { role?: "member" | "viewer"; ttl_s?: number; project?: string } = {}) { return this.c.post(`/projects/${this.p(o.project)}/keys`, drop({ name, role: o.role ?? "member", ttl_s: o.ttl_s })); }
  revokeKey(id: string, project?: string) { return this.c.delete(`/projects/${this.p(project)}/keys/${id}`); }
  async defaultNetwork(project?: string) { return (await this.c.get(`/projects/${this.p(project)}/network`)).network; }
  setDefaultNetwork(network: Json, project?: string) { return this.c.put(`/projects/${this.p(project)}/network`, network); }
}

/** What waits for an owner: egress relaxations asked by members. */
export class NetworkRequests {
  private c: Client;
  constructor(c: Client) { this.c = c; }
  async requests(all = false) { return (await this.c.get("/network/requests" + (all ? "?all=1" : ""))).requests; }
  async approve(id: string) { return (await this.c.post(`/network/requests/${id}/approve`)).request; }
  async deny(id: string) { return (await this.c.post(`/network/requests/${id}/deny`)).request; }
}
