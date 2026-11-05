#!/usr/bin/env node
// E2B conformance, JS flavour: the OFFICIAL `e2b` npm package against a CairnSandbox control plane's E2B surface, case by case (the Python run is ops/e2b-conformance.py).
//
//   npm install e2b
//   E2B_API_URL=http://127.0.0.1:18710 E2B_SANDBOX_URL=http://127.0.0.1:18710 E2B_API_KEY=<token> node ops/e2b-conformance.mjs [--json out.json]
//
// Same rules as the Python script: each case is PASS / FAIL / SKIP with the reason, the exit code is the number of FAILs.
import { Sandbox, CommandExitError, NotFoundError, ConnectionConfig } from 'e2b'
import { writeFileSync, readFileSync, existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const eq = (a, b, m = '') => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m} expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`) }
const ok = (c, m) => { if (!c) throw new Error(m) }
const TEMPLATE = process.env.E2B_TEMPLATE || 'base'
const CASES = []
const kase = (group, name, fn) => CASES.push([group, name, fn])

kase('commands', 'run: stdout, stderr, exit code 0', async (sb) => {
  const r = await sb.commands.run('echo hello; echo -n world; echo err >&2')
  eq(r.stdout, 'hello\nworld'); eq(r.stderr, 'err\n'); eq(r.exitCode, 0)
})
kase('commands', 'run: non-zero exit throws CommandExitError with the code', async (sb) => {
  try { await sb.commands.run('exit 7') } catch (e) { ok(e instanceof CommandExitError, String(e)); eq(e.exitCode, 7); return }
  throw new Error('no error')
})
kase('commands', 'run: streaming callbacks deliver output while it is produced', async (sb) => {
  const got = [], t = [], t0 = Date.now()
  await sb.commands.run('echo a; sleep 1; echo b', { onStdout: (d) => { got.push(d); t.push(Date.now() - t0) } })
  eq(got.join(''), 'a\nb\n'); ok(t.length >= 2 && t[t.length - 1] - t[0] > 500, `not streamed: ${t}`)
})
kase('commands', 'run: envs, cwd, user=root', async (sb) => {
  eq((await sb.commands.run('echo $FOO; pwd', { envs: { FOO: 'bar' }, cwd: '/tmp' })).stdout, 'bar\n/tmp\n')
  eq((await sb.commands.run('id -un', { user: 'root' })).stdout.trim(), 'root')
})
kase('commands', 'background: handle, list, kill', async (sb) => {
  const h = await sb.commands.run('sleep 60', { background: true })
  ok((await sb.commands.list()).some((p) => p.pid === h.pid), 'not listed')
  ok(await sb.commands.kill(h.pid), 'kill returned false')
})
kase('commands', 'stdin: sendStdin to a stdin-enabled process', async (sb) => {
  const h = await sb.commands.run('cat', { background: true, stdin: true })
  await sb.commands.sendStdin(h.pid, 'ping\n'); await sleep(500)
  await sb.commands.closeStdin(h.pid).catch(() => {})
  const r = await h.wait()
  ok(r.stdout.includes('ping'), r.stdout)
})
kase('commands', 'connect: re-attach to a running background command', async (sb) => {
  const h = await sb.commands.run("sh -c 'echo first; sleep 2; echo second'", { background: true })
  await sleep(500)
  const h2 = await sb.commands.connect(h.pid)
  const r = await h2.wait()
  ok(r.stdout.includes('second'), r.stdout)
})
kase('pty', 'create, send input, read output, resize, kill', async (sb) => {
  const out = []
  const h = await sb.pty.create({ cols: 80, rows: 24, onData: (d) => out.push(Buffer.from(d).toString()) })
  await sb.pty.sendInput(h.pid, new TextEncoder().encode('echo pty-ok\n')); await sleep(800)
  await sb.pty.resize(h.pid, { cols: 100, rows: 30 })
  ok(out.join('').includes('pty-ok'), JSON.stringify(out))
  ok(await sb.pty.kill(h.pid), 'kill false')
})
kase('files', 'write + read text and bytes', async (sb) => {
  const i = await sb.files.write('/tmp/e2b/a.txt', 'héllo'); eq(i.path, '/tmp/e2b/a.txt')
  eq(await sb.files.read('/tmp/e2b/a.txt'), 'héllo')
  const b = Uint8Array.from({ length: 256 }, (_, k) => k)
  await sb.files.write('/tmp/e2b/b.bin', b.buffer)
  eq(Array.from(await sb.files.read('/tmp/e2b/b.bin', { format: 'bytes' })), Array.from(b))
})
kase('files', 'writeFiles (several at once)', async (sb) => {
  await sb.files.write([{ path: '/tmp/e2b/m1.txt', data: '1' }, { path: '/tmp/e2b/m2.txt', data: '2' }])
  eq(await sb.files.read('/tmp/e2b/m1.txt'), '1'); eq(await sb.files.read('/tmp/e2b/m2.txt'), '2')
})
kase('files', 'a file written by the SDK is visible to commands and vice versa', async (sb) => {
  await sb.files.write('/tmp/e2b/c.txt', 'from sdk'); eq((await sb.commands.run('cat /tmp/e2b/c.txt')).stdout, 'from sdk')
  await sb.commands.run('echo -n from-shell > /tmp/e2b/d.txt'); eq(await sb.files.read('/tmp/e2b/d.txt'), 'from-shell')
})
kase('files', 'read of a missing file throws NotFoundError', async (sb) => {
  try { await sb.files.read('/tmp/e2b/nope') } catch (e) { ok(e instanceof NotFoundError, `${e?.name}: ${e?.message}`); return }
  throw new Error('no error')
})
kase('files', 'list, exists, getInfo', async (sb) => {
  await sb.files.write('/tmp/e2b/list/x.txt', 'x')
  eq((await sb.files.list('/tmp/e2b/list')).map((e) => e.name), ['x.txt'])
  ok(await sb.files.exists('/tmp/e2b/list/x.txt') && !(await sb.files.exists('/tmp/e2b/list/y.txt')), 'exists')
  const i = await sb.files.getInfo('/tmp/e2b/list/x.txt'); eq(Number(i.size), 1)
})
kase('files', 'makeDir (true, then false), rename, remove', async (sb) => {
  eq(await sb.files.makeDir('/tmp/e2b/newdir'), true); eq(await sb.files.makeDir('/tmp/e2b/newdir'), false)
  await sb.files.write('/tmp/e2b/newdir/f', '1'); await sb.files.rename('/tmp/e2b/newdir/f', '/tmp/e2b/newdir/g')
  ok(await sb.files.exists('/tmp/e2b/newdir/g') && !(await sb.files.exists('/tmp/e2b/newdir/f')), 'rename')
  await sb.files.remove('/tmp/e2b/newdir'); ok(!(await sb.files.exists('/tmp/e2b/newdir')), 'remove')
})
kase('files', 'watchDir sees a created file', async (sb) => {
  await sb.files.makeDir('/tmp/e2b/watch')
  const ev = []; const w = await sb.files.watchDir('/tmp/e2b/watch', (e) => ev.push(e))
  await sb.files.write('/tmp/e2b/watch/new.txt', '1'); await sleep(1500); await w.stop()
  ok(ev.some((e) => e.name === 'new.txt'), JSON.stringify(ev))
})
kase('files', 'a 20 MiB upload and download round-trips intact', async (sb) => {
  const data = new Uint8Array(20 << 20); for (let i = 0; i < data.length; i += 4099) data[i] = i & 255
  await sb.files.write('/tmp/e2b/big.bin', data.buffer)
  const back = await sb.files.read('/tmp/e2b/big.bin', { format: 'bytes' })
  ok(back.length === data.length && Buffer.compare(Buffer.from(back), Buffer.from(data)) === 0, 'differs')
})
kase('sandbox', 'getInfo, setTimeout, isRunning', async (sb) => {
  ok(await sb.isRunning(), 'not running'); await sb.setTimeout(600000); eq((await sb.getInfo()).sandboxId, sb.sandboxId)
})
kase('sandbox', 'pause then connect resumes with files intact', async (sb) => {
  await sb.files.write('/tmp/e2b/p.txt', 'kept'); await sb.pause()
  const s2 = await Sandbox.connect(sb.sandboxId)
  eq(await s2.files.read('/tmp/e2b/p.txt'), 'kept'); eq((await s2.commands.run('echo back')).stdout, 'back\n')
})
kase('sandbox', 'envs passed at create are visible to commands', async () => {
  const s = await Sandbox.create(TEMPLATE, { envs: { CREATE_ENV: 'yes' }, timeoutMs: 120000 })
  try { eq((await s.commands.run('echo $CREATE_ENV')).stdout, 'yes\n') } finally { await s.kill() }
})
kase('sandbox', 'kill, then the sandbox is gone', async () => {
  const s = await Sandbox.create(TEMPLATE, { timeoutMs: 120000 }); await s.kill(); await sleep(500)
  ok(!(await s.isRunning()), 'still running')
})

const args = process.argv.slice(2); const jsonOut = args.includes('--json') ? args[args.indexOf('--json') + 1] : null
for (const k of ['E2B_API_URL', 'E2B_API_KEY']) if (!process.env[k]) { console.error(`${k} is required`); process.exit(2) }
// Refuse to send the API key anywhere but where the caller said: an SDK version that ignores E2B_API_URL would send it to api.e2b.app.
let got; try { got = new ConnectionConfig({}).apiUrl } catch {}
if (!got || got.replace(/\/$/, '') !== process.env.E2B_API_URL.replace(/\/$/, '')) { console.error(`this SDK version would talk to ${got}, not E2B_API_URL=${process.env.E2B_API_URL}: it does not support a custom API URL (or it cannot be checked); nothing was sent`); process.exit(2) }
const sb = await Sandbox.create(TEMPLATE, { timeoutMs: 900000 })
const results = []
try {
  for (const [group, name, fn] of CASES) {
    const t0 = Date.now(); let st = 'PASS', why = ''
    try { await fn(sb) } catch (e) { st = 'FAIL'; why = `${e?.name}: ${String(e?.message).slice(0, 300)}` }
    results.push({ group, case: name, status: st, why, seconds: +((Date.now() - t0) / 1000).toFixed(2) })
    console.log(`${st.padEnd(5)} ${group.padEnd(9)} ${name}${why ? '  -- ' + why : ''}`)
  }
} finally { await sb.kill().catch(() => {}) }
const n = { PASS: 0, FAIL: 0, SKIP: 0 }; for (const r of results) n[r.status]++
let ver = '?'; try { let d = dirname(createRequire(import.meta.url).resolve('e2b')); while (d !== dirname(d) && !existsSync(join(d, 'package.json'))) d = dirname(d); ver = JSON.parse(readFileSync(join(d, 'package.json'), 'utf8')).version } catch {}
console.log(`\ne2b (npm) ${ver}: ${n.PASS} passed, ${n.FAIL} failed, ${n.SKIP} skipped, of ${results.length}`)
if (jsonOut) writeFileSync(jsonOut, JSON.stringify({ e2b_version: ver, api_url: process.env.E2B_API_URL, summary: n, results }, null, 1))
process.exit(n.FAIL)
