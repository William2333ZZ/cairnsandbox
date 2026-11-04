import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { Client, NotFound, Forbidden, NoCapacity, QuotaExceeded, NeedsApproval, ApiError } from "../src/index.ts";

type Seen = { method: string; path: string; headers: http.IncomingHttpHeaders; body: Buffer };
async function serve(handler: (req: Seen, res: http.ServerResponse) => void) {
  const seen: Seen[] = [];
  const srv = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", c => chunks.push(c));
    req.on("end", () => { const s = { method: req.method!, path: req.url!, headers: req.headers, body: Buffer.concat(chunks) }; seen.push(s); handler(s, res); });
  });
  await new Promise<void>(r => srv.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${(srv.address() as any).port}`;
  return { url, seen, close: () => { srv.closeAllConnections(); srv.close(); } };
}
const json = (res: http.ServerResponse, code: number, body: unknown) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(body)); };

test("sends the bearer and the project, and drops unset options from the body", async () => {
  const s = await serve((_, res) => json(res, 201, { id: "sb-1", status: "running", node: "n1" }));
  const c = new Client({ url: s.url, token: "tok", project: "web" });
  const sb = await c.sandboxes.create({ mem: 512, vcpu: undefined, class: "ephemeral", network: { allow_hosts: ["pypi.org"] } });
  assert.equal(sb.id, "sb-1"); assert.equal(sb.status, "running"); assert.equal(sb.node, "n1");
  assert.equal(s.seen[0].headers.authorization, "Bearer tok"); assert.equal(s.seen[0].headers["x-project"], "web");
  assert.deepEqual(JSON.parse(s.seen[0].body.toString()), { mem: 512, class: "ephemeral", network: { allow_hosts: ["pypi.org"] } });
  s.close();
});

test("status codes become the matching error classes; 202 is NeedsApproval with the request", async () => {
  const codes: Record<string, [number, unknown]> = { "/a": [404, { error: "nope" }], "/b": [403, { error: "not yours" }], "/c": [429, { error: "quota: sandboxes" }], "/d": [402, { error: "quota: daily budget" }], "/e": [500, "boom"], "/f": [202, { request: { id: "r1" } }] };
  const s = await serve((r, res) => { const [code, body] = codes[r.path]; typeof body === "string" ? (res.writeHead(code), res.end(body)) : json(res, code, body); });
  const c = new Client({ url: s.url });
  await assert.rejects(c.get("/a"), (e: any) => e instanceof NotFound && e.status === 404 && /nope/.test(e.message));
  await assert.rejects(c.get("/b"), Forbidden); await assert.rejects(c.get("/c"), NoCapacity); await assert.rejects(c.get("/d"), QuotaExceeded);
  await assert.rejects(c.get("/e"), (e: any) => e instanceof ApiError && e.status === 500 && e.body === "boom");
  await assert.rejects(c.put("/f", {}), (e: any) => e instanceof NeedsApproval && (e.request as any).id === "r1");
  s.close();
});

test("a refused connection is an ApiError with status 0", async () => {
  const c = new Client({ url: "http://127.0.0.1:1", timeoutMs: 2000 });
  await assert.rejects(c.health(), (e: any) => e instanceof ApiError && e.status === 0 && /transport/.test(e.message));
});

test("commands.run returns a result; check() throws on a non-zero exit", async () => {
  const s = await serve((r, res) => json(res, 200, JSON.parse(r.body.toString()).cmd === "ok" ? { exit: 0, stdout: "hi\n", stderr: "", elapsed_s: 0.1 } : { exit: 3, stdout: "", stderr: "bad" }));
  const c = new Client({ url: s.url }); const box = new (await import("../src/sandbox.ts")).Sandbox(c, { id: "sb-1" });
  const r = await box.commands.run("ok", { timeout: 5, cwd: "/work" });
  assert.equal(r.stdout, "hi\n"); assert.equal(r.exit, 0); r.check();
  assert.deepEqual(JSON.parse(s.seen.at(-1)!.body.toString()), { cmd: "ok", timeout: 5, cwd: "/work" });
  await assert.rejects(box.commands.run("fail").then(x => x.check()), /exit 3: bad/);
  s.close();
});

test("commands.stream yields chunks as they arrive and ends with the result", async () => {
  const s = await serve((_, res) => {
    res.writeHead(200, { "content-type": "application/x-ndjson" });
    res.write('{"event":"out","data":"a"}\n{"event":"er'); setTimeout(() => { res.write('r","data":"b"}\n'); res.end('{"done":true,"exit":0,"stdout":"a","stderr":"b"}\n'); }, 20);
  });
  const c = new Client({ url: s.url }); const box = new (await import("../src/sandbox.ts")).Sandbox(c, { id: "sb-1" });
  const evs = []; for await (const e of box.commands.stream("x")) evs.push(e);
  assert.deepEqual(evs.slice(0, 2), [{ type: "stdout", data: "a" }, { type: "stderr", data: "b" }]);
  assert.equal(evs[2].type, "done"); assert.equal((evs[2] as any).result.exit, 0);
  s.close();
});

test("files write and read move raw bytes; the path is encoded", async () => {
  let stored: Buffer = Buffer.alloc(0);
  const s = await serve((r, res) => { if (r.method === "PUT") { stored = Buffer.from(r.body); json(res, 200, { bytes: r.body.length }); } else { res.writeHead(200); res.end(stored); } });
  const c = new Client({ url: s.url }); const box = new (await import("../src/sandbox.ts")).Sandbox(c, { id: "sb-1" });
  assert.equal(await box.files.write("/home/agent/a b.txt", "héllo"), 6);
  assert.equal(s.seen[0].path, "/sandboxes/sb-1/files?path=%2Fhome%2Fagent%2Fa%20b.txt"); assert.equal(s.seen[0].headers["content-type"], "application/octet-stream");
  assert.equal(await box.files.readText("/home/agent/a b.txt"), "héllo");
  s.close();
});

test("checkpoints, fork, network and ports go to the contract's paths", async () => {
  const s = await serve((r, res) => {
    if (r.path.endsWith("/fork")) return json(res, 200, { clones: [{ id: "sb-2" }, { id: "sb-3" }] });
    if (r.path.endsWith("/rollback")) return json(res, 200, { id: "sb-1", status: "running", rolled: true });
    if (r.path.endsWith("/checkpoints") && r.method === "GET") return json(res, 200, { checkpoints: [{ name: "mk" }] });
    json(res, 200, { network: { mode: "closed" } });
  });
  const c = new Client({ url: s.url, token: "t" }); const box = new (await import("../src/sandbox.ts")).Sandbox(c, { id: "sb-1" });
  const kids = await box.fork({ n: 2, checkpoint: "mk" }); assert.deepEqual(kids.map(k => k.id), ["sb-2", "sb-3"]);
  assert.deepEqual(JSON.parse(s.seen[0].body.toString()), { n: 2, class: "ephemeral", checkpoint: "mk" });
  await box.checkpoints.rollback("mk"); assert.equal(box.info.rolled, true);
  assert.deepEqual(await box.checkpoints.list(), [{ name: "mk" }]);
  assert.deepEqual(await box.network.set({ mode: "closed" }), { mode: "closed" }); assert.equal(s.seen.at(-1)!.method, "PUT");
  assert.equal(box.ports.url(8080, "x"), `${s.url}/sandboxes/sb-1/ports/8080/x`);
  assert.equal(box.ports.headers().Authorization, "Bearer t");
  s.close();
});

test("builds.wait polls until the build is done", async () => {
  let n = 0;
  const s = await serve((_, res) => json(res, 200, { id: "b1", status: ++n < 3 ? "running" : "done" }));
  const b = await new Client({ url: s.url }).builds.wait("b1", { everyS: 0.01 });
  assert.equal(b.status, "done"); assert.equal(n, 3);
  s.close();
});
