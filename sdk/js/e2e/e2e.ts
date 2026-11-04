// A smoke test of the JS SDK against a real control plane (not part of `npm test`): CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT in the environment.
// One small ephemeral sandbox; it is killed at the end. Exit code 1 on the first failed expectation.
import { Client, Forbidden } from "../src/index.ts";

let failed = 0;
const ok = (name: string, cond: unknown, detail = "") => { console.log(`[${cond ? "OK " : "FAIL"}] ${name} ${detail}`); if (!cond) failed++; };

const c = new Client({ timeoutMs: 120_000 });
const me = await c.me();
ok("who am I", me.user, JSON.stringify({ user: me.user, role: me.role, key: me.key, projects: me.projects }));
const sb = await c.sandboxes.create({ mem: 128, class: "ephemeral", ttl_max_s: 600 });
try {
  ok("create", sb.id?.startsWith("sb-"), `${sb.id} on ${sb.node}`);
  const r = await sb.commands.run("echo hello; echo oops >&2; exit 3");
  ok("commands.run keeps stdout, stderr and the exit code", r.stdout.trim() === "hello" && r.stderr.trim() === "oops" && r.exit === 3, JSON.stringify([r.stdout, r.stderr, r.exit]));
  ok("check() throws on a non-zero exit", (() => { try { r.check(); return false; } catch { return true; } })());
  const chunks: string[] = []; let done: any;
  for await (const ev of sb.commands.stream("echo a; sleep 0.3; echo b")) { if (ev.type === "done") done = ev.result; else chunks.push(ev.data); }
  ok("commands.stream yields chunks then the result", chunks.join("").replace(/\s/g, "") === "ab" && done?.exit === 0, JSON.stringify(chunks));
  ok("files.write returns the byte count", (await sb.files.write("/home/agent/a.txt", "héllo")) === 6);
  ok("files.readText round-trips UTF-8", (await sb.files.readText("/home/agent/a.txt")) === "héllo");
  ok("files.exists / list", (await sb.files.exists("/home/agent/a.txt")) && (await sb.files.list("/home/agent")).includes("a.txt"));
  await sb.checkpoints.create("mk");
  await sb.commands.run("rm /home/agent/a.txt");
  ok("the file is gone", !(await sb.files.exists("/home/agent/a.txt")));
  await sb.checkpoints.rollback("mk");
  ok("rollback brings it back", await sb.files.exists("/home/agent/a.txt"));
  ok("checkpoints.list", (await sb.checkpoints.list()).some((x: any) => x.name === "mk"));
  await sb.pause(); ok("pause", sb.status === "paused", sb.status);
  await sb.resume(); ok("resume then exec", (await sb.commands.run("echo again")).stdout.trim() === "again");
  ok("sandboxes.list contains it", (await c.sandboxes.list()).some((x: any) => x.id === sb.id));
  const bad = new Client({ token: "definitely-wrong" });
  try { await bad.sandboxes.list(); ok("a wrong token is refused", false); } catch (e) { ok("a wrong token is refused", (e as any).status === 401, String((e as Error).message).slice(0, 60)); }
  void Forbidden;
} finally {
  await sb.kill();
}
console.log(failed ? `FAILED ${failed}` : "ALL OK");
process.exit(failed ? 1 : 0);
