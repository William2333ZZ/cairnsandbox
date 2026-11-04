// One sandbox and the things done to it. Every method talks to the control plane; `info` is the last answer we have, `refresh()` asks again.
import { type Client, type Json, drop, qs } from "./client.ts";

export class CommandResult {
  exit: number | null; stdout: string; stderr: string; timedOut: boolean; elapsedS?: number; raw: Json;
  constructor(raw: Json) { this.raw = raw; this.exit = raw.exit ?? null; this.stdout = raw.stdout ?? ""; this.stderr = raw.stderr ?? ""; this.timedOut = !!raw.timeout; this.elapsedS = raw.elapsed_s; }
  /** Throw when the command did not exit 0 (like execa's default). */
  check(): this { if (this.exit !== 0) throw new Error(`exit ${this.exit}: ${(this.stderr || this.stdout).slice(-400)}`); return this; }
}

export type StreamEvent = { type: "stdout" | "stderr"; data: string } | { type: "done"; result: CommandResult };
export interface RunOptions { cwd?: string; env?: Record<string, string>; timeout?: number; user?: "agent" | "root"; stdin?: string; landlock?: boolean | Json }

const sh = (s: string) => JSON.stringify(s);

export class Commands {
  private sb: Sandbox;
  constructor(sb: Sandbox) { this.sb = sb; }
  /** Run a shell command (bash -c) and wait. `timeout` is in seconds. */
  async run(cmd: string, o: RunOptions = {}): Promise<CommandResult> {
    return new CommandResult(await this.sb.c.post(`/sandboxes/${this.sb.id}/exec`, drop({ cmd, ...o }), { timeoutMs: ((o.timeout ?? 600) + 30) * 1000 }));
  }
  /** Yield stdout / stderr chunks as the command runs; the last event is {type:"done", result}. */
  async *stream(cmd: string, o: Omit<RunOptions, "stdin" | "landlock"> = {}): AsyncGenerator<StreamEvent> {
    const res: Response = await this.sb.c.request("POST", `/sandboxes/${this.sb.id}/exec/stream`, { body: drop({ cmd, ...o }), stream: true, timeoutMs: ((o.timeout ?? 600) + 30) * 1000 });
    const dec = new TextDecoder(); let buf = "";
    for await (const chunk of res.body as unknown as AsyncIterable<Uint8Array>) {
      buf += dec.decode(chunk, { stream: true });
      let i: number;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.done) { yield { type: "done", result: new CommandResult(ev) }; return; }
        yield { type: ev.event === "err" ? "stderr" : "stdout", data: ev.data ?? "" };   // the node says out / err
      }
    }
  }
}

export class Files {
  private sb: Sandbox;
  constructor(sb: Sandbox) { this.sb = sb; }
  /** Whole file; a string is UTF-8. Returns the byte count. */
  async write(path: string, data: string | Uint8Array): Promise<number> {
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    return (await this.sb.c.put(`/sandboxes/${this.sb.id}/files?path=${encodeURIComponent(path)}`, bytes)).bytes;
  }
  read(path: string): Promise<Uint8Array> { return this.sb.c.get(`/sandboxes/${this.sb.id}/files?path=${encodeURIComponent(path)}`, { raw: true }); }
  async readText(path: string): Promise<string> { return new TextDecoder().decode(await this.read(path)); }
  /** Names in a directory (a trailing / marks sub-directories). */
  async list(path = "/home/agent"): Promise<string[]> { return (await this.sb.commands.run(`ls -1Ap ${sh(path)}`)).check().stdout.split("\n").filter(Boolean); }
  async exists(path: string) { return (await this.sb.commands.run(`test -e ${sh(path)}`)).exit === 0; }
  async remove(path: string) { (await this.sb.commands.run(`rm -rf -- ${sh(path)}`)).check(); }
  async mkdir(path: string) { (await this.sb.commands.run(`mkdir -p -- ${sh(path)}`)).check(); }
}

/** A long-lived shell (optionally a PTY): exec keeps state between calls; write / read are the raw byte stream. */
export class Session {
  private sb: Sandbox; id: string; pty: boolean;
  constructor(sb: Sandbox, id: string, pty: boolean) { this.sb = sb; this.id = id; this.pty = pty; }
  async exec(cmd: string, timeout = 60) { return new CommandResult(await this.sb.c.post(`/sandboxes/${this.sb.id}/sessions/${this.id}/exec`, { cmd, timeout }, { timeoutMs: (timeout + 30) * 1000 })); }
  async write(data: string | Uint8Array) {
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    await this.sb.c.post(`/sandboxes/${this.sb.id}/sessions/${this.id}/write`, { data_b64: Buffer.from(bytes).toString("base64") });
  }
  async read(waitS = 0.5): Promise<Uint8Array> {
    const r = await this.sb.c.post(`/sandboxes/${this.sb.id}/sessions/${this.id}/read`, { wait: waitS }, { timeoutMs: (waitS + 30) * 1000 });
    return new Uint8Array(Buffer.from(r.data_b64 ?? "", "base64"));
  }
  close() { return this.sb.c.delete(`/sandboxes/${this.sb.id}/sessions/${this.id}`); }
}

export class Pty {
  private sb: Sandbox;
  constructor(sb: Sandbox) { this.sb = sb; }
  async open(o: { user?: string; pty?: boolean } = {}) { const r = await this.sb.c.post(`/sandboxes/${this.sb.id}/sessions`, drop({ pty: true, ...o })); return new Session(this.sb, r.session, r.pty ?? o.pty ?? true); }
}

/** A service listening inside the sandbox, reached through the control plane. */
export class Ports {
  private sb: Sandbox;
  constructor(sb: Sandbox) { this.sb = sb; }
  url(port: number, path = "/") { return `${this.sb.c.url}/sandboxes/${this.sb.id}/ports/${Math.trunc(port)}${path.startsWith("/") ? path : "/" + path}`; }
  /** fetch() to the guest's port with the bearer and project added. For a WebSocket, open `url(port)` (ws:// scheme) with these headers via your websocket library. */
  fetch(port: number, path = "/", init: RequestInit = {}) { return fetch(this.url(port, path), { ...init, headers: { ...this.sb.c.headers(), ...(init.headers as Record<string, string> ?? {}) } }); }
  headers() { return this.sb.c.headers(); }
}

/** Points to go back to: taken live, rolled back in place, forked from; they travel with the sandbox when it migrates. */
export class Checkpoints {
  private sb: Sandbox;
  constructor(sb: Sandbox) { this.sb = sb; }
  create(name?: string, note?: string) { return this.sb.c.post(`/sandboxes/${this.sb.id}/checkpoints`, drop({ name, note })); }
  async list() { return (await this.sb.c.get(`/sandboxes/${this.sb.id}/checkpoints`)).checkpoints; }
  async rollback(name: string) { const r = await this.sb.c.post(`/sandboxes/${this.sb.id}/rollback`, { checkpoint: name }); this.sb.info = r; return r; }
  drop(name: string) { return this.sb.c.delete(`/sandboxes/${this.sb.id}/checkpoints/${name}`); }
}

export class NetworkPolicy {
  private sb: Sandbox;
  constructor(sb: Sandbox) { this.sb = sb; }
  async get() { return (await this.sb.refresh()).network; }
  /** mode / allow_hosts / allow_direct / rules / limits. Tightening applies at once; a member's loosening throws NeedsApproval (its .request waits for an owner). */
  async set(policy: Json) { return (await this.sb.c.put(`/sandboxes/${this.sb.id}/network`, policy)).network; }
  async decisions() { return (await this.sb.c.get(`/sandboxes/${this.sb.id}/egress`)).decisions; }
}

export class Sandbox {
  commands = new Commands(this); files = new Files(this); pty = new Pty(this); ports = new Ports(this); checkpoints = new Checkpoints(this); network = new NetworkPolicy(this);
  c: Client; info: Json;
  constructor(c: Client, info: Json) { this.c = c; this.info = info; }
  get id(): string { return this.info.id; }
  get status(): string | undefined { return this.info.status; }
  get node(): string | undefined { return this.info.node; }

  async refresh() { this.info = await this.c.get(`/sandboxes/${this.id}`); return this.info; }
  async pause() { this.info = await this.c.post(`/sandboxes/${this.id}/pause`); return this; }
  async resume() { this.info = await this.c.post(`/sandboxes/${this.id}/resume`); return this; }
  kill() { return this.c.delete(`/sandboxes/${this.id}`); }
  /** Idle / max TTL and how long a paused sandbox is kept (0 = until deleted). */
  async setTimeout(o: { ttl_idle_s?: number; ttl_max_s?: number; retain_paused_s?: number }) { this.info = await this.c.post(`/sandboxes/${this.id}/ttl`, drop(o)); return this; }
  /** n copy-on-write clones from now, or from a checkpoint; the parent keeps running. */
  async fork(o: { n?: number; checkpoint?: string; class?: string; ttl_idle_s?: number; ttl_max_s?: number; qos?: string } = {}) {
    const r = await this.c.post(`/sandboxes/${this.id}/fork`, drop({ n: 1, class: "ephemeral", ...o }));
    return (r.clones as Json[]).map(x => new Sandbox(this.c, x));
  }
  pack(name: string) { return this.c.post(`/sandboxes/${this.id}/pack`, { name }); }
  /** This moment (memory + disk) as a named template new sandboxes boot warm from (a Build; see client.builds.wait). */
  commit(name: string, note?: string) { return this.c.builds.commit(this.id, name, note); }
  migrate(to?: string) { return this.c.post(`/sandboxes/${this.id}/migrate`, to ? { to } : {}); }
  diff(cwd = "/work/repo", base = "main") { return this.c.get(`/sandboxes/${this.id}/diff` + qs({ cwd, base })); }
  usage() { return this.c.billing.sandboxUsage(this.id); }

  // ---- what the sandbox knows about the agent inside it (the kernel ABI; CONTRACT §7) ----
  async budget(o: { max_usd?: number; max_tokens?: number }) { return (await this.c.put(`/sandboxes/${this.id}/budget`, drop(o))).budget; }
  async ticket(ticket: string) { return (await this.c.put(`/sandboxes/${this.id}/ticket`, { ticket })).ticket; }
  inbox(text: string, o: { kind?: string; actor?: string; thread?: string } = {}) { return this.c.post(`/sandboxes/${this.id}/inbox`, drop({ kind: "note", text, ...o })); }
  /** Events from the sandbox's log (model calls, reports, notes, checkpoints) and the last sequence number. */
  async events(since = 0, max?: number): Promise<{ events: Json[]; lastSeq: number }> {
    const r = await this.c.get(`/sandboxes/${this.id}/events` + qs({ since, max }));
    return { events: r.events ?? [], lastSeq: r.last_seq ?? since };
  }
  /** Block until the harness files a report that ends its turn (ready / waiting / blocked / failed; includeProgress also returns on progress); null on timeout. */
  async waitForReport(o: { timeoutS?: number; everyS?: number; since?: number; includeProgress?: boolean } = {}): Promise<Json | null> {
    const t0 = Date.now(), timeout = (o.timeoutS ?? 3600) * 1000; let seq = o.since ?? 0;
    while (Date.now() - t0 < timeout) {
      const r = await this.events(seq); seq = r.lastSeq;
      for (const e of r.events) if (e.type === "report" && (o.includeProgress || ["ready", "waiting", "blocked", "failed"].includes(e.data?.state))) return e;
      await new Promise(res => setTimeout(res, (o.everyS ?? 2) * 1000));
    }
    return null;
  }
}
